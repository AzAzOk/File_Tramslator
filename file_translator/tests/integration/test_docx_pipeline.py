"""Integration tests for DocxTranslator pipeline (Okapi mocked)."""

import re
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from file_translator.domain.models import DocumentFormat
from file_translator.infrastructure.translators.docx_translator import DocxTranslator


# ---------------------------------------------------------------------------
# Fixtures: programmatically-built DOCX files
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_docx(tmp_path) -> Path:
    """Minimal valid DOCX: one paragraph, one run, plain text."""
    path = tmp_path / "test.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _WORD_RELS)
        z.writestr("word/document.xml", _DOC_BODY % "Hello World")
    return path


@pytest.fixture
def docx_with_cjk(tmp_path) -> Path:
    """DOCX with CJK fonts in document.xml."""
    doc_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p>
      <w:r>
        <w:rPr>
          <w:rFonts w:eastAsia="SimSun" w:ascii="SimSun" w:hAnsi="SimSun"/>
        </w:rPr>
        <w:t>翻译文本</w:t>
      </w:r>
    </w:p>
  </w:body>
</w:document>"""
    path = tmp_path / "cjk_test.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _WORD_RELS)
        z.writestr("word/document.xml", doc_xml)
    return path


@pytest.fixture
def docx_with_exact_row_height(tmp_path) -> Path:
    """DOCX with w:hRule='exact' on a table row."""
    doc_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:tbl>
      <w:tr>
        <w:trPr>
          <w:trHeight w:val="500" w:hRule="exact"/>
        </w:trPr>
        <w:tc>
          <w:p>
            <w:r><w:t>Cell text</w:t></w:r>
          </w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>"""
    path = tmp_path / "height_test.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _WORD_RELS)
        z.writestr("word/document.xml", doc_xml)
    return path


@pytest.fixture
def docx_with_both_cjk_and_height(tmp_path) -> Path:
    """DOCX with both CJK fonts and exact row height."""
    doc_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p>
      <w:r>
        <w:rPr>
          <w:rFonts w:eastAsia="SimSun" w:ascii="Arial" w:hAnsi="Arial"/>
        </w:rPr>
        <w:t>标题</w:t>
      </w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:trPr>
          <w:trHeight w:val="300" w:hRule="exact"/>
        </w:trPr>
        <w:tc><w:p><w:r><w:t>数据</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>"""
    path = tmp_path / "both.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _WORD_RELS)
        z.writestr("word/document.xml", doc_xml)
    return path


@pytest.fixture
def docx_multiple_cjk_fonts(tmp_path) -> Path:
    """DOCX with multiple different CJK fonts to test all replacements."""
    doc_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:rPr><w:rFonts w:eastAsia="SimHei"/></w:rPr><w:t>黑体</w:t></w:r></w:p>
    <w:p><w:r><w:rPr><w:rFonts w:eastAsia="MingLiU"/></w:rPr><w:t>細明體</w:t></w:r></w:p>
    <w:p><w:r><w:rPr><w:rFonts w:eastAsia="MS Mincho"/></w:rPr><w:t>明朝</w:t></w:r></w:p>
    <w:p><w:r><w:rPr><w:rFonts w:eastAsia="MS Gothic"/></w:rPr><w:t>ゴシック</w:t></w:r></w:p>
    <w:p><w:r><w:rPr><w:rFonts w:eastAsia="DengXian"/></w:rPr><w:t>等线</w:t></w:r></w:p>
    <w:p><w:r><w:rPr><w:rFonts w:eastAsia="FangSong"/></w:rPr><w:t>仿宋</w:t></w:r></w:p>
    <w:p><w:r><w:rPr><w:rFonts w:eastAsia="KaiTi"/></w:rPr><w:t>楷体</w:t></w:r></w:p>
    <w:p><w:r><w:rPr><w:rFonts w:eastAsia="NSimSun"/></w:rPr><w:t>新宋体</w:t></w:r></w:p>
    <w:p><w:r><w:rPr><w:rFonts w:eastAsia="PMingLiU"/></w:rPr><w:t>新細明體</w:t></w:r></w:p>
  </w:body>
</w:document>"""

    path = tmp_path / "multi_cjk.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _WORD_RELS)
        z.writestr("word/document.xml", doc_xml)
    return path


# ---------------------------------------------------------------------------
# Shared XML boilerplate
# ---------------------------------------------------------------------------

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_WORD_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""

_DOC_BODY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p>
      <w:r>
        <w:t>%s</w:t>
      </w:r>
    </w:p>
  </w:body>
