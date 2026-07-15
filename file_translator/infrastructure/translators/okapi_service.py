"""Service layer wrapping Okapi Tikal CLI for XLIFF extraction and merge.

Uses subprocess calls to tikal (Java JAR) to:
- Extract DOCX -> XLIFF
- Merge XLIFF -> DOCX
Provides XLIFF parsing and manipulation via xml.etree.
"""

from __future__ import annotations

import contextlib
import copy
import html
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from file_translator.infrastructure.config import TIKAL_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# XLIFF 1.2 namespace
NS_XLIFF = "urn:oasis:names:tc:xliff:document:1.2"


@dataclass
class XliffUnit:
    """Represents a single trans-unit from an XLIFF document.

    Maps directly to XLIFF trans-unit elements.
    Inline codes (<g>, <x>, <bx>, <ex>) are tracked separately
    from the plain text content for clean LLM consumption.
    """

    id: str
    source_text: str
    target_text: str = ""
    translate: bool = True
    source_xml: str = ""  # Raw <source> inner XML (with inline codes)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def plain_text(self) -> str:
        return self.source_text

    @property
    def needs_translation(self) -> bool:
        return self.translate and bool(self.source_text.strip()) and not self.target_text


class OkapiServiceError(Exception):
    """Raised when an Okapi operation fails."""


class TikalNotAvailableError(OkapiServiceError):
    """Raised when the tikal CLI cannot be found or invoked."""


def _sanitize_filename(name: str) -> str:
    """Replace non-ASCII characters with underscores for Tikal compatibility.

    Tikal (Java) fails on paths containing Cyrillic or other non-ASCII
    characters. This ensures temp work filenames are always ASCII-safe.
    """
    return "".join(ch if ch.isascii() else "_" for ch in name)


@contextlib.contextmanager
def _temp_work_dir(prefix: str = "tikal_"):
    """Context manager: creates a temp directory, yields it, cleans up on exit.

    Cleanup happens in the ``finally`` block, so the directory is removed
    even if an exception occurs. Exceptions during cleanup are logged but
    not re-raised (does not mask the original error).
    """
    temp_dir = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield temp_dir
    finally:
        try:
            shutil.rmtree(str(temp_dir))
        except Exception:
            logger.exception(f"Temp cleanup failed for {temp_dir}")


