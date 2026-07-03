"""Test XlsxTranslator with a minimal XLSX file."""
import zipfile
from pathlib import Path

import pytest

from file_translator.infrastructure.translators.xlsx_translator import XlsxTranslator


@pytest.fixture
def minimal_xlsx(tmp_path):
    xlsx = tmp_path / "test.xlsx"
    with zipfile.ZipFile(xlsx, "w") as z:
        z.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            '</Types>'
        ))
        z.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ))
        z.writestr("xl/workbook.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>'
        ))
        z.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            '</Relationships>'
        ))
        z.writestr("xl/sharedStrings.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="2" uniqueCount="2">'
            '<si><t>Hello World</t></si>'
            '<si><r><rPr><b/><sz val="11"/><rFont val="Calibri"/></rPr><t>Bold</t></r><r><rPr><sz val="11"/><rFont val="Calibri"/></rPr><t>Normal</t></r></si>'
            '</sst>'
        ))
        z.writestr("xl/worksheets/sheet1.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="inlineStr"><is><t>Inline Cell</t></is></c>'
            '</row></sheetData>'
            '</worksheet>'
        ))
        z.writestr("xl/comments1.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<comments xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<authors><author>User</author></authors>'
            '<commentList><comment ref="A1" authorId="0">'
            '<text><r><rPr><sz val="10"/><rFont val="Calibri"/></rPr><t>Comment text</t></r></text>'
            '</comment></commentList>'
            '</comments>'
        ))
    return xlsx


class TestXlsxTranslator:
    def test_can_process_xlsx(self, minimal_xlsx):
        t = XlsxTranslator()
        assert t.can_process(minimal_xlsx)

    def test_can_process_rejects_non_xlsx(self, tmp_path):
        t = XlsxTranslator()
        f = tmp_path / "test.txt"
        f.write_text("hello")
        assert not t.can_process(f)

    def test_supported_formats(self):
        t = XlsxTranslator()
        fmts = t.supported_formats()
        assert any(f.value == "xlsx" for f in fmts)
        assert any(f.value == "xls" for f in fmts)

    def test_extract_shared_strings(self, minimal_xlsx):
        t = XlsxTranslator()
        result = t.extract(minimal_xlsx)
        units = result["text_units"]
        ids = [u.id for u in units]
        assert "ss_0" in ids
        assert "ss_1" in ids

    def test_extract_inline_strings(self, minimal_xlsx):
        t = XlsxTranslator()
        result = t.extract(minimal_xlsx)
        units = result["text_units"]
        inline = [u for u in units if u.metadata.get("source_type") == "inline_string"]
        assert len(inline) == 1
        assert inline[0].original_text == "Inline Cell"

    def test_extract_comments(self, minimal_xlsx):
        t = XlsxTranslator()
        result = t.extract(minimal_xlsx)
        units = result["text_units"]
        comments = [u for u in units if u.metadata.get("source_type") == "comment"]
        assert len(comments) == 1
        assert comments[0].original_text == "Comment text"

    def test_translate_shared_strings(self, minimal_xlsx):
        t = XlsxTranslator()
        result = t.extract(minimal_xlsx)
        translations = {"ss_0": "Привет мир", "ss_1": "Жирный Нормальный"}
        translated = t.translate(result, translations)
        assert translated["translations_applied"] == 2

    def test_translate_inline_strings(self, minimal_xlsx):
        t = XlsxTranslator()
        result = t.extract(minimal_xlsx)
        inline = [u for u in result["text_units"] if u.metadata.get("source_type") == "inline_string"]
        translations = {inline[0].id: "Ячейка"}
        translated = t.translate(result, translations)
        assert translated["translations_applied"] == 1

    def test_save_roundtrip(self, minimal_xlsx, tmp_path):
        t = XlsxTranslator()
        result = t.extract(minimal_xlsx)
        translations = {"ss_0": "Привет мир", "ss_1": "Жирный Нормальный"}
        translated = t.translate(result, translations)
        out = tmp_path / "output.xlsx"
        t.save(translated, out)
        assert out.exists()

    def test_rich_text_preserved_after_translation(self, minimal_xlsx):
        t = XlsxTranslator()
        result = t.extract(minimal_xlsx)
        rich = [u for u in result["text_units"] if u.metadata.get("source_type") == "shared_string" and u.metadata.get("has_rich_text")]
        assert len(rich) == 1
        assert "source_xml" in rich[0].metadata

    def test_serialization_no_ns0_prefix(self, minimal_xlsx):
        """_serialize_xml must not produce auto-generated ns0: prefixes."""
        t = XlsxTranslator()
        result = t.extract(minimal_xlsx)
        translated = t.translate(result, {"ss_0": "Test"})
        ss = translated["archive_data"]["xl/sharedStrings.xml"]
        assert b"ns0:" not in ss
        assert b'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"' in ss

    def test_empty_shared_strings_skipped(self, tmp_path):
        xlsx = tmp_path / "empty.xlsx"
        with zipfile.ZipFile(xlsx, "w") as z:
            z.writestr("[Content_Types].xml", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '</Types>'
            ))
            z.writestr("_rels/.rels", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>'
            ))
            z.writestr("xl/workbook.xml", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
                '</workbook>'
            ))
            z.writestr("xl/_rels/workbook.xml.rels", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '</Relationships>'
            ))
            z.writestr("xl/worksheets/sheet1.xml", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>'
                '</worksheet>'
            ))
        t = XlsxTranslator()
        result = t.extract(xlsx)
        assert len(result["text_units"]) == 0
