"""Page-to-unit mapping (diagnostics group 8).

Given a text fragment from a rendered page (e.g. «страница 9 не переведена»
→ the Cyrillic sentence visible on that page) and a directory of retained
XLIFF files (kept on disk by the debug artifact retention, group 7), find the
trans-units whose source text contains that fragment.

For each matched trans-unit the tools report:

- ``unit_id``  — the XLIFF ``id`` attribute (also written into the DOCX by
  Tikal, so it maps back to a paragraph/table cell),
- ``source``  — visible plain text of the ``<source>`` element,
- ``source_xml`` — serialized ``<source>`` XML (inline codes, tags),
- ``target`` / ``is_translated`` — target population state.

The visible-text algorithm mirrors ``OkapiService._simple_plain_text``
(placeholder tags ``<bpt>/<ept>/<ph>/<it>`` carry inline-code markup in their
``.text`` and must be skipped) but is implemented here stand-alone: this
package is host-side tooling and must not import the running service.
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from typing import Iterator, TypedDict

import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

_XLIFF_GLOBS = ("*.xliff", "*.xlf")
_PLACEHOLDER_TAGS = {"bpt", "ept", "ph", "it"}
_XLIFF_TRANS_UNIT = "trans-unit"
_XLIFF_SOURCE = "source"
_XLIFF_TARGET = "target"


class UnitMatch(TypedDict):
    """One matching trans-unit, as reported by :func:`locate_fragment`.

    ``target`` is ``None`` when the ``<target>`` element is missing and the
    empty string when it exists but is empty — both mean untranslated
    (``is_translated=False``).
    """

    unit_id: str
    file: str
    source: str
    source_xml: str
    target: str | None
    is_translated: bool


def visible_text(element: ET.Element) -> str:
    """Extract visible plain text from an XLIFF element, skipping inline-code markup.

    Walks the element tree: ``.text`` of placeholder tags (``bpt``, ``ept``,
    ``ph``, ``it``) is inline code markup (``&lt;hyperlink1&gt;``), not visible
    text, so only their ``.tail`` is kept. All whitespace collapses to single
    spaces so a fragment from the rendered page matches regardless of line breaks.
    """
    parts: list[str] = []

    def walk(el: ET.Element) -> None:
        if el.text:
            parts.append(el.text)
        for child in el:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag not in _PLACEHOLDER_TAGS:
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(element)
    text = html.unescape("".join(parts))
    text = re.sub(r"</?run\d+\s*/?>", "", text)
    text = re.sub(r"</?tags?\d*\s*/?>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _local_tag(el: ET.Element) -> str:
    return el.tag.split("}")[-1] if "}" in el.tag else el.tag


def iter_xliff_files(xliff_dir: str | Path) -> Iterator[Path]:
    """Yield XLIFF files (``*.xliff`` / ``*.xlf``) directly under ``xliff_dir``.

    Also accepts a path to a single XLIFF file.
    """
    path = Path(xliff_dir)
    if path.is_file():
        yield path
        return
    for glob in _XLIFF_GLOBS:
        yield from sorted(path.glob(glob))


def _source_xml(source_elem: ET.Element | None) -> str:
    if source_elem is None:
        return ""
    raw = ET.tostring(source_elem, encoding="unicode")
    return html.unescape(raw)


def locate_fragment(
    fragment: str,
    xliff_path: str | Path,
    *,
    case_insensitive: bool = True,
) -> list[UnitMatch]:
    """Find trans-units in one XLIFF file whose source contains ``fragment``.

    Returns a list of dicts (in document order)::

        {
            "unit_id": "p_12",
            "file": "C:/.../doc.xlf",
            "source": "Вид и объем выполненных работ: ...",
            "source_xml": "<source>...</source>",
            "target": None,            # missing/untranslated unit
            "is_translated": False,
        }

    ``target`` is ``None`` when the ``<target>`` element is missing, and the
    empty string when it exists but is empty — both are reported as
    ``is_translated=False``.
    """
    needle = re.sub(r"\s+", " ", fragment or "").strip()
    needle_cf = needle.casefold() if case_insensitive else needle
    if not needle:
        return []

    path = Path(xliff_path)
    if not path.exists():
        logger.warning("XLIFF file not found: %s", path)
        return []

    matches: list[UnitMatch] = []
    try:
        tree = ET.parse(str(path))
    except ET.ParseError as e:
        logger.warning("Failed to parse XLIFF %s: %s", path, e)
        return []

    root = tree.getroot()
    for trans_unit in root.iter():
        if _local_tag(trans_unit) != _XLIFF_TRANS_UNIT:
            continue

        source_elem = None
        target_elem = None
        for child in trans_unit:
            tag = _local_tag(child)
            if tag == _XLIFF_SOURCE and source_elem is None:
                source_elem = child
            elif tag == _XLIFF_TARGET and target_elem is None:
                target_elem = child

        source_text = visible_text(source_elem) if source_elem is not None else ""
        haystack_cf = source_text.casefold() if case_insensitive else source_text
        if needle_cf not in haystack_cf:
            continue

        target_text = visible_text(target_elem) if target_elem is not None else None
        is_translated = target_text is not None and bool(target_text.strip())
        matches.append(
            {
                "unit_id": trans_unit.get("id", ""),
                "file": str(path),
                "source": source_text,
                "source_xml": _source_xml(source_elem),
                "target": target_text,
                "is_translated": is_translated,
            }
        )

    return matches


def locate_fragment_in_xliff_dir(
    fragment: str,
    xliff_dir: str | Path,
    *,
    case_insensitive: bool = True,
) -> list[UnitMatch]:
    """Search for ``fragment`` across every XLIFF file in ``xliff_dir``.

    Accepts either a directory (globs ``*.xliff``/``*.xlf``) or a single file.
    Returns matches from all files in document order; callers may group by
    ``file``/``unit_id``.
    """
    results: list[UnitMatch] = []
    for path in iter_xliff_files(xliff_dir):
        results.extend(locate_fragment(fragment, path, case_insensitive=case_insensitive))
    return results