"""Unit tests for page-to-unit mapping (diagnostics group 8).

Covers ``file_translator/diagnostics/unit_locator.py``:
- fragment resolves to a trans-unit (spec scenario 1)
- placeholder/inline-code markup does not pollute visible source text
- missing target → ``is_translated=False``, ``target=None`` (spec scenario 2)
- empty target → ``is_translated=False``, ``target=""``
- populated target → ``is_translated=True``
- dir-level search across multiple XLIFF files
- case-insensitive matching + whitespace normalization
- missing/empty fragment and missing file → no matches
"""

from __future__ import annotations

from pathlib import Path

import pytest

from file_translator.diagnostics.unit_locator import (
    iter_xliff_files,
    locate_fragment,
    locate_fragment_in_xliff_dir,
    visible_text,
)


def _write_xliff(path: Path, body: str) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">\n'
        f"<file original=\"doc.docx\" source-language=\"ru\" target-language=\"en\">\n"
        f"<body>\n{body}\n</body>\n</file>\n</xliff>",
        encoding="utf-8",
    )
    return path


_TRANSLATED_UNIT = (
    '<trans-unit id="p_12">'
    "<source>Вид и объем выполненных работ: топографическая съемка.</source>"
    "<target>Scope of performed works: topographic survey.</target>"
    "</trans-unit>"
)

_UNTRANSLATED_UNIT = (
    '<trans-unit id="p_9">'
    "<source>Ситуационный план земельного участка.</source>"
    "</trans-unit>"
)

_EMPTY_TARGET_UNIT = (
    '<trans-unit id="p_340">'
    "<source>Координатная сетка на план наносится.</source>"
    "<target></target>"
    "</trans-unit>"
)


# ── visible_text / inline-code handling ────────────────────

def test_visible_text_skips_placeholder_markup(tmp_path):
    # Tikal wraps runs in bpt/ept whose .text is inline-code markup, not text;
    # <g> is generic inline code whose content IS visible text.
    xliff = _write_xliff(
        tmp_path / "doc.xlf",
        '<trans-unit id="p_1">'
        '<source>Текст с <g id="1">жирным</g> и '
        "гиперссылкой <bpt id=\"2\">&lt;run1&gt;</bpt>"
        "&lt;hyperlink1&gt;<ept id=\"2\">&lt;/run1&gt;</ept>.</source>"
        "<target>Text with <g id=\"1\">bold</g> and a link.</target>"
        "</trans-unit>",
    )
    units = locate_fragment("гиперссылкой", xliff)
    assert len(units) == 1
    # Visible text inside <g> is kept; bpt/ept inline-code markup is skipped.
    assert units[0]["source"] == "Текст с жирным и гиперссылкой <hyperlink1>."
    assert "run1" not in units[0]["source"]


# ── fragment resolves to a unit ────────────────────────────

def test_fragment_resolves_to_unit(tmp_path):
    xliff = _write_xliff(tmp_path / "doc.xlf", _TRANSLATED_UNIT)
    units = locate_fragment("топографическая съемка", xliff)
    assert len(units) == 1
    unit = units[0]
    assert unit["unit_id"] == "p_12"
    assert unit["file"] == str(xliff)
    assert unit["source"].startswith("Вид и объем")
    assert "source" in unit["source_xml"]
    assert "<" in unit["source_xml"]
    assert unit["is_translated"] is True
    assert unit["target"] == "Scope of performed works: topographic survey."


def test_fragment_matches_only_relevant_unit(tmp_path):
    xliff = _write_xliff(
        tmp_path / "doc.xlf",
        _UNTRANSLATED_UNIT + _TRANSLATED_UNIT + _EMPTY_TARGET_UNIT,
    )
    units = locate_fragment("Ситуационный план", xliff)
    assert [u["unit_id"] for u in units] == ["p_9"]


def test_fragment_not_found_returns_empty(tmp_path):
    xliff = _write_xliff(tmp_path / "doc.xlf", _TRANSLATED_UNIT)
    assert locate_fragment("такого текста нет", xliff) == []


