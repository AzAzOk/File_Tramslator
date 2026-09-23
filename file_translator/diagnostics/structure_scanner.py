"""Structure invariant scanner — compares a translated DOCX against its
original and reports deltas for structural invariants.

Topological invariants (paragraphs, tables, table rows, embedded media) must
hold for every origin (native DOCX, DOC-via-LibreOffice, PDF-via-First-PDF).
Count-only invariants (text-element nodes, tab stops) legitimately change for
converted inputs, so those are reported as warnings with ``source_origin``
context, not hard failures.

Renderer page counts are supplied separately by the render loop
(:mod:`file_translator.diagnostics.render`) and merged by the CLI.
"""

from __future__ import annotations

import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Invariants that must match regardless of input origin.
TOPOLOGICAL_INVARIANTS = ("paragraphs", "tables", "rows", "media")
# Invariants that may legitimately change for converted inputs.
COUNT_INVARIANTS = ("text_elements", "tabs")

# XML members that appear in DOCX wordparts (local tag name).
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _local_name(tag: str) -> str:
    """Return the local (namespace-stripped) tag name."""
    return tag.rsplit("}", 1)[-1]


@dataclass
class StructureProfile:
    """Structural counts of one DOCX file."""

    paragraphs: int = 0
    tables: int = 0
    rows: int = 0
    media: int = 0
    text_elements: int = 0
    tabs: int = 0
    pages: int | None = None
    source_origin: str = "native"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iter_xml_parts(zf: zipfile.ZipFile):
    """Yield (member_name, local_name) for each XML element in word parts.

    Only ``word/*.xml`` members (excluding ``.rels``) are scanned, matching
    the extraction surface of the Okapi pipeline.
    """
    for name in zf.namelist():
        if not name.startswith("word/") or not name.endswith(".xml"):
            continue
        if "/_rels/" in name or name.endswith(".rels"):
            continue
        with zf.open(name) as fh:
            for _event, elem in ET.iterparse(fh, events=("end",)):
                yield name, _local_name(elem.tag)
                elem.clear()


def profile_docx(path: Path | str, source_origin: str = "native",
                 pages: int | None = None) -> StructureProfile:
    """Compute structural counts for a DOCX file.

    Uses streaming ``ElementTree.iterparse`` and ``elem.clear()`` so files
    with hundreds of thousands of paragraphs (bloat-flagged corpora) do not
    blow up memory.
    """
    profile = StructureProfile(source_origin=source_origin, pages=pages)
    with zipfile.ZipFile(str(path)) as zf:
        for _member, tag in _iter_xml_parts(zf):
            if tag == "p":
                profile.paragraphs += 1
            elif tag == "tbl":
                profile.tables += 1
            elif tag == "tr":
                profile.rows += 1
            elif tag == "t":
                profile.text_elements += 1
            elif tag == "tab":
                profile.tabs += 1
        profile.media = sum(
            1 for n in zf.namelist()
            if n.startswith("word/media/") and not n.endswith("/")
        )
    return profile


@dataclass
class InvariantDelta:
    """One invariant comparison result."""

    name: str
    original: int
    translated: int
    kind: str  # "topological" | "count"
    status: str  # "ok" | "delta"
    origin: str = "native"


@dataclass
class ScanReport:
    """Result of comparing an original vs translated pair."""

    original_profile: StructureProfile
    translated_profile: StructureProfile
    deltas: list[InvariantDelta] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[InvariantDelta]:
        return [d for d in self.deltas if d.kind == "topological" and d.status == "delta"]

    @property
    def warnings(self) -> list[InvariantDelta]:
        return [d for d in self.deltas if d.kind == "count" and d.status == "delta"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": self.original_profile.to_dict(),
            "translated": self.translated_profile.to_dict(),
            "deltas": [asdict(d) for d in self.deltas],
            "hard_failures": [d.name for d in self.hard_failures],
            "warnings": [d.name for d in self.warnings],
        }


def compare_structures(original: StructureProfile,
                       translated: StructureProfile) -> ScanReport:
    """Compare two profiles and produce a delta report.

    Topological invariants become hard failures when they differ; count-only
    invariants become warnings (conversion may legitimately change them).
    """
    report = ScanReport(original_profile=original, translated_profile=translated)
    for name in (*TOPOLOGICAL_INVARIANTS, *COUNT_INVARIANTS):
        orig_val = getattr(original, name)
        trans_val = getattr(translated, name)
        kind = "topological" if name in TOPOLOGICAL_INVARIANTS else "count"
        status = "ok" if orig_val == trans_val else "delta"
        report.deltas.append(
            InvariantDelta(
                name=name,
                original=orig_val,
                translated=trans_val,
                kind=kind,
                status=status,
                origin=translated.source_origin,
            )
        )
    return report