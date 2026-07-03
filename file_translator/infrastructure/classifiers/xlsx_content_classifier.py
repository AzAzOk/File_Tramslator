"""Content classifier for XLSX files.

Separates text-bearing XML content into categories so the translation
pipeline knows exactly what should be sent to the LLM and what must be
left untouched.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from lxml import etree

logger = logging.getLogger(__name__)

# ── Namespaces ──

_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

# ── Public types ──


class Category(Enum):
    """High-level category for a piece of cell/content data."""

    TEXT = "text"
    FORMULA = "formula"
    NUMBER = "number"
    DATE = "date"
    ERROR = "error"
    BOOLEAN = "boolean"
    HYPERLINK = "hyperlink"


class SourceType(Enum):
    """Where the text originates inside the XLSX."""

    SHARED_STRING = "shared_string"
    INLINE_STRING = "inline_string"
    COMMENT = "comment"
    CHART_TEXT = "chart_text"
    SHAPE_TEXT = "shape_text"
    HEADER_FOOTER = "header_footer"
    HYPERLINK_TEXT = "hyperlink_text"
    SHEET_NAME = "sheet_name"


@dataclass
class TextSource:
    """A classified piece of text extracted from an XLSX file.

    ``id`` is the lookup key that the translation layer uses to match
    translated text back to its origin.  It must be stable across
    ``extract → translate → save``.
    """

    id: str
    original_text: str
    path: str  # XML file path within the ZIP archive
    category: Category = Category.TEXT
    source_type: SourceType = SourceType.SHARED_STRING
    should_translate: bool = True
    xpath: str | None = None  # XPath to the text-bearing element
    metadata: dict[str, Any] = field(default_factory=dict)


# ── XML helpers (shared with the translator) ──


def _parse_xml(data: bytes) -> etree._Element:
    return etree.fromstring(data)


def _get_si_plain_text(si: etree._Element) -> str:
    """Extract plain text from an ``<si>`` (shared-string item) element."""
    r_runs = si.findall(f"{{{_SHEET_NS}}}r")
    if r_runs:
        parts: list[str] = []
        for r in r_runs:
            t_elem = r.find(f"{{{_SHEET_NS}}}t")
            if t_elem is not None and t_elem.text:
                parts.append(t_elem.text)
        return "".join(parts)
    t_elem = si.find(f"{{{_SHEET_NS}}}t")
    if t_elem is not None and t_elem.text:
        return t_elem.text
    return ""


# ── The classifier ──


class XlsxContentClassifier:
    """Walks every relevant XML file in an unpacked XLSX archive and returns
    classified :class:`TextSource` items.

    The classifier is **read-only** — it never modifies the XML trees.
    """

    # Regex patterns used to match archive entry names
    _SHEET_RE = re.compile(r"xl/worksheets/sheet\d+\.xml$")
    _COMMENT_RE = re.compile(r"xl/comments\d+\.xml$")
    _CHART_RE = re.compile(r"xl/charts/chart\d+\.xml$")
    _DRAWING_RE = re.compile(r"xl/drawings/drawing\d+\.xml$")
    _SHARED_STR = "xl/sharedStrings.xml"

    # ── Public API ──

    def classify(self, archive_data: dict[str, bytes]) -> list[TextSource]:
        """Classify all text-bearing content in the archive.

        Returns a flat list of ``TextSource`` items, each describing a
        single translatable or non-translatable piece of text.
        """
        sources: list[TextSource] = []
        counter = 0

        # Shared strings (the primary text store)
        sources.extend(self._classify_shared_strings(archive_data))

        # Inline strings in worksheets
        for name in list(archive_data):
            if not self._SHEET_RE.match(name):
                continue
            sources.extend(self._classify_inline_strings(name, archive_data[name]))

        # Comments
        for name in list(archive_data):
            if not self._COMMENT_RE.match(name):
                continue
            sources.extend(self._classify_comments(name, archive_data[name]))

        # Chart text
        for name in list(archive_data):
            if not self._CHART_RE.match(name):
                continue
            sources.extend(self._classify_chart_text(name, archive_data[name]))

        # Shape / textbox text
        for name in list(archive_data):
            if not self._DRAWING_RE.match(name):
                continue
            sources.extend(self._classify_shape_text(name, archive_data[name]))

        logger.info(
            "Classifier found %d text sources (%d translatable)",
            len(sources),
            sum(1 for s in sources if s.should_translate),
        )
        return sources

    # ── per-source classifiers ──

    def _classify_shared_strings(self, archive_data: dict[str, bytes]) -> list[TextSource]:
        ss_data = archive_data.get(self._SHARED_STR)
        if not ss_data:
            return []
        try:
            tree = _parse_xml(ss_data)
        except Exception as e:
            logger.warning("Failed to parse %s: %s", self._SHARED_STR, e)
            return []

        sources: list[TextSource] = []
        for idx, si in enumerate(tree.iter(f"{{{_SHEET_NS}}}si")):
            text = _get_si_plain_text(si)
            if not text.strip():
                continue
            sources.append(TextSource(
                id=f"ss_{idx}",
                original_text=text,
                path=self._SHARED_STR,
                category=Category.TEXT,
                source_type=SourceType.SHARED_STRING,
                metadata={
                    "si_index": idx,
                    "has_rich_text": len(si.findall(f"{{{_SHEET_NS}}}r")) > 0,
                    "source_xml": etree.tostring(si, encoding="unicode"),
                },
            ))
        return sources

    def _classify_inline_strings(self, name: str, data: bytes) -> list[TextSource]:
        try:
            tree = _parse_xml(data)
        except Exception as e:
            logger.warning("Failed to parse %s: %s", name, e)
            return []

        sources: list[TextSource] = []
        for c in tree.iter(f"{{{_SHEET_NS}}}c"):
            t_attr = c.get("t", "")
            if t_attr != "inlineStr":
                continue
            is_elem = c.find(f"{{{_SHEET_NS}}}is")
            if is_elem is None:
                continue
            t_elem = is_elem.find(f"{{{_SHEET_NS}}}t")
            if t_elem is None or not t_elem.text:
                continue
            sources.append(TextSource(
                id=f"ws_{name}_cell_{c.get('r', '')}",
                original_text=t_elem.text,
                path=name,
                category=Category.TEXT,
                source_type=SourceType.INLINE_STRING,
                metadata={"cell_ref": c.get("r", "")},
            ))
        return sources

    def _classify_comments(self, name: str, data: bytes) -> list[TextSource]:
        try:
            tree = _parse_xml(data)
        except Exception as e:
            logger.warning("Failed to parse %s: %s", name, e)
            return []

        sources: list[TextSource] = []
        for comment in tree.iter(f"{{{_SHEET_NS}}}comment"):
            text_elem = comment.find(f".//{{{_SHEET_NS}}}t")
            if text_elem is None or not text_elem.text:
                continue
            sources.append(TextSource(
                id=f"cm_{name}_{comment.get('ref', '')}",
                original_text=text_elem.text,
                path=name,
                category=Category.TEXT,
                source_type=SourceType.COMMENT,
                metadata={"ref": comment.get("ref", ""), "author_id": comment.get("authorId", "")},
            ))
        return sources

    def _classify_chart_text(self, name: str, data: bytes) -> list[TextSource]:
        """Extract text from chart XML (titles, series, axis labels, etc.)."""
        try:
            tree = _parse_xml(data)
        except Exception as e:
            logger.warning("Failed to parse chart %s: %s", name, e)
            return []

        sources: list[TextSource] = []
        for t_elem in tree.iter(f"{{{_A_NS}}}t"):
            if t_elem.text and t_elem.text.strip():
                # Build an XPath-like locator by walking up the tree
                refs = []
                p = t_elem
                while p is not None and p != tree:
                    refs.append(p.tag.split("}")[-1] if "}" in p.tag else p.tag)
                    p = p.getparent() if p.getparent() is not None else None
                sources.append(TextSource(
                    id=f"chart_{name}_{t_elem.text[:20]}",
                    original_text=t_elem.text,
                    path=name,
                    category=Category.TEXT,
                    source_type=SourceType.CHART_TEXT,
                    metadata={"tag_path": " > ".join(reversed(refs))},
                ))
        return sources

    def _classify_shape_text(self, name: str, data: bytes) -> list[TextSource]:
        """Extract text from shapes and textboxes in drawing XML."""
        try:
            tree = _parse_xml(data)
        except Exception as e:
            logger.warning("Failed to parse drawing %s: %s", name, e)
            return []

        sources: list[TextSource] = []
        for t_elem in tree.iter(f"{{{_A_NS}}}t"):
            if t_elem.text and t_elem.text.strip():
                sources.append(TextSource(
                    id=f"shape_{name}_{t_elem.text[:20]}",
                    original_text=t_elem.text,
                    path=name,
                    category=Category.TEXT,
                    source_type=SourceType.SHAPE_TEXT,
                ))
        return sources