def test_dir_search_across_multiple_files(tmp_path):
    _write_xliff(tmp_path / "a.xlf", _TRANSLATED_UNIT)
    _write_xliff(tmp_path / "b.xliff", _UNTRANSLATED_UNIT)
    _write_xliff(tmp_path / "c.xlf", _EMPTY_TARGET_UNIT)
    units = locate_fragment_in_xliff_dir("план", tmp_path)
    ids = {u["unit_id"] for u in units}
    assert ids == {"p_9", "p_340"}


def test_single_file_path_works_for_dir_function(tmp_path):
    xliff = _write_xliff(tmp_path / "doc.xlf", _TRANSLATED_UNIT)
    units = locate_fragment_in_xliff_dir("топографическая", xliff)
    assert len(units) == 1
    assert units[0]["unit_id"] == "p_12"


# ── untranslated unit exposure ─────────────────────────────

def test_missing_target_reported_untranslated(tmp_path):
    xliff = _write_xliff(tmp_path / "doc.xlf", _UNTRANSLATED_UNIT)
    units = locate_fragment("Ситуационный план", xliff)
    assert len(units) == 1
    unit = units[0]
    assert unit["is_translated"] is False
    assert unit["target"] is None


def test_empty_target_reported_untranslated(tmp_path):
    xliff = _write_xliff(tmp_path / "doc.xlf", _EMPTY_TARGET_UNIT)
    units = locate_fragment("Координатная сетка", xliff)
    assert len(units) == 1
    assert units[0]["is_translated"] is False
    assert units[0]["target"] == ""


def test_mixed_units_report_states(tmp_path):
    xliff = _write_xliff(
        tmp_path / "doc.xlf",
        _TRANSLATED_UNIT + _UNTRANSLATED_UNIT + _EMPTY_TARGET_UNIT,
    )
    units = locate_fragment_in_xliff_dir(".", xliff)  # every source ends with "."
    by_id = {u["unit_id"]: u for u in units}
    assert by_id["p_12"]["is_translated"] is True
    assert by_id["p_9"]["is_translated"] is False
    assert by_id["p_340"]["is_translated"] is False


# ── matching robustness ────────────────────────────────────

def test_case_insensitive_match(tmp_path):
    xliff = _write_xliff(tmp_path / "doc.xlf", _TRANSLATED_UNIT)
    units = locate_fragment("ТОПОГРАФИЧЕСКАЯ СЪЕМКА", xliff)
    assert len(units) == 1
    assert units[0]["unit_id"] == "p_12"


def test_whitespace_normalized_both_sides(tmp_path):
    # Fragment from a rendered page may contain line breaks / extra spaces.
    xliff = _write_xliff(
        tmp_path / "doc.xlf",
        "<trans-unit id=\"p_5\"><source>Первая строка.  Вторая строка.</source>"
        "<target>First line. Second line.</target></trans-unit>",
    )
    units = locate_fragment("Первая\nстрока.  Вторая", xliff)
    assert len(units) == 1


def test_empty_fragment_returns_empty(tmp_path):
    xliff = _write_xliff(tmp_path / "doc.xlf", _TRANSLATED_UNIT)
    assert locate_fragment("   ", xliff) == []


def test_missing_xliff_file_returns_empty(tmp_path):
    assert locate_fragment("что-то", tmp_path / "missing.xlf") == []
    assert locate_fragment_in_xliff_dir("что-то", tmp_path / "missing_dir") == []


def test_no_xliff_files_in_dir(tmp_path):
    (tmp_path / "note.txt").write_text("not xliff", encoding="utf-8")
    assert locate_fragment_in_xliff_dir("что-то", tmp_path) == []


def test_iter_xliff_files_globs_both_extensions(tmp_path):
    a = _write_xliff(tmp_path / "a.xlf", _TRANSLATED_UNIT)
    b = _write_xliff(tmp_path / "b.xliff", _UNTRANSLATED_UNIT)
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    files = sorted(iter_xliff_files(tmp_path))
    assert files == [a, b]


def test_iter_xliff_files_accepts_single_file(tmp_path):
    xliff = _write_xliff(tmp_path / "a.xlf", _TRANSLATED_UNIT)
    assert list(iter_xliff_files(xliff)) == [xliff]