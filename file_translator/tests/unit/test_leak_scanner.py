"""Unit tests for the normative whitelist and the Cyrillic-leak scanner."""

from __future__ import annotations

import json

import pytest

from file_translator.diagnostics.leak_scanner import (
    _classify_fragment,
    scan_docx_leaks,
    scan_page_text,
)
from file_translator.diagnostics.normative_whitelist import (
    DEFAULT_DESIGNATION_PREFIXES,
    is_list_heading,
    is_normative_reference,
    matches_normative_designation,
    strip_cjk,
    strip_cyrillic,
)


# ---------------------------------------------------------------------------
# whitelist matchers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "СН РК 1.02-04-2013 «Инженерно-геодезические изыскания»",
    "ГОСТ 21.1101-2013",
    "ГОСТ Р 21.1101-2013",
    "ГКИНП-02-033 «Инструкция по нивелированию»",
    "СП РК 1.02-101-2014",
    "СНиП 2.04.05-91* Отопление, вентиляция и кондиционирование",
    "ВСН 34-91",
    "ЕНиР Е1-1",
])
def test_matches_normative_designation(text):
    assert matches_normative_designation(text)


@pytest.mark.parametrize("text", [
    "- СН РК 1.02-04-2013 «Инженерно-геодезические изыскания»",
    "— СП РК 1.02-101-2014",
    "• ГОСТ 21.1101-2013",
    "    - ГКИНП-02-033 «Инструкция»",
])
def test_matches_normative_designation_with_bullet(text):
    # Normative lists render entries with leading bullets; the designation is
    # still a reference and must stay whitelisted.
    assert matches_normative_designation(text)


@pytest.mark.parametrize("text", [
    "",
    "СП РК",
    "Проект организации строительства",
    "Инженерно-геодезические изыскания для строительства",
    "Нормативные документы",
])
def test_matches_normative_designation_negative(text):
    # Bare prefixes without a following identifier are not references.
    assert not matches_normative_designation(text)


@pytest.mark.parametrize("text", [
    "- СН РК",
    "— СП РК",
    "• ГОСТ",
])
def test_bulleted_bare_prefix_requires_identifier(text):
    # A bullet in front of a lone prefix is still NOT a reference.
    assert not matches_normative_designation(text)


def test_prefix_requires_identifier():
    # A lone prefix with no code is NOT a designation match even though the
    # prefix itself is in the list.
    assert not matches_normative_designation("СН РК")


@pytest.mark.parametrize("text", [
    "НПА и технические условия:",
    "Перечень НПА по строительству",
    "Нормативные документы",
    "Нормативные ссылки",
    "Список используемой литературы",
])
def test_is_list_heading(text):
    assert is_list_heading(text)


def test_heading_not_reference():
    # The heading «НПА и технические условия:» must NOT be whitelisted.
    assert not is_normative_reference("НПА и технические условия:")


def test_reference_not_heading():
    assert is_normative_reference("СН РК 1.02-04-2013")
    assert not is_list_heading("СН РК 1.02-04-2013")


def test_custom_prefixes():
    custom = ("XYZ",)
    assert matches_normative_designation("XYZ-001", prefixes=custom)
    assert not matches_normative_designation("ГОСТ 21.1101-2013", prefixes=custom)


def test_strip_helpers():
    assert strip_cjk("Привет 中文 world") == "Привет  world"
    assert strip_cyrillic("Привет 中文 world") == " 中文 world"


# ---------------------------------------------------------------------------
# leak scanner — fragment classification
# ---------------------------------------------------------------------------

def test_english_fragment_not_flagged():
    assert _classify_fragment("This paragraph is fully translated.", None) is None


def test_cyrillic_fragment_flagged():
    finding = _classify_fragment("Не переведённый абзац документа.", None)
    assert finding is not None
    assert finding.reason == "untranslated_fragment"
    assert finding.cyrillic_chars == _cyrillic_count_expected("Не переведённый абзац документа.")


def _cyrillic_count_expected(text: str) -> int:
    # Characters in U+0400..U+04FF (Cyrillic block). "Не переведённый абзац
    # документа." → Не(2)+переведённый(12)+абзац(5)+документа(9) = 28.
    import re as _re
    return len(_re.findall(r"[\u0400-\u04ff]", text))


