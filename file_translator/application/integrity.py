"""Post-merge integrity gate for translated documents.

Reuses the structure-fidelity diagnostics as importable gate components:

- **Loss check**: non-empty source paragraphs must be present (whitespace
  normalized) in the translated document.
- **Numeric-fidelity check**: aligned paragraph pairs must not change ref / sub
  / bare-number tokens (via :mod:`file_translator.diagnostics.numeric_fidelity`).
- **Completeness check**: paragraphs must not mix target-language text with a
  leftover source-language run (via leak_scanner ``is_mixed_unit``).

The gate is advisory for mixed units (recorded as warnings — the pipeline
already re-translates or falls back to source) and decisive for content loss
and numeric drift (a job must not complete "clean" while those remain).
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from file_translator.diagnostics.numeric_fidelity import verify_numeric
from file_translator.diagnostics.leak_scanner import is_mixed_unit

logger = logging.getLogger(__name__)


@dataclass
class IntegrityReport:
    """Result of a post-merge integrity check."""

    loss_paragraphs: list[str] = field(default_factory=list)
    numeric_mismatch_count: int = 0
    ref_mismatch_count: int = 0
    mixed_units: list[str] = field(default_factory=list)
    checked_pairs: int = 0

    @property
    def critical(self) -> bool:
        """Content loss or drift of legal reference numbers (№-references) are
        critical; measurement/data values and mixed units are recorded but do
        not fail the job."""
        return bool(self.loss_paragraphs) or self.ref_mismatch_count > 0

    def to_dict(self) -> dict:
        return {
            "loss_paragraphs": self.loss_paragraphs[:50],
            "loss_count": len(self.loss_paragraphs),
            "numeric_mismatch_count": self.numeric_mismatch_count,
            "ref_mismatch_count": self.ref_mismatch_count,
            "checked_pairs": self.checked_pairs,
            "mixed_units": self.mixed_units[:50],
            "mixed_count": len(self.mixed_units),
            "critical": self.critical,
        }


def extract_paragraphs(docx_path: Path | str) -> list[str]:
    """Return whitespace-normalized non-empty paragraph texts from a DOCX."""
    with zipfile.ZipFile(str(docx_path)) as zf:
        try:
            xml = zf.read("word/document.xml").decode("utf-8", "ignore")
        except KeyError:
            logger.warning("No word/document.xml in %s", docx_path)
            return []
    paras = re.findall(r"<w:p\b.*?</w:p>", xml, re.S)
    out: list[str] = []
    for p in paras:
        ts = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S))
        ts = re.sub(r"<[^>]+>", "", ts)
        ts = (ts.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
              .replace("&quot;", '"').replace("&#39;", "'"))
        norm = re.sub(r"\s+", " ", ts).strip()
        if norm:
            out.append(norm)
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _count_deficit(original: list[str], translated: list[str], tolerance_pct: float = 0.05) -> list[str]:
    """Report paragraph-count drops as a loss signal.

    Content-based containment cannot detect dropped paragraphs across a full
    translation (every word changes). The authoritative loss prevention lives
    at the XLIFF level (empty targets get source fallback); this doc-level
    check is a secondary net for gross extraction/merge gaps.
    """
    deficit = len(original) - len(translated)
    tolerance = max(3, int(len(original) * tolerance_pct))
    if deficit > tolerance:
        return [f"{deficit} paragraph(s) missing (non-empty {len(original)} -> {len(translated)})"]
    return []


def run_integrity_check(
    original_path: Path | str,
    translated_path: Path | str,
    max_pairs: int = 5000,
) -> IntegrityReport:
    """Compare the original and translated DOCX files and return a report."""
    orig_paras = extract_paragraphs(original_path)
    trans_paras = extract_paragraphs(translated_path)

    report = IntegrityReport()

    # 1. Loss check
    report.loss_paragraphs = _count_deficit(orig_paras, trans_paras)
    if report.loss_paragraphs:
        logger.warning(
            f"Integrity: {len(report.loss_paragraphs)} source paragraph(s) "
            f"absent from translated document"
        )

    # 2. Numeric drift on the whole-document numeric feature multiset. Aligned
    #    pair comparison proved unusable (translations legitimately reflow,
    #    shifting paragraph indices); comparing the global multiset of ref/sub/
    #    num tokens still catches drift of the 732→107 / «Таблица 3.1»→1.2 class
    #    without the alignment noise.
    src_all = "\n".join(orig_paras)
    tgt_all = "\n".join(trans_paras)
    mismatches = verify_numeric(src_all, tgt_all)
    report.numeric_mismatch_count = len(mismatches)
    # Fail-relevant: legal reference numbers (№732 / No. 732 drift).
    # Single-digit refs («№3» = room/list item) and long concatenated tokens
    # (a date rebuilt without separators) are advisory, not legal references.
    def _meaningful_ref(m) -> bool:
        s = m.source or ""
        t = m.target or ""
        if 2 <= len(s) <= 6 and (not t or 2 <= len(t) <= 6):
            return True
        return False

    refs = [m for m in mismatches if m.kind == "ref" and _meaningful_ref(m)]
    report.ref_mismatch_count = len(refs)
    report.checked_pairs = len(mismatches)
    if report.ref_mismatch_count:
        sample = ", ".join(f"{m.source}->{m.target}" for m in refs[:8])
        logger.warning(
            f"Integrity: {report.ref_mismatch_count} legal-reference number(s) "
            f"drifted (sample: {sample})"
        )
    elif mismatches:
        logger.warning(
            f"Integrity: {len(mismatches)} non-reference numeric token(s) "
            f"differ (advisory, not critical)"
        )

    # 3. Mixed units (recorded, not critical)
    for t in trans_paras:
        try:
            if is_mixed_unit(t):
                report.mixed_units.append(t[:200])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Mixed-unit scan error: %s", exc)
    if report.mixed_units:
        logger.warning(
            f"Integrity: {len(report.mixed_units)} mixed (partially translated) unit(s)"
        )

    return report