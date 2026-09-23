"""Unit tests for file_translator.diagnostics.structure_scanner.

Covers the invariant scanner against minimal hand-built DOCX fixtures:
- an intact pair passes (no hard failures, no warnings)
- a table-structure delta is reported as a hard failure
- a text-element delta is reported as a count warning with source_origin
- page-count drift is carried through the profile (no compare-level flag)
- bloat input (hundreds of thousands of paragraphs) profiles without a
  memory blow-up thanks to streaming iterparse.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from file_translator.diagnostics.structure_scanner import (
    COUNT_INVARIANTS,
    TOPOLOGICAL_INVARIANTS,
    StructureProfile,
    compare_structures,
    profile_docx,
)

# Minimal OOXML word/document.xml templates.
_DOC_BODY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {body}
  </w:body>
</w:document>
"""

_PARA = "<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

_TABLE = (
    "<w:tbl>"
    "<w:tr><w:tc>{para}</w:tc></w:tr>"
    "<w:tr><w:tc>{para}</w:tc></w:tr>"
    "<w:tr><w:tc>{para}</w:tc></w:tr>"
    "</w:tbl>"
)


def _build_docx(
    body: str,
    *,
    media_files: int = 0,
    extra_parts: dict[str, str] | None = None,
) -> bytes:
    """Pack a minimal DOCX archive into memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("_rels/.rels",
                    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        zf.writestr("word/document.xml", _DOC_BODY.format(body=body))
        for i in range(media_files):
            zf.writestr(f"word/media/image{i}.png", b"\x89PNG")
        for name, content in (extra_parts or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _intact_original() -> bytes:
    body = (
        _PARA.format(text="Hello world")
        + _PARA.format(text="Second paragraph")
        + "".join(_TABLE.format(para=_PARA.format(text=f"cell {i}")) for i in range(2))
    )
    return _build_docx(body, media_files=2)


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_intact_pair_passes(tmp_path):
    orig = _write(tmp_path, "original.docx", _intact_original())
    trans = _write(tmp_path, "translated.docx", _intact_original())
    report = compare_structures(
        profile_docx(orig, source_origin="native"),
        profile_docx(trans, source_origin="native"),
    )
    assert report.hard_failures == []
    assert report.warnings == []
    # Sanity: the fixture has 2 paragraphs + 6 cell paragraphs; 2 tables, 6 rows.
    assert report.original_profile.paragraphs == 8
    assert report.original_profile.tables == 2
    assert report.original_profile.rows == 6
    assert report.original_profile.media == 2


def test_table_delta_is_hard_failure(tmp_path):
    orig = _write(tmp_path, "original.docx", _intact_original())
    # Translated drops the second table (2 tables → 1 table) but compensates
    # with standalone paragraphs so the paragraph count stays identical.
    body = (
        _PARA.format(text="Hello world")
        + _PARA.format(text="Second paragraph")
        + _TABLE.format(para=_PARA.format(text="cell 0"))
        + "".join(_PARA.format(text=f"cell {i}") for i in range(3))
    )
    trans = _write(tmp_path, "translated.docx", _build_docx(body, media_files=2))
    report = compare_structures(
        profile_docx(orig, source_origin="native"),
        profile_docx(trans, source_origin="native"),
    )
    assert [d.name for d in report.hard_failures] == ["tables", "rows"]
    assert report.warnings == []


def test_text_element_delta_is_warning_with_origin(tmp_path):
    # Original: "Aa" (1 text node) + run-split "A|B" (2 text nodes) → total 3.
    # Translated merges the split runs → "Aa" + "AB" (2 text nodes).
    orig_body = (
        _PARA.format(text="Aa")
        + "<w:p><w:r><w:t>A</w:t></w:r><w:r><w:t>B</w:t></w:r></w:p>"
    )
    trans_body = (
        _PARA.format(text="Aa")
        + _PARA.format(text="AB")
    )
    orig = _write(tmp_path, "original.docx", _build_docx(orig_body))
    trans = _write(tmp_path, "translated.docx", _build_docx(trans_body))
    report = compare_structures(
        profile_docx(orig, source_origin="native"),
        profile_docx(trans, source_origin="pdf_converted", pages=5),
    )
    assert report.hard_failures == []
    warnings = {d.name: d for d in report.warnings}
    assert "text_elements" in warnings
    assert warnings["text_elements"].original == 3
    assert warnings["text_elements"].translated == 2
    # source_origin context rides along with the warning.
    assert warnings["text_elements"].origin == "pdf_converted"


def test_page_count_drift_rides_on_profile(tmp_path):
    """Pages are carried on the profile; a drift is visible in the profiles."""
    orig = _write(tmp_path, "original.docx", _intact_original())
    trans = _write(tmp_path, "translated.docx", _intact_original())
    report = compare_structures(
        profile_docx(orig, source_origin="native", pages=10),
        profile_docx(trans, source_origin="native", pages=12),
    )
    # Structure is intact but page counts differ → visible on both profiles.
    assert report.hard_failures == []
    assert report.original_profile.pages == 10
    assert report.translated_profile.pages == 12
    assert report.original_profile.pages != report.translated_profile.pages


def test_bloat_input_profiles_streamingly(tmp_path):
    """Thousands of paragraphs must not exhaust memory (iterparse streaming)."""
    # 5_000 paragraphs ≈ bloat-scale fixture without a 167 MB source file.
    body = "".join(_PARA.format(text=f"paragraph {i}") for i in range(5_000))
    doc = _write(tmp_path, "bloat.docx", _build_docx(body))
    profile = profile_docx(doc, source_origin="native")
    assert profile.paragraphs == 5_000
    assert profile.text_elements == 5_000


def test_tabs_are_counted(tmp_path):
    para_with_tabs = "<w:p><w:r><w:t>A</w:t></w:r><w:r><w:tab/><w:tab/><w:t>B</w:t></w:r></w:p>"
    doc = _write(tmp_path, "tabs.docx", _build_docx(para_with_tabs))
    profile = profile_docx(doc)
    assert profile.tabs == 2
    assert profile.text_elements == 2


def test_invariant_kinds_mapping():
    for name in TOPOLOGICAL_INVARIANTS:
        assert name in ("paragraphs", "tables", "rows", "media")
    for name in COUNT_INVARIANTS:
        assert name in ("text_elements", "tabs")


def test_to_dict_shape(tmp_path):
    orig = _write(tmp_path, "original.docx", _intact_original())
    trans = _write(tmp_path, "translated.docx", _intact_original())
    report = compare_structures(
        profile_docx(orig, source_origin="native"),
        profile_docx(trans, source_origin="native"),
    )
    d = report.to_dict()
    assert set(d) == {"original", "translated", "deltas", "hard_failures", "warnings"}
    assert d["hard_failures"] == []
    assert d["deltas"][0]["name"] == "paragraphs"
    assert d["deltas"][0]["kind"] == "topological"
    assert d["deltas"][0]["status"] == "ok"