def test_normative_reference_not_flagged():
    text = "СН РК 1.02-04-2013 «Инженерно-геодезические изыскания»"
    assert _classify_fragment(text, None) is None


def test_bulleted_normative_reference_not_flagged():
    # B2 real-data case: normative lists render entries with "- " bullets; the
    # preserved norm designation must not be reported as a leak.
    text = "- СН РК 1.02-04-2013 «Инженерно-геодезические изыскания»"
    assert _classify_fragment(text, None) is None


def test_list_heading_flagged():
    finding = _classify_fragment("НПА и технические условия:", None)
    assert finding is not None
    assert finding.reason == "list_heading"


def test_bulleted_list_heading_still_flagged():
    # A bullet before a heading must NOT whitelist it.
    finding = _classify_fragment("- НПА и технические условия:", None)
    assert finding is not None
    assert finding.reason == "list_heading"


# ---------------------------------------------------------------------------
# leak scanner — page_text.jsonl input
# ---------------------------------------------------------------------------

def _page_text_page(page: int, word_lines: list[tuple[str, int, int]], raw: str = "") -> dict:
    return {
        "page": page,
        "pdf": "x.pdf",
        "text": raw,
        "words": [
            {"text": t, "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0,
             "block": b, "line": l, "word": i}
            for i, (t, b, l) in enumerate(word_lines)
        ],
    }


def test_scan_page_text_reports_page_and_fragment(tmp_path):
    path = tmp_path / "page_text.jsonl"
    rec = _page_text_page(
        9,
        [
            ("НПА", 0, 0), ("и", 0, 0), ("технические", 0, 0), ("условия:", 0, 0),
            ("СН", 0, 1), ("РК", 0, 1), ("1.02-04-2013", 0, 1),
            ("Good", 1, 0), ("English", 1, 0),
        ],
    )
    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    findings = scan_page_text(path)
    assert len(findings) == 1  # heading flagged; normative reference + EN skipped
    f = findings[0]
    assert f["page"] == 9
    assert f["reason"] == "list_heading"
    assert f["cyrillic_chars"] > 0
    assert f["block"] == 0 and f["line"] == 0


def test_scan_page_text_no_words_fallback_to_raw(tmp_path):
    path = tmp_path / "page_text.jsonl"
    rec = {"page": 3, "pdf": "x.pdf", "text": "Остаток исходного текста\n", "words": []}
    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    findings = scan_page_text(path)
    assert len(findings) == 1
    assert findings[0]["page"] == 3
    assert "Остаток исходного" in findings[0]["fragment"]


def test_scan_page_text_skips_malformed_lines(tmp_path):
    path = tmp_path / "page_text.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    assert scan_page_text(path) == []


# ---------------------------------------------------------------------------
# leak scanner — docx input
# ---------------------------------------------------------------------------

def _build_docx(paragraphs: list[str]) -> bytes:
    import io
    import zipfile

    body = "".join(
        '<w:p><w:r><w:t>{}</w:t></w:r></w:p>'.format(p) for p in paragraphs
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>{}</w:body></w:document>".format(body)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", doc)
    return buf.getvalue()


def test_scan_docx_leaks(tmp_path):
    docx = tmp_path / "t.docx"
    docx.write_bytes(_build_docx([
        "Этот абзац не переведён.",
        "СН РК 1.02-04-2013 «Инженерно-геодезические изыскания»",
        "- СП РК 1.02-101-2014 «Строительный кадастр»",
        "НПА и технические условия:",
        "Fully translated paragraph.",
    ]))
    findings = scan_docx_leaks(docx)
    reasons = {f["reason"] for f in findings}
    fragments = [f["fragment"] for f in findings]
    assert reasons == {"untranslated_fragment", "list_heading"}
    assert any("Этот абзац" in fr for fr in fragments)
    assert any(fr.startswith("НПА и технические условия") for fr in fragments)
    assert not any("1.02-04" in fr for fr in fragments)  # whitelisted
    assert not any("1.02-101" in fr for fr in fragments)  # bulleted, whitelisted


def test_scan_docx_leaks_no_document_xml(tmp_path):
    import io
    import zipfile

    docx = tmp_path / "empty.docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    docx.write_bytes(buf.getvalue())
    assert scan_docx_leaks(docx) == []


def test_all_default_prefixes_present():
    for p in DEFAULT_DESIGNATION_PREFIXES:
        assert matches_normative_designation(f"{p} 123-2020")