</w:document>"""

_XLIFF_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="test.docx" source-language="en" target-language="ru" datatype="x-ooxml">
    <body>
      <trans-unit id="p_0" translate="yes">
        <source>Hello World</source>
        <target/>
      </trans-unit>
      <trans-unit id="p_1" translate="yes">
        <source><g id="1">Warning</g> — do not exceed limits</source>
        <target/>
      </trans-unit>
      <trans-unit id="p_2" translate="no">
        <source>Do not translate</source>
        <target/>
      </trans-unit>
    </body>
  </file>
</xliff>"""


# ===================================================================
# can_process tests
# ===================================================================

class TestCanProcess:
    def test_valid_docx(self, minimal_docx):
        assert DocxTranslator().can_process(minimal_docx)

    def test_invalid_file(self, tmp_path):
        p = tmp_path / "not_a_docx.docx"
        p.write_text("not a zip")
        assert not DocxTranslator().can_process(p)

    def test_nonexistent_file(self):
        assert not DocxTranslator().can_process(Path("/nonexistent/file.docx"))

    def test_doc_suffix(self, tmp_path):
        p = tmp_path / "test.doc"
        p.write_text("anything")
        assert DocxTranslator().can_process(p)

    def test_unsupported_suffix(self, tmp_path):
        p = tmp_path / "test.pdf"
        p.write_text("anything")
        assert not DocxTranslator().can_process(p)

    def test_docx_without_word_dir(self, tmp_path):
        """DOCX that is a valid zip but missing word/ — should be rejected."""
        p = tmp_path / "invalid.docx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        assert not DocxTranslator().can_process(p)


# ===================================================================
# _post_process_docx tests (real byte-level manipulation)
# ===================================================================

class TestPostProcessDocx:
    def _read_decompressed(self, docx_path: Path, filename: str) -> bytes:
        """Read a file from inside a DOCX ZIP as decompressed bytes."""
        import zipfile
        with zipfile.ZipFile(str(docx_path), "r") as z:
            return z.read(filename)

    def _all_decompressed_text(self, docx_path: Path) -> bytes:
        """Concatenate all decompressed XML/.rels content for searching."""
        import zipfile
        parts = []
        with zipfile.ZipFile(str(docx_path), "r") as z:
            for name in z.namelist():
                if name.lower().endswith((".xml", ".rels")):
                    parts.append(z.read(name))
        return b"\n".join(parts)

    def test_cjk_replaced_with_arial(self, docx_with_cjk):
        DocxTranslator._post_process_docx(docx_with_cjk)
        raw = self._all_decompressed_text(docx_with_cjk)
        assert b'SimSun' not in raw, "SimSun should be replaced"
        assert b'Arial' in raw, "Arial should be present"

    def test_exact_height_changed_to_atleast(self, docx_with_exact_row_height):
        DocxTranslator._post_process_docx(docx_with_exact_row_height)
        raw = self._all_decompressed_text(docx_with_exact_row_height)
        assert b'w:hRule="exact"' not in raw, "exact should be replaced"
        assert b'w:hRule="atLeast"' in raw

    def test_both_fixes_applied(self, docx_with_both_cjk_and_height):
        DocxTranslator._post_process_docx(docx_with_both_cjk_and_height)
        raw = self._all_decompressed_text(docx_with_both_cjk_and_height)
        assert b'SimSun' not in raw
        assert b'Arial' in raw
        assert b'w:hRule="exact"' not in raw
        assert b'w:hRule="atLeast"' in raw

    def test_all_cjk_fonts_replaced(self, docx_multiple_cjk_fonts):
        DocxTranslator._post_process_docx(docx_multiple_cjk_fonts)
        raw = self._all_decompressed_text(docx_multiple_cjk_fonts)
        for font in (b'SimSun', b'SimHei', b'MingLiU', b'MS Mincho',
                     b'MS Gothic', b'DengXian', b'FangSong', b'KaiTi',
                     b'NSimSun', b'PMingLiU'):
            assert font not in raw, f"{font} should be replaced"
        arial_count = raw.count(b'Arial')
        assert arial_count >= 9, f"Expected at least 9 Arial, got {arial_count}"

    def test_no_changes_needed(self, minimal_docx):
        """No CJK fonts or exact heights — no modifications."""
        original = minimal_docx.read_bytes()
        DocxTranslator._post_process_docx(minimal_docx)
        assert minimal_docx.read_bytes() == original

    def test_nonexistent_file(self, tmp_path):
        p = tmp_path / "nonexistent.docx"
        DocxTranslator._post_process_docx(p)

    def test_non_docx_zip(self, tmp_path):
        """ZIP file without XML — should not crash."""
        p = tmp_path / "not_docx.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("readme.txt", "hello")
        DocxTranslator._post_process_docx(p)

    def test_cjk_in_rels_file(self, tmp_path):
        """CJK font attributes inside XML files are replaced (rels files pass through)."""
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"'
            ' Target="word/styles.xml"/>'
            '</Relationships>'
        )
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body>'
            '<w:p><w:r><w:rPr><w:rFonts w:eastAsia="SimSun"/></w:rPr><w:t>\u6587\u672c</w:t></w:r></w:p>'
            '</w:body>'
            '</w:document>'
        )
        p = tmp_path / "rels_cjk.docx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("[Content_Types].xml", _CONTENT_TYPES)
            z.writestr("_rels/.rels", rels_xml)
            z.writestr("word/_rels/document.xml.rels", _WORD_RELS)
            z.writestr("word/document.xml", doc_xml)
        DocxTranslator._post_process_docx(p)
        raw = self._all_decompressed_text(p)
        assert b'SimSun' not in raw
        assert b'Arial' in raw


