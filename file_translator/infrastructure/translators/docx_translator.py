"""DOCX document translator — thin orchestration around Okapi Framework.

This module delegates all DOCX XML manipulation to Okapi Tikal CLI:
  extract()  → Tikal extraction → XLIFF → domain TextUnit objects
  translate() → update XLIFF <target> nodes
  save()     → Tikal merge → DOCX

No direct work with <w:t>, <w:r>, <w:p> or any WordprocessingML.
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
from file_translator.infrastructure.translators.okapi_service import (
    OkapiService,
    OkapiServiceError,
    TikalNotAvailableError,
)

logger = logging.getLogger(__name__)


class DocxTranslator(DocumentTranslator):
    """Translator implementation for DOCX documents using Okapi Framework.

    Orchestrates the Okapi Tikal CLI pipeline:
      extract()  → tikal -x -> XLIFF -> TextUnit list
      translate() → updates XLIFF <target> with LLM output
      save()     → tikal -m -> translated DOCX

    Document conversion (.doc → .docx) is handled by LibreOffice
    before Okapi extraction.
    """

    SUPPORTED_FORMATS = {DocumentFormat.DOCX, DocumentFormat.DOC}

    def __init__(self, okapi_service: OkapiService | None = None):
        self._temp_dir: Path | None = None
        self._okapi = okapi_service or OkapiService()

    @classmethod
    def supported_formats(cls) -> set[DocumentFormat]:
        return cls.SUPPORTED_FORMATS.copy()

    def can_process(self, file_path: Path) -> bool:
        if not file_path.exists():
            return False
        suffix = file_path.suffix.lower()
        if suffix == '.docx':
            try:
                with zf.ZipFile(file_path, 'r') as zip_file:
                    return any(
                        name.startswith('word/') and name.endswith('.xml')
                        for name in zip_file.namelist()
                    )
            except Exception:
                return False
        if suffix == '.doc':
            return True
        return False

    def extract(self, file_path: Path,
                source_lang: str = "en",
                target_lang: str = "ru") -> dict:
        """Extract text units via Okapi XLIFF pipeline.

        1. If .doc, convert to .docx via LibreOffice.
        2. Create temp working directory.
        3. Run Tikal extraction: tikal -x input.docx → input.docx.xlf
        4. Parse XLIFF into TextUnit objects (one per trans-unit).
        5. Return dict with text_units, metadata, temp_dir, xliff_path.

        Raises:
            DocumentOpenError: If the file cannot be opened.
            DocumentParseError: If extraction fails.
        """
        logger.info(f"Extracting text via Okapi: {file_path}")

        self._cleanup()
        self._temp_dir = Path(tempfile.mkdtemp(prefix="docx_okapi_"))
        self._document_path = file_path

        try:
            actual_path: Path = file_path

            if file_path.suffix.lower() == '.doc':
                from file_translator.infrastructure.converters.doc_to_docx_converter import (
                    LibreOfficeConverter,
                )
                converted = LibreOfficeConverter.convert(file_path, self._temp_dir)
                actual_path = converted
                logger.info(f"Converted .doc to .docx: {converted}")

            # Run Tikal extraction
            xliff_path = self._okapi.extract_to_xliff(
                actual_path,
                self._temp_dir,
                source_lang=source_lang,
                target_lang=target_lang,
            )

            # Parse XLIFF into domain TextUnits
            xliff_units = self._okapi.load_xliff(xliff_path)

            text_units: list[TextUnit] = []
            for xu in xliff_units:
                if not xu.source_text.strip():
                    continue
                unit = TextUnit(
                    id=xu.id,
                    original_text=xu.source_text,
                    path=xu.id,
                    metadata={
                        "translate": xu.translate,
                        "source_xml": xu.source_xml,
                    },
                )
                text_units.append(unit)

            metadata = DocumentMetadata(
                has_tables=True,
                has_headers=True,
                has_footers=True,
            )

            logger.info(
                f"Okapi extraction complete: {len(text_units)} units "
                f"from {xliff_path.name}"
            )

            return {
                "text_units": text_units,
                "metadata": metadata,
                "temp_dir": str(self._temp_dir),
                "document_path": str(actual_path),
                "xliff_path": str(xliff_path),
            }

        except (TikalNotAvailableError, OkapiServiceError, Exception) as e:
            logger.error(f"Okapi extraction failed: {e}")
            self._cleanup()
            if isinstance(e, TikalNotAvailableError):
                raise DocumentParseError(
                    details=f"Okapi Tikal required: {e}"
                ) from e
            raise DocumentParseError(details=str(e)) from e

    def translate(self, extracted_data: dict, translations: dict[str, str],
                  supports_tags: bool = False) -> dict:
        """Apply translations to XLIFF <target> nodes.

        Writes translated text into the XLIFF file's <target> elements.
        Never modifies inline codes, skeleton metadata, or file attributes.
        The 'supports_tags' parameter is accepted for backward compatibility
        with the interface but is ignored — Okapi manages inline codes.

        Args:
            extracted_data: Data from extract() containing xliff_path.
            translations: {unit_id: translated_text} mapping.

        Returns:
            Updated extracted_data dict.
        """
        xliff_path = Path(extracted_data["xliff_path"])
        logger.info(
            f"Applying {len(translations)} translations to XLIFF: {xliff_path.name}"
        )

        try:
            self._okapi.save_xliff(xliff_path, translations)
        except OkapiServiceError as e:
            logger.error(f"Failed to update XLIFF: {e}")
            extracted_data.setdefault("errors", []).append(str(e))

        extracted_data["translations_applied"] = len(translations)
        return extracted_data

    def save(self, translated_data: dict, output_path: Path) -> None:
        """Save translated document by merging XLIFF back to DOCX.

        Calls tikal -m to merge the translated XLIFF (with updated <target>
        nodes) back to DOCX format, then post-processes the DOCX to ensure
        CJK fonts are correctly set.

        Args:
            translated_data: Data from translate() containing xliff_path.
            output_path: Destination path for the translated DOCX.

        Raises:
            SaveDocumentError: If merge or file copy fails.
        """
        xliff_path = Path(translated_data["xliff_path"])
        logger.info(
            f"Merging XLIFF to DOCX: {xliff_path.name} → {output_path.name}"
        )

        try:
            self._okapi.merge_from_xliff(
                xliff_path, output_path,
                original_path=getattr(self, "_document_path", None),
            )
            self._post_process_docx(output_path)
            logger.info(f"Document saved: {output_path}")
        except TikalNotAvailableError as e:
            raise SaveDocumentError(
                output_path=str(output_path),
                reason=f"Okapi Tikal required: {e}",
            ) from e
        except OkapiServiceError as e:
            raise SaveDocumentError(
                output_path=str(output_path),
                reason=str(e),
            ) from e
        except Exception as e:
            raise SaveDocumentError(
                output_path=str(output_path),
                reason=str(e),
            ) from e
        finally:
            self._cleanup()

    @staticmethod
    def _post_process_docx(docx_path: Path) -> None:
        """Apply all post-processing fixes to a merged DOCX archive in one pass.

        Combines two fixes that were previously applied separately (and thus
        required reading/writing the ZIP archive twice):

        1. **CJK font fix**: Replace SimSun and other CJK fonts with Arial in
           XML files. Tikal's merge injects w:eastAsia="SimSun" for translated
           CJK text; this ensures consistent rendering.

        2. **Table row height fix**: Change w:hRule="exact" to "atLeast" in all
           table rows. Translated text can expand and overflow fixed-height rows,
           hiding the content. Using "atLeast" allows rows to grow as needed.

        Both fixes use regex matching on XML files only (and .rels). The archive
        is read once, modified files updated in memory, then written back
        atomically via temp file + os.replace.

        Modifies the file in-place.
        """
        if not docx_path.exists():
            return

        # CJK font replacement patterns (10 fonts)
        _CJK_FONTS = [
            "SimSun", "SimHei", "MingLiU", "MS Mincho", "MS Gothic",
            "DengXian", "FangSong", "KaiTi", "NSimSun", "PMingLiU",
        ]
        _font_alts = "|".join(re.escape(f) for f in _CJK_FONTS)
        _CJK_ATTR_RE = re.compile(
            rb'([\w.-]+:(?:eastAsia|ascii|hAnsi))\s*=\s*"(('
            + _font_alts.encode()
            + rb'))"'
        )

        # Table row height fix pattern (exact → atLeast)
        _HRULE_RE = re.compile(rb'([\w.-]+):hRule\s*=\s*"exact"')

        try:
            with zf.ZipFile(str(docx_path), "r") as z:
                archive = {name: z.read(name) for name in z.namelist()}
        except Exception as e:
            logger.warning(f"DOCX post-process: failed to read archive: {e}")
            return

        cjk_total = 0
        hrule_total = 0
        modified_files = 0

        for name, data in archive.items():
            # Only process XML and .rels files
            low = name.lower()
            if not (low.endswith(".xml") or low.endswith(".rels")):
                continue

            new_data = data
            file_changes = False

            # Apply CJK font fix
            new_data, cjk_count = _CJK_ATTR_RE.subn(
                lambda m: m.group(0).replace(m.group(2), b"Arial"),
                new_data,
            )
            if cjk_count:
                file_changes = True
                cjk_total += cjk_count

            # Apply table height fix
            new_data, hrule_count = _HRULE_RE.subn(
                lambda m: m.group(0).replace(b'"exact"', b'"atLeast"'),
                new_data,
            )
            if hrule_count:
                file_changes = True
                hrule_total += hrule_count

            if file_changes:
                archive[name] = new_data
                modified_files += 1
                logger.debug(
                    f"  {name}: CJK={cjk_count}, height={hrule_count} fix(es)"
                )

        # Write back atomically only if changes were made
        total_changes = cjk_total + hrule_total
        if total_changes:
            tmp = docx_path.with_suffix(docx_path.suffix + ".tmp")
            try:
                with zf.ZipFile(str(tmp), "w", zf.ZIP_DEFLATED) as z:
                    for name, data in archive.items():
                        z.writestr(name, data)
                os.replace(str(tmp), str(docx_path))
                logger.info(
                    f"DOCX post-process: {docx_path.name} "
                    f"(CJK={cjk_total}, height={hrule_total}, files={modified_files})"
                )
            except Exception as e:
                logger.warning(f"DOCX post-process: failed to write archive: {e}")
                if tmp.exists():
                    tmp.unlink()
        else:
            logger.debug(f"No changes needed for {docx_path.name}")


    def _cleanup(self):
        """Clean up temporary files. Never raises."""
        try:
            if self._temp_dir and self._temp_dir.exists():
                shutil.rmtree(str(self._temp_dir))
                logger.debug(f"Cleaned up temp directory: {self._temp_dir}")
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")
        self._temp_dir = None
        self._document_path = None