class OkapiService:
    """Wraps the Okapi Tikal CLI for XLIFF extraction and merge.

    Requires Java 8+ and the Okapi Tikal distribution available at
    the path specified by TIKAL_HOME env var or tikal on PATH.

    Typical workflow:
        svc = OkapiService()
        xlf_path = svc.extract_to_xliff("input.docx", "/tmp/work")
        units = svc.load_xliff(xlf_path)
        # ... translate units via LLM ...
        svc.save_xliff(xlf_path, {u.id: translated_text for u in units})
        result = svc.merge_from_xliff(xlf_path, "/tmp/output.docx")
    """

    def __init__(self, tikal_home: str | None = None):
        self._tikal_home: str | None = tikal_home
        self._tikal_cmd: list[str] | None = None
        self._tikal_lock = threading.Lock()

    @property
    def tikal_cmd(self) -> list[str]:
        if self._tikal_cmd is not None:
            return self._tikal_cmd

        with self._tikal_lock:
            if self._tikal_cmd is not None:
                return self._tikal_cmd
            tikal_home = self._tikal_home
            if not tikal_home:
                tikal_home = self._resolve_tikal_home()

            if tikal_home:
                if platform.system() == "Windows":
                    cmd = [str(Path(tikal_home) / "tikal.bat")]
                else:
                    cmd = [str(Path(tikal_home) / "tikal.sh")]
                self._tikal_cmd = cmd
            else:
                self._tikal_cmd = ["tikal"]

            return self._tikal_cmd

    def _resolve_tikal_home(self) -> str | None:
        """Resolve TIKAL_HOME from environment variables."""
        for var in ("TIKAL_HOME", "OKAPI_HOME"):
            val = os.environ.get(var)
            if val:
                return val
        return None

    def check_available(self) -> bool:
        """Check if Tikal CLI is available."""
        try:
            result = subprocess.run(
                self.tikal_cmd + ["-?"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0 or "tikal" in (result.stdout + result.stderr).lower()
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            return False

    def extract_to_xliff(
        self,
        input_path: Path,
        output_dir: Path,
        source_lang: str = "en",
        target_lang: str = "ru",
    ) -> Path:
        """Run tikal -x to extract translatable content to XLIFF.

        Args:
            input_path: Path to the source DOCX file.
            output_dir: Directory for working files (XLIFF + skeleton).
            source_lang: Source language code (e.g. 'en', 'zh').
            target_lang: Target language code (e.g. 'ru').

        Returns:
            Path to the generated .xlf file.
        """
        if not input_path.exists():
            raise OkapiServiceError(f"Input file not found: {input_path}")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Tikal creates .xlf in same dir as input; copy to output_dir first
        work_input = output_dir / _sanitize_filename(input_path.name)
        if work_input.resolve() != input_path.resolve():
            shutil.copy2(str(input_path), str(work_input))
        else:
            logger.debug(f"Input already in output dir, skipping copy: {input_path}")

        cmd = self.tikal_cmd + [
            "-x", str(work_input),
            "-sl", source_lang,
            "-tl", target_lang,
        ]

        logger.info(f"Running Tikal extract: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=TIKAL_TIMEOUT_SECONDS,
                cwd=str(output_dir),
            )
        except FileNotFoundError as e:
            raise TikalNotAvailableError(
                f"Tikal CLI not found. Install Okapi Tikal or set TIKAL_HOME. "
                f"Command tried: {' '.join(self.tikal_cmd)}"
            ) from e
        except subprocess.TimeoutExpired:
            raise OkapiServiceError(f"Tikal extraction timed out ({TIKAL_TIMEOUT_SECONDS}s)")

        if result.returncode != 0:
            logger.error(f"Tikal extract stderr: {result.stderr}")
            raise OkapiServiceError(
                f"Tikal extract failed (code {result.returncode}): {result.stderr[:500]}"
            )

        # Tikal generates <input>.xlf in the same dir as input
        xlf_path = work_input.with_suffix(f"{work_input.suffix}.xlf")
        if not xlf_path.exists():
            # Also check for .xlf without double extension
            alt = work_input.with_suffix(".xlf")
            if alt.exists():
                xlf_path = alt
            else:
                raise OkapiServiceError(
                    f"XLIFF file not found after extraction. "
                    f"Tried: {xlf_path}, {alt}"
                )

        logger.info(f"XLIFF extracted: {xlf_path}")
        return xlf_path

    def load_xliff(self, xliff_path: Path) -> list[XliffUnit]:
        """Parse an XLIFF 1.2 document and extract all trans-units.

        Strips inline codes (<g>, <x>, <bx>, <ex>) from source text
        for clean LLM consumption. The raw source XML is preserved
        in XliffUnit.source_xml for later reconstruction.

        Args:
            xliff_path: Path to the .xlf file.

        Returns:
            List of XliffUnit objects.
        """
        if not xliff_path.exists():
            raise OkapiServiceError(f"XLIFF file not found: {xliff_path}")

        tree = ET.parse(str(xliff_path))
        root = tree.getroot()

        units: list[XliffUnit] = []

        for trans_unit in root.iter(f"{{{NS_XLIFF}}}trans-unit"):
            unit_id = trans_unit.get("id", "")
            if not unit_id:
                continue

            translate_attr = trans_unit.get("translate", "yes")
            translate = translate_attr.lower() != "no"

            source_elem = trans_unit.find(f"{{{NS_XLIFF}}}source")
            if source_elem is None:
                continue

            # Build plain text: strip all inline codes, keep only text
            source_text = self._get_plain_text(source_elem)

            # Preserve raw XML for inline code reconstruction
            source_xml = "".join(
                ET.tostring(child, encoding="unicode")
                for child in source_elem
            ).strip()

            target_elem = trans_unit.find(f"{{{NS_XLIFF}}}target")
            target_text = ""
            if target_elem is not None and target_elem.text:
                target_text = target_elem.text.strip()

            unit = XliffUnit(
                id=unit_id,
                source_text=source_text,
                target_text=target_text,
                translate=translate,
                source_xml=source_xml,
            )
            units.append(unit)

        logger.info(f"Loaded {len(units)} units from {xliff_path.name}")
        return units

    @staticmethod
    def _get_plain_text(element: ET.Element) -> str:
        """Extract plain text from an XLIFF element, stripping inline codes.

        Inline code tags (<g>, <x/>, <bx/>, <ex/>, etc.) are removed,
        replaced with a space to prevent adjacent words from merging.
        """
        return OkapiService._simple_plain_text(element)

    @staticmethod
    def _simple_plain_text(element: ET.Element) -> str:
        """Extract plain text, stripping XML tags and Tikal inline code artifacts."""
        xml_str = ET.tostring(element, encoding="unicode")
        text = re.sub(r"<[^>]+>", " ", xml_str)
        text = html.unescape(text)
        text = re.sub(r"</?run\d+\s*/?>", "", text)
        text = re.sub(r"</?tags?\d*\s*/?>", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def save_xliff(
        self,
        xliff_path: Path,
        translations: dict[str, str],
        output_path: Path | None = None,
    ) -> Path:
        """Update <target> elements in an XLIFF document with translations.

        Never modifies:
        - Inline codes (<g>, <x>, <bx>, <ex>)
        - Skeleton metadata (<skl>, <external-file>)
        - okp:* attributes
        - file metadata (original, source-language, target-language)
        - trans-unit attributes (id, translate)

        Inline code structure is preserved from <source> into <target>,
        with translated text distributed proportionally among text/tail nodes.

        Args:
            xliff_path: Path to the original XLIFF file.
            translations: Mapping of {unit_id: translated_text}.
            output_path: Optional output path. If None, overwrites input.

        Returns:
            Path to the saved XLIFF file.
        """
        tree = ET.parse(str(xliff_path))
        root = tree.getroot()

        updated_count = 0
        skipped_count = 0

        for trans_unit in root.iter(f"{{{NS_XLIFF}}}trans-unit"):
            unit_id = trans_unit.get("id", "")
            if unit_id not in translations:
                continue

            translated = translations[unit_id]
            if not translated.strip():
                skipped_count += 1
                continue

            target_elem = trans_unit.find(f"{{{NS_XLIFF}}}target")
            if target_elem is None:
                source_elem = trans_unit.find(f"{{{NS_XLIFF}}}source")
                if source_elem is None:
                    continue
                idx = list(trans_unit).index(source_elem)
                target_elem = ET.SubElement(trans_unit, f"{{{NS_XLIFF}}}target")
                trans_unit.remove(target_elem)
                trans_unit.insert(idx + 1, target_elem)

            source_elem = trans_unit.find(f"{{{NS_XLIFF}}}source")
            if source_elem is not None:
                self._set_target_with_inline_codes(source_elem, target_elem, translated)
            else:
                for child in list(target_elem):
                    target_elem.remove(child)
                target_elem.text = translated
            updated_count += 1

        if updated_count == 0:
            logger.warning(f"No translations applied to XLIFF (0/{len(translations)} units matched)")

        save_path = output_path or xliff_path
        tree.write(str(save_path), xml_declaration=True, encoding="UTF-8")

        logger.info(
            f"XLIFF saved: {save_path.name} "
            f"({updated_count} updated, {skipped_count} skipped)"
        )
        return save_path

    @staticmethod
    def _set_target_with_inline_codes(
        source_elem: ET.Element,
        target_elem: ET.Element,
        translated_text: str,
    ) -> None:
        """Replace <target> content preserving inline codes from <source>.

        Deep-copies <source> children (inline codes) into <target>, then
        distributes the translated text among all text and tail nodes using
        a water-fill algorithm: each position gets floor(proportion) words,
        and remaining words are assigned one-by-one to positions with the
        largest fractional remainder.

        Positions that receive 0 words keep their original separator text,
        preserving whitespace boundaries between inline codes.
        """
        for child in list(target_elem):
            target_elem.remove(child)
        target_elem.text = None

        for child in source_elem:
            target_elem.append(copy.deepcopy(child))

        # Collect text/tail positions with original lengths
        positions: list[tuple[ET.Element, bool, str]] = []

        if source_elem.text is not None:
            target_elem.text = source_elem.text
            positions.append((target_elem, False, source_elem.text))

        for src_child, tgt_child in zip(source_elem, target_elem):
            if src_child.text is not None:
                tgt_child.text = src_child.text
                positions.append((tgt_child, False, src_child.text))
            if src_child.tail is not None:
                tgt_child.tail = src_child.tail
                positions.append((tgt_child, True, src_child.tail))

        if not positions:
            target_elem.text = translated_text
            return

        # Split positions into content (non-whitespace) and whitespace-only
        # Whitespace-only positions keep their original whitespace and
        # don't consume words — this prevents triple-space artifacts
        # when LLM returns fewer words than there are runs.
        content_indices = [
            i for i, (_, _, t) in enumerate(positions)
            if t.strip()
        ]
        is_ws = [i not in content_indices for i in range(len(positions))]

        if not content_indices:
            target_elem.text = (target_elem.text or "") + translated_text
            return

        content_lens = [len(positions[i][2]) for i in content_indices]
        total_len = sum(content_lens)
        if total_len == 0:
            target_elem.text = (target_elem.text or "") + translated_text
            return

        words = translated_text.split()
        total_words = len(words)

        # Water-fill only on content positions
        raw_counts = {}
        remainders = {}
        for idx, length in zip(content_indices, content_lens):
            exact = length / total_len * total_words
            base = int(exact)
            raw_counts[idx] = base
            remainders[idx] = exact - base

        remaining = total_words - sum(raw_counts.values())
        if remaining > 0:
            for idx in sorted(content_indices, key=lambda i: -remainders[i]):
                if remaining <= 0:
                    break
                raw_counts[idx] = raw_counts.get(idx, 0) + 1
                remaining -= 1

        counts = [raw_counts.get(i, 0) for i in range(len(positions))]

        word_idx = 0
        for i, (elem, is_tail, orig_text) in enumerate(positions):
            n = counts[i]
            if is_ws[i]:
                # Whitespace-only — preserve original, consume no words
                if n > 0:
                    # Shouldn't happen, but be safe
                    word_idx += n
                continue

            chunk = " ".join(words[word_idx:word_idx + n]) if n > 0 else ""
            word_idx += n

            if is_tail:
                leading_match = re.match(r'^(\s*)', orig_text)
                leading = leading_match.group(1) if leading_match else ""
                elem.tail = leading + chunk
            else:
                trailing_match = re.search(r'(\s*)$', orig_text)
                trailing = trailing_match.group(1) if trailing_match else ""
                elem.text = chunk + trailing

    def merge_from_xliff(
        self,
        xliff_path: Path,
        output_path: Path | None = None,
        original_path: Path | None = None,
    ) -> Path:
        """Run tikal -m to merge XLIFF back to DOCX.

        Each merge runs in an isolated temp directory to guarantee
        deterministic output file discovery — no glob ambiguity when
        multiple merges run concurrently.

        Args:
            xliff_path: Path to the translated XLIFF file.
            output_path: Desired output path. If None, auto-generated.
            original_path: Path to the original source document. If provided,
                copied alongside the XLIFF so Tikal can find it during merge.

        Returns:
            Path to the generated DOCX file.
        """
        if not xliff_path.exists():
            raise OkapiServiceError(f"XLIFF file not found for merge: {xliff_path}")

        with _temp_work_dir(prefix="tikal_merge_") as merge_dir:
            # Copy XLIFF into isolated directory (Tikal creates output alongside input)
            isolated_xliff = merge_dir / _sanitize_filename(xliff_path.name)
            shutil.copy2(str(xliff_path), str(isolated_xliff))

            # Copy original document alongside the XLIFF — Tikal needs it during merge
            if original_path and original_path.exists():
                orig_copy = merge_dir / _sanitize_filename(original_path.name)
                shutil.copy2(str(original_path), str(orig_copy))
                logger.debug(f"Original file copied for merge: {orig_copy.name}")

            cmd = self.tikal_cmd + [
                "-m", str(isolated_xliff),
            ]

            logger.info(f"Running Tikal merge (isolated dir): {' '.join(cmd)}")
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=TIKAL_TIMEOUT_SECONDS,
                    cwd=str(merge_dir),
                )
            except FileNotFoundError as e:
                raise TikalNotAvailableError(
                    f"Tikal CLI not found. Command tried: {' '.join(self.tikal_cmd)}"
                ) from e
            except subprocess.TimeoutExpired:
                raise OkapiServiceError(f"Tikal merge timed out ({TIKAL_TIMEOUT_SECONDS}s)")

            if result.returncode != 0:
                logger.error(f"Tikal merge stderr: {result.stderr}")
                raise OkapiServiceError(
                    f"Tikal merge failed (code {result.returncode}): {result.stderr[:500]}"
                )

            # Deterministic discovery: Tikal created exactly one *.out.* file
            out_candidates = sorted(
                merge_dir.glob("*.out.*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not out_candidates:
                raise OkapiServiceError(
                    f"Merged file not found in isolated directory {merge_dir}. "
                    f"Tikal stdout: {result.stdout[:500]}"
                )
            out_path = out_candidates[0]

            if output_path:
                shutil.copy2(str(out_path), str(output_path))
                logger.info(f"DOCX merged and copied to: {output_path}")
                return output_path

            logger.info(f"DOCX merged: {out_path}")
            return out_path

    def cleanup(self, *paths: Path) -> None:
        """Remove temporary files created during processing."""
        for p in paths:
            try:
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(str(p))
            except Exception as e:
                logger.warning(f"Cleanup failed for {p}: {e}")
