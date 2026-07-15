"""XLSX document translator — direct ZIP + lxml approach.

Preserves all formatting, formulas, images, charts, VBA, merged cells, etc.
by modifying only text content nodes in the XLSX XML files.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import zipfile as zf
from pathlib import Path
from typing import Any

from lxml import etree

from file_translator.domain.errors import (
    DocumentOpenError,
    DocumentParseError,
    SaveDocumentError,
)
from file_translator.domain.interfaces import DocumentTranslator
from file_translator.domain.models import (
    DocumentFormat,
    DocumentMetadata,
    TextUnit,
)
from file_translator.infrastructure.classifiers.xlsx_content_classifier import (
    Category,
    SourceType,
    TextSource,
    XlsxContentClassifier,
)

logger = logging.getLogger(__name__)

# Safety limit: reject archives with uncompressed XML content over 512 MB
_MAX_ARCHIVE_XML_SIZE = 512 * 1024 * 1024  # 512 MB

_NSMAP = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


# Register the relationships namespace so it serializes as r: prefix.
# NOTE: we intentionally do NOT register the empty (default) namespace — in lxml ≥ 6
# etree.register_namespace("", uri) raises ValueError: Invalid tag name ''.
# Instead, _serialize_xml post‑processes the output to use a clean default namespace.
etree.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")


def _find_xml_files(z: zf.ZipFile) -> list[str]:
    """Return all XML and .rels file paths inside the archive, safe from ZipSlip."""
    result = []
    for n in z.namelist():
        if not n.lower().endswith((".xml", ".rels")):
            continue
        name = n.replace("\\", "/")
        if ".." in name.split("/"):
            logger.warning("Skipping ZipSlip path in archive: %s", n)
            continue
        result.append(n)
    return result


def _parse_xml(data: bytes) -> etree._Element:
    return etree.fromstring(data)


def _serialize_xml(tree: etree._Element) -> bytes:
    """Serialize XML, converting auto‑generated namespace prefixes to a clean default namespace.

    lxml ≥ 6 no longer allows ``etree.register_namespace("", uri)``, so without
    registration it emits ``ns0:tag`` / ``ns1:tag`` for the spreadsheet namespace.
    This function rewrites the serialized output so that the spreadsheet namespace
    appears as the default (unprefixed) namespace.
    """
    raw = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
    text = raw.decode("UTF-8")
    # Find any auto‑generated ns-prefix that maps to our spreadsheet namespace
    # e.g. xmlns:ns0="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_pat = re.compile(r'xmlns:(ns\d+)="' + re.escape(_SHEET_NS) + r'"')
    m = ns_pat.search(text)
    if m:
        prefix = m.group(1)
        # Strip the auto‑generated prefix from all element tags
        text = re.sub(rf'</?{prefix}:', lambda m: m.group(0).replace(f'{prefix}:', ''), text)
        # Replace xmlns:nsX="..." with xmlns="..."
        text = text.replace(f'xmlns:{prefix}="{_SHEET_NS}"', f'xmlns="{_SHEET_NS}"')
    return text.encode("UTF-8")


# ── Translation helpers (module level, reusable) ──


def _set_r_text(r_runs: list[etree._Element], translated: str) -> None:
    """Distribute translated text across rich text runs proportionally."""
    if not r_runs:
        return
    if len(r_runs) == 1:
        t_elem = r_runs[0].find(f"{{{_SHEET_NS}}}t")
        if t_elem is not None:
            t_elem.text = translated
        return

    original_lengths = []
    total_len = 0
    for r in r_runs:
        t_elem = r.find(f"{{{_SHEET_NS}}}t")
        length = len(t_elem.text) if t_elem is not None and t_elem.text else 0
        original_lengths.append(length)
        total_len += length
    if total_len == 0:
        return

    trans_chars = list(translated)
    pos = 0
    for i, r in enumerate(r_runs):
        t_elem = r.find(f"{{{_SHEET_NS}}}t")
        if t_elem is None:
            continue
        proportion = original_lengths[i] / total_len if total_len > 0 else 0
        chunk_size = max(1, int(len(trans_chars) * proportion)) if i < len(r_runs) - 1 else len(trans_chars) - pos
        chunk_size = min(chunk_size, len(trans_chars) - pos)
        t_elem.text = "".join(trans_chars[pos:pos + chunk_size])
        pos += chunk_size


def _translate_sources_by_path(
    archive_data: dict[str, bytes],
    sources: list[TextSource],
    translations: dict[str, str],
) -> None:
    """Apply *translations* to the XML trees in *archive_data*.

    Sources are grouped by file path so each XML file is parsed at most
    once.  Element location uses the metadata stored by the classifier.
    """
    # Group translations by file path
    by_path: dict[str, list[TextSource]] = {}
    for src in sources:
        if src.id not in translations:
            continue
        by_path.setdefault(src.path, []).append(src)

    for path, path_sources in by_path.items():
        data = archive_data.get(path)
        if data is None:
            continue
        try:
            tree = _parse_xml(data)
        except Exception as e:
            logger.warning("Failed to parse %s for translation: %s", path, e)
            continue

        for src in path_sources:
            translated = translations[src.id]
            try:
                _apply_one(src, tree, translated)
            except Exception as e:
                logger.warning(
                    "Failed to apply translation for %s (%s): %s",
                    src.id, path, e,
                )

        archive_data[path] = _serialize_xml(tree)


def _apply_one(src: TextSource, tree: etree._Element, translated: str) -> None:
    """Locate the text-bearing element described by *src* in *tree* and
    replace its text content with *translated*."""
    st = src.source_type

    if st == SourceType.SHARED_STRING:
        _apply_to_shared_string(tree, src, translated)
    elif st == SourceType.INLINE_STRING:
        _apply_to_inline_string(tree, src, translated)
    elif st == SourceType.COMMENT:
        _apply_to_comment(tree, src, translated)
    elif st in (SourceType.CHART_TEXT, SourceType.SHAPE_TEXT):
        _apply_to_chart_or_shape(tree, src, translated)
    else:
        logger.debug("No handler for source_type=%s (%s)", st, src.id)


def _apply_to_shared_string(tree: etree._Element, src: TextSource, translated: str) -> None:
    si_index = src.metadata.get("si_index")
    if si_index is None:
        return
    for idx, si in enumerate(tree.iter(f"{{{_SHEET_NS}}}si")):
        if idx != si_index:
            continue
        r_runs = si.findall(f"{{{_SHEET_NS}}}r")
        if r_runs:
            _set_r_text(r_runs, translated)
        else:
            t_elem = si.find(f"{{{_SHEET_NS}}}t")
            if t_elem is not None:
                t_elem.text = translated
        break


def _apply_to_inline_string(tree: etree._Element, src: TextSource, translated: str) -> None:
    cell_ref = src.metadata.get("cell_ref", "")
    if not cell_ref:
        return
    for c in tree.iter(f"{{{_SHEET_NS}}}c"):
        if c.get("r", "") != cell_ref:
            continue
        is_elem = c.find(f"{{{_SHEET_NS}}}is")
        if is_elem is None:
            continue
        t_elem = is_elem.find(f"{{{_SHEET_NS}}}t")
        if t_elem is not None:
            t_elem.text = translated
        break


def _apply_to_comment(tree: etree._Element, src: TextSource, translated: str) -> None:
    ref = src.metadata.get("ref", "")
    if not ref:
        return
    for comment in tree.iter(f"{{{_SHEET_NS}}}comment"):
        if comment.get("ref", "") != ref:
            continue
        text_elem = comment.find(f".//{{{_SHEET_NS}}}t")
        if text_elem is not None:
            text_elem.text = translated
        break


def _apply_to_chart_or_shape(tree: etree._Element, src: TextSource, translated: str) -> None:
    """Match and replace chart/shape text by iterating ``<a:t>`` elements.
    Since chart/shape text sources are rare and usually unique, matching
    by original text is adequate.
    """
    original = src.original_text
    for t_elem in tree.iter(f"{{{_A_NS}}}t"):
        if t_elem.text == original:
            t_elem.text = translated
            return


class XlsxTranslator(DocumentTranslator):
    """Translator implementation for XLSX documents.

    Opens the XLSX as a ZIP, locates all text-bearing XML nodes,
    extracts plain text for LLM translation, then replaces only
    the text content in the original XML trees — preserving all
    formatting, formulas, images, and other metadata.
    """

    SUPPORTED_FORMATS = {DocumentFormat.XLSX, DocumentFormat.XLS}

    def __init__(self):
        self._temp_dir: Path | None = None
        self._converted_path: Path | None = None

    @classmethod
    def supported_formats(cls) -> set[DocumentFormat]:
        return cls.SUPPORTED_FORMATS.copy()

    def can_process(self, file_path: Path) -> bool:
        if not file_path.exists():
            return False
        suffix = file_path.suffix.lower()
        if suffix in (".xlsx", ".xlsm"):
            try:
                with zf.ZipFile(file_path, "r") as z:
                    names = z.namelist()
                    return any(n.startswith("xl/") for n in names)
            except Exception:
                return False
        if suffix == ".xls":
            return True
        return False

    def extract(self, file_path: Path,
                source_lang: str = "en",
                target_lang: str = "ru") -> dict[str, Any]:
        """Extract text units from XLSX.

        Uses :class:`XlsxContentClassifier` to identify all text-bearing
        content and classify it by type — only TEXT sources are exposed
        as translatable :class:`TextUnit` items; formulas, numbers, dates
        etc. are silently left untouched.

        Returns:
            dict with text_units, metadata, temp_dir, archive_data,
            classified_sources (for use by ``translate()``).
        """
        if not file_path.exists():
            raise DocumentOpenError(
                file_path=str(file_path),
                reason="XLSX file not found",
            )

        self._temp_dir = Path(tempfile.mkdtemp(prefix="xlsx_"))
        actual_path: Path = file_path

        file_size = actual_path.stat().st_size
        logger.info(f"XLSX file check: {actual_path} ({file_size} bytes)")

        try:
            if file_path.suffix.lower() == ".xls":
                from file_translator.infrastructure.converters.doc_to_docx_converter import (
                    LibreOfficeConverter,
                )
                converted = LibreOfficeConverter.convert(file_path, self._temp_dir)
                actual_path = converted
                self._converted_path = converted
                logger.info(f"Converted .xls to .xlsx: {converted}")

            with zf.ZipFile(str(actual_path), "r") as z:
                names = _find_xml_files(z)
                total_size = sum(z.getinfo(n).file_size for n in names)
                if total_size > _MAX_ARCHIVE_XML_SIZE:
                    raise DocumentOpenError(
                        file_path=str(actual_path),
                        reason=f"XLSX XML content too large ({total_size / 1024 / 1024:.1f} MB; limit {_MAX_ARCHIVE_XML_SIZE / 1024 / 1024:.0f} MB)",
                    )
                archive_data: dict[str, bytes] = {name: z.read(name) for name in names}
        except DocumentOpenError:
            raise
        except Exception as e:
            raise DocumentOpenError(
                file_path=str(actual_path),
                reason=f"Failed to read XLSX archive: {e}",
            )

        classifier = XlsxContentClassifier()
        classified_sources = classifier.classify(archive_data)

        text_units: list[TextUnit] = []
        skipped_count = 0
        for src in classified_sources:
            if src.category == Category.TEXT and src.should_translate:
                text_units.append(TextUnit(
                    id=src.id,
                    original_text=src.original_text,
                    path=src.path,
                    metadata={
                        **src.metadata,
                        "source_type": src.source_type.value,
                        "category": src.category.value,
                    },
                ))
            else:
                skipped_count += 1

        if skipped_count:
            logger.info(
                "Content classifier skipped %d non-text sources "
                "(formulas, numbers, errors, …)",
                skipped_count,
            )

        metadata = DocumentMetadata(
            has_tables=True,
            page_count=len(text_units),
        )

        logger.info(
            "XLSX extraction complete: %d text units from %s",
            len(text_units), actual_path.name,
        )

        try:
            return {
                "text_units": text_units,
                "metadata": metadata,
                "temp_dir": str(self._temp_dir),
                "archive_data": archive_data,
                "document_path": str(actual_path),
                "classified_sources": classified_sources,
            }
        except Exception:
            self._cleanup()
            raise

    def translate(self, extracted_data: dict[str, Any],
                  translations: dict[str, str],
                  supports_tags: bool = False) -> dict[str, Any]:
        """Apply translations to the XLSX XML trees.

        Uses the ``classified_sources`` produced by :meth:`extract` for
        precise element location (by index, cell ref, or comment ref).
        For rich text (``<r>`` runs) the translated text is distributed
        proportionally across runs to preserve formatting.
        """
        archive_data = extracted_data.get("archive_data", {})
        classified_sources: list[TextSource] = extracted_data.get("classified_sources", [])

        _translate_sources_by_path(
            archive_data, classified_sources, translations,
        )

        extracted_data["translations_applied"] = len(translations)
        return extracted_data

    def save(self, translated_data: dict[str, Any], output_path: Path) -> Path:
        """Save translated XLSX by writing back modified XML files.

        Copies the original XLSX, then replaces modified XML entries
        in-place. Unchanged files are preserved as-is.

        Returns:
            Path to the saved output file.
        """
        archive_data = translated_data.get("archive_data", {})
        document_path = Path(translated_data.get("document_path", ""))

        if not document_path.exists():
            raise SaveDocumentError(
                output_path=str(output_path),
                reason=f"Original document not found: {document_path}",
            )

        try:
            shutil.copy2(str(document_path), str(output_path))
        except Exception as e:
            raise SaveDocumentError(
                output_path=str(output_path),
                reason=f"Failed to copy original: {e}",
            )

        try:
            with zf.ZipFile(str(output_path), "r") as zr:
                original_names = zr.namelist()

            with zf.ZipFile(str(output_path), "r") as zr:
                with zf.ZipFile(str(output_path) + ".tmp", "w", zf.ZIP_DEFLATED) as zw:
                    for name in original_names:
                        if name in archive_data:
                            zw.writestr(name, archive_data[name])
                        else:
                            zw.writestr(name, zr.read(name))

            os.replace(str(output_path) + ".tmp", str(output_path))
            logger.info(f"XLSX saved: {output_path}")
        except Exception as e:
            tmp = Path(str(output_path) + ".tmp")
            if tmp.exists():
                tmp.unlink()
            raise SaveDocumentError(
                output_path=str(output_path),
                reason=f"Failed to write XLSX archive: {e}",
            )
        finally:
            self._cleanup()

        return output_path

    # ── Private helpers ──

    def _cleanup(self):
        """Clean up temporary files. Never raises."""
        try:
            if self._temp_dir and self._temp_dir.exists():
                shutil.rmtree(str(self._temp_dir))
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")
        self._temp_dir = None