# ===================================================================
# Pipeline integration tests (OkapiService mocked)
# ===================================================================

class TestDocxPipeline:
    """Full extract → translate → save cycle with mocked Tikal."""

    @pytest.fixture
    def mock_okapi(self):
        okapi = MagicMock()
        okapi.check_available.return_value = True
        return okapi

    @pytest.fixture
    def translator(self, mock_okapi):
        return DocxTranslator(okapi_service=mock_okapi)

    def test_extract_creates_temp_dir_and_returns_structure(self, translator, mock_okapi, minimal_docx):
        mock_okapi.extract_to_xliff.return_value = Path("/fake/xliff.xlf")
        mock_okapi.load_xliff.return_value = []

        result = translator.extract(minimal_docx, source_lang="en", target_lang="ru")

        assert "text_units" in result
        assert "metadata" in result
        assert "temp_dir" in result
        assert "xliff_path" in result
        assert Path(result["temp_dir"]).exists()
        mock_okapi.extract_to_xliff.assert_called_once_with(
            minimal_docx, Path(result["temp_dir"]),
            source_lang="en", target_lang="ru",
        )

    def test_extract_with_doc_conversion(self, translator, mock_okapi, tmp_path):
        """.doc file triggers LibreOffice conversion before Tikal."""
        with patch(
            "file_translator.infrastructure.converters.doc_to_docx_converter.LibreOfficeConverter.convert"
        ) as mock_convert:
            doc_path = tmp_path / "test.doc"
            doc_path.write_text("dummy doc")
            converted_docx = tmp_path / "test.docx"
            converted_docx.write_text("dummy docx")
            mock_convert.return_value = converted_docx

            mock_okapi.extract_to_xliff.return_value = Path("/fake/xliff.xlf")
            mock_okapi.load_xliff.return_value = []

            result = translator.extract(doc_path, source_lang="en", target_lang="ru")

            mock_convert.assert_called_once()
            mock_okapi.extract_to_xliff.assert_called_once_with(
                converted_docx, Path(result["temp_dir"]),
                source_lang="en", target_lang="ru",
            )

    def test_extract_skips_whitespace_units(self, translator, mock_okapi, minimal_docx):
        from file_translator.infrastructure.translators.okapi_service import XliffUnit

        mock_okapi.extract_to_xliff.return_value = Path("/fake/xliff.xlf")
        mock_okapi.load_xliff.return_value = [
            XliffUnit(id="p_0", source_text="Hello World"),
            XliffUnit(id="p_1", source_text="   "),
            XliffUnit(id="p_2", source_text="Another text"),
        ]

        result = translator.extract(minimal_docx)
        units = result["text_units"]

        assert len(units) == 2
        assert units[0].id == "p_0"
        assert units[1].id == "p_2"

    def test_translate_updates_xliff(self, translator, mock_okapi, minimal_docx):
        mock_okapi.extract_to_xliff.return_value = Path("/fake/xliff.xlf")
        mock_okapi.load_xliff.return_value = []

        extracted = translator.extract(minimal_docx)
        translations = {"p_0": "Привет мир"}
        result = translator.translate(extracted, translations)

        mock_okapi.save_xliff.assert_called_once_with(
            Path("/fake/xliff.xlf"), translations,
        )
        assert result["translations_applied"] == 1

    def test_translate_error_logged(self, translator, mock_okapi, minimal_docx):
        from file_translator.infrastructure.translators.okapi_service import OkapiServiceError

        mock_okapi.extract_to_xliff.return_value = Path("/fake/xliff.xlf")
        mock_okapi.load_xliff.return_value = []
        mock_okapi.save_xliff.side_effect = OkapiServiceError("XLIFF error")

        extracted = translator.extract(minimal_docx)
        result = translator.translate(extracted, {"p_0": "text"})

        assert "errors" in result
        assert "XLIFF error" in result["errors"]

    def test_save_calls_merge_and_post_process(self, translator, mock_okapi, tmp_path):
        mock_okapi.merge_from_xliff.return_value = tmp_path / "output.docx"

        translated_data = {"xliff_path": str(tmp_path / "input.xlf")}
        output_path = tmp_path / "output.docx"

        translator.save(translated_data, output_path)

        mock_okapi.merge_from_xliff.assert_called_once()

    def test_save_tikal_not_available(self, translator, mock_okapi, tmp_path):
        from file_translator.domain.errors import SaveDocumentError
        from file_translator.infrastructure.translators.okapi_service import TikalNotAvailableError

        mock_okapi.merge_from_xliff.side_effect = TikalNotAvailableError("not found")

        translated_data = {"xliff_path": str(tmp_path / "input.xlf")}
        output_path = tmp_path / "output.docx"

        with pytest.raises(SaveDocumentError):
            translator.save(translated_data, output_path)

    def test_extract_tikal_not_available(self, translator, mock_okapi, minimal_docx):
        from file_translator.domain.errors import DocumentParseError
        from file_translator.infrastructure.translators.okapi_service import TikalNotAvailableError

        mock_okapi.extract_to_xliff.side_effect = TikalNotAvailableError("not found")

        with pytest.raises(DocumentParseError):
            translator.extract(minimal_docx)


