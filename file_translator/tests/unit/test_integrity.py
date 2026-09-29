# -*- coding: utf-8 -*-
"""Unit tests for the post-merge integrity gate."""

import zipfile

from file_translator.application.integrity import (
    extract_paragraphs,
    run_integrity_check,
)


def _make_docx(path, paragraphs):
    """Write a minimal DOCX whose body contains one paragraph per item."""
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "\n".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>'
        for p in paragraphs
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document {ns}><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/word/document.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                    "</Types>")
        zf.writestr("word/document.xml", document)


def test_extract_paragraphs(tmp_path):
    p = tmp_path / "a.docx"
    _make_docx(p, ["Приказ МВД РК №732", "Hello world", "  spaced   out  "])
    paras = extract_paragraphs(p)
    assert paras == ["Приказ МВД РК №732", "Hello world", "spaced out"]


def test_gate_clean(tmp_path):
    orig = tmp_path / "o.docx"
    trans = tmp_path / "t.docx"
    paras = ["Приказ МВД РК от 24.10.2014 г. №732", "Some text to keep here",
             "The project provides maximum use of structures"]
    _make_docx(orig, paras)
    _make_docx(trans, [p.replace("№732", "№732") for p in paras])
    report = run_integrity_check(orig, trans)
    assert not report.critical
    assert report.loss_paragraphs == []
    assert report.numeric_mismatch_count == 0
    assert report.mixed_units == []


def test_gate_flags_loss(tmp_path):
    orig = tmp_path / "o.docx"
    trans = tmp_path / "t.docx"
    _make_docx(orig, [f"source paragraph number {i} with enough words to count" for i in range(10)])
    # translated doc drops four paragraphs entirely
    _make_docx(trans, [f"source paragraph number {i} with enough words to count" for i in range(0, 6)])
    report = run_integrity_check(orig, trans)
    assert report.critical
    assert report.loss_paragraphs != []


def test_gate_flags_numeric_drift(tmp_path):
    orig = tmp_path / "o.docx"
    trans = tmp_path / "t.docx"
    _make_docx(orig, ["Order of the Ministry of Internal Affairs No. 732"])
    _make_docx(trans, ["Order of the Ministry of Internal Affairs No. 107"])
    report = run_integrity_check(orig, trans)
    assert report.critical
    assert report.ref_mismatch_count > 0


def test_gate_non_reference_numeric_drift_is_advisory(tmp_path):
    orig = tmp_path / "o.docx"
    trans = tmp_path / "t.docx"
    _make_docx(orig, ["Amount of material equals 31 kilotons"])
    _make_docx(trans, ["Amount of material equals 41 kilotons"])
    report = run_integrity_check(orig, trans)
    assert not report.critical
    assert report.numeric_mismatch_count > 0
    assert report.ref_mismatch_count == 0


def test_gate_flags_mixed_units(tmp_path):
    orig = tmp_path / "o.docx"
    trans = tmp_path / "t.docx"
    _make_docx(orig, ["Проект предусматривает максимально возможное использование"])
    _make_docx(trans, ["The project предусматривает максимально возможное использование"])
    report = run_integrity_check(orig, trans)
    assert not report.critical  # mixed units are recorded, not critical
    assert len(report.mixed_units) >= 1