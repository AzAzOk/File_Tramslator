"""Unit tests for the word-boundary breakage (glued-word) scanner."""

from __future__ import annotations

import json

import pytest

from file_translator.diagnostics.glue_scanner import (
    _classify_token,
    find_glued_words,
    scan_docx_glued_words,
    scan_page_text,
)


# ---------------------------------------------------------------------------
# token classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", [
    # Documented B3 cases.
    "fortheproject",      # for + the + project
    "createan",           # create + an
    "ofthesite",          # of + the + site
    "theinitial",         # the + initial
    # Camel-boundary glue (space dropped before a capitalized word).
    "andSP",              # and SP
    "theProject",         # the + Project
    # Long content-word concatenation.
    "engineeringtopographic",  # engineering + topographic
])
def test_glued_tokens_flagged(token):
    assert _classify_token(token) is not None


@pytest.mark.parametrize("token", [
    # Legitimate CamelCase / product / proper names.
    "AutoCAD",
    "PetroKazakhstan",
    "GalaxyG6",
    "OpenAI",
    "Geostroinzyskaniya",
    # Dictionary words that a naive splitter would decompoose.
    "everything",
    "another",
    "because",
    "together",
    "underground",
    "groundwater",
    "information",
    "construction",
    "development",
    "engineering",
    "geophysical",
    "hydrogeological",
    "nonetheless",
    "therefore",
    "investigations",
    # Short / single words.
    "the",
    "for",
    "project",
])
def test_non_glue_tokens_not_flagged(token):
    assert _classify_token(token) is None


def test_cyrillic_token_not_flagged():
    # Cyrillic text is a leak-scanner concern, not a glue concern.
    assert _classify_token("выполненных") is None
    assert _classify_token("СНРК") is None


# ---------------------------------------------------------------------------
# text-level scanning
# ---------------------------------------------------------------------------

def test_find_glued_words_reports_token_and_context():
    text = "surveys is to createan up-to-date engineering topographic plan"
    findings = find_glued_words(text)
    assert any(f["token"] == "createan" for f in findings)
    f = findings[0]
    assert f["reason"] in ("lowercase_concat", "camel_boundary")
    assert "createan" in f["context"]


def test_find_glued_words_clean_text():
    text = "The engineering and geodetic surveys were completed for the project."
    assert find_glued_words(text) == []


# ---------------------------------------------------------------------------
# docx paragraph scan
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


def test_scan_docx_glued_words(tmp_path):
    docx = tmp_path / "t.docx"
    # fotheproject must be split across two <w:t> runs to test paragraph
    # reconstruction (the real-world B3 case).
    body = (
        '<w:p><w:r><w:t>for</w:t></w:r><w:r><w:t>theproject</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Clean paragraph with proper spacing.</w:t></w:r></w:p>'
    )
    import zipfile
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>{}</w:body></w:document>".format(body)
    )
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("word/document.xml", doc)

    findings = scan_docx_glued_words(docx)
    assert len(findings) == 1
    f = findings[0]
    assert f["token"] == "fortheproject"
    assert f["paragraph"] == 0
    assert f["page"] is None
    assert "for" in f["fragment"] and "theproject" in f["fragment"]


def test_scan_docx_glued_words_no_document_xml(tmp_path):
    import io
    import zipfile

    docx = tmp_path / "empty.docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    docx.write_bytes(buf.getvalue())
    assert scan_docx_glued_words(docx) == []


# ---------------------------------------------------------------------------
# rendered page-text scan
# ---------------------------------------------------------------------------

def _page_text_page(page: int, word_lines: list[tuple[str, int, int]]) -> dict:
    return {
        "page": page,
        "pdf": "x.pdf",
        "text": "",
        "words": [
            {"text": t, "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0,
             "block": b, "line": l, "word": i}
            for i, (t, b, l) in enumerate(word_lines)
        ],
    }


def test_scan_page_text_reports_page_block_line(tmp_path):
    path = tmp_path / "page_text.jsonl"
    # One visual line contains a single glued token (missing space), another
    # contains the legitimate CamelCase product name.
    rec = _page_text_page(
        15,
        [
            ("The", 0, 0), ("work", 0, 0), ("was", 0, 0), ("done", 0, 0),
            ("fortheproject", 0, 0),
            ("AutoCAD", 1, 0), ("files", 1, 0), ("are", 1, 0), ("ready", 1, 0),
        ],
    )
    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    findings = scan_page_text(path)
    # fortheproject is a fragment-level token; AutoCAD must NOT be flagged.
    assert len(findings) == 1
    f = findings[0]
    assert f["page"] == 15
    assert f["token"] == "fortheproject"
    assert f["block"] == 0 and f["line"] == 0


def test_scan_page_text_fallback_raw(tmp_path):
    path = tmp_path / "page_text.jsonl"
    rec = {"page": 9, "pdf": "x.pdf", "text": "done fortheproject over here\n", "words": []}
    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    findings = scan_page_text(path)
    assert len(findings) == 1
    assert findings[0]["page"] == 9
    assert findings[0]["token"] == "fortheproject"


def test_scan_page_text_skips_malformed(tmp_path):
    path = tmp_path / "page_text.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    assert scan_page_text(path) == []