# ===================================================================
# Pipeline: E2E with all mocked (requires Tikal for real merge)
# ===================================================================

class TestDocxPipelineFullMock:
    """Full pipeline from end to end with all dependencies mocked."""

    @pytest.fixture
    def mock_okapi(self):
        okapi = MagicMock()
        okapi.check_available.return_value = True
        # Simulate a 2-unit extraction
        from file_translator.infrastructure.translators.okapi_service import XliffUnit
        okapi.load_xliff.return_value = [
            XliffUnit(id="p_0", source_text="Hello World"),
            XliffUnit(id="p_1", source_text="Second paragraph"),
        ]
        return okapi

    def test_full_pipeline(self, mock_okapi, minimal_docx, tmp_path):
        translator = DocxTranslator(okapi_service=mock_okapi)

        # Extract
        extracted = translator.extract(minimal_docx, source_lang="en", target_lang="ru")
        assert len(extracted["text_units"]) == 2
        assert extracted["text_units"][0].original_text == "Hello World"

        # Translate
        translations = {"p_0": "Привет мир", "p_1": "Второй абзац"}
        translated_data = translator.translate(extracted, translations)
        assert translated_data["translations_applied"] == 2

        # Save
        output = tmp_path / "translated.docx"
        translator.save(translated_data, output)

        mock_okapi.extract_to_xliff.assert_called_once()
        mock_okapi.save_xliff.assert_called_once()
        mock_okapi.merge_from_xliff.assert_called_once()

    def test_pipeline_incomplete_translation(self, mock_okapi, minimal_docx, tmp_path):
        """Some units from LLM are missing — pipeline should still proceed."""
        translator = DocxTranslator(okapi_service=mock_okapi)
        extracted = translator.extract(minimal_docx)

        translations = {"p_0": "Привет мир"}
        translated_data = translator.translate(extracted, translations)

        assert translated_data["translations_applied"] == 1

        output = tmp_path / "partial.docx"
        translator.save(translated_data, output)

    def test_pipeline_cleans_up_temp_dir(self, mock_okapi, minimal_docx, tmp_path):
        translator = DocxTranslator(okapi_service=mock_okapi)
        extracted = translator.extract(minimal_docx)
        temp_dir = Path(extracted["temp_dir"])
        assert temp_dir.exists()

        translations = {"p_0": "Привет мир", "p_1": "Второй абзац"}
        translated_data = translator.translate(extracted, translations)
        output = tmp_path / "cleaned.docx"
        translator.save(translated_data, output)

        assert not temp_dir.exists(), "Temp dir should be cleaned up after save"
