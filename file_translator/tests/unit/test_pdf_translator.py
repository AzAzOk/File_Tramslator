"""Unit tests for PdfTranslator — composition over DocxTranslator.

PdfTranslator converts PDF→DOCX (PdfToDocxConverter) and then delegates to an
internal DocxTranslator. Both collaborators are injected, so these tests use
plain mocks and never touch Tikal / the converter service.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from file_translator.domain.errors import ConversionError, ConversionTimeoutError, DocumentOpenError
from file_translator.domain.models import DocumentFormat
from file_translator.infrastructure.translators.pdf_translator import PdfTranslator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_translator(converted: Path | None = None, extract_result: dict | None = None):
    converter = MagicMock()
    if converted is not None:
        converter.convert_sync.return_value = converted
    docx = MagicMock()
    docx.extract.return_value = extract_result or {
        "text_units": [],
        "temp_dir": "/tmp/docx_okapi_fake",
        "xliff_path": "/tmp/docx_okapi_fake/file.xlf",
    }
    return PdfTranslator(converter=converter, docx_translator=docx), converter, docx


@pytest.fixture
def pdf_file(tmp_path) -> Path:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4")
    return p


# ---------------------------------------------------------------------------
# 8.2 can_process / supported formats
# ---------------------------------------------------------------------------

class TestCanProcess:
    def test_supported_formats(self):
        assert PdfTranslator.supported_formats() == {DocumentFormat.PDF}
        assert PdfTranslator.SUPPORTED_FORMATS == {DocumentFormat.PDF}

    def test_accepts_pdf(self, pdf_file):
        assert PdfTranslator().can_process(pdf_file)

    def test_rejects_docx(self, tmp_path):
        p = tmp_path / "a.docx"
        p.write_bytes(b"x")
        assert not PdfTranslator().can_process(p)

    def test_rejects_txt(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("x")
        assert not PdfTranslator().can_process(p)

    def test_rejects_nonexistent(self, tmp_path):
        assert not PdfTranslator().can_process(tmp_path / "missing.pdf")


# ---------------------------------------------------------------------------
# 8.2 extract delegation
# ---------------------------------------------------------------------------

class TestExtract:
    def test_delegates_to_converter_and_docx(self, pdf_file, tmp_path):
        converted = tmp_path / "converted.docx"
        converted.write_bytes(b"docx")
        translator, converter, docx = _make_translator(converted=converted)

        result = translator.extract(pdf_file, source_lang="en", target_lang="ru")

        # Converter invoked with a pdf_convert_* temp dir.
        converter.convert_sync.assert_called_once()
        called_pdf, called_dir = converter.convert_sync.call_args.args
        assert called_pdf == pdf_file
        assert Path(called_dir).name.startswith("pdf_convert_")

        # Docx half receives the converted DOCX.
        docx.extract.assert_called_once_with(
            converted, source_lang="en", target_lang="ru"
        )

        # Return dict is passed through, plus temp-dir bookkeeping.
        assert result["pdf_convert_dir"] == str(called_dir)
        assert result["converted_docx"] == str(converted)
        assert result["temp_dir"] == "/tmp/docx_okapi_fake"
        assert Path(result["pdf_convert_dir"]).exists()

    def test_conversion_error_wrapped_as_document_open_error(self, pdf_file):
        translator, converter, _ = _make_translator()
        converter.convert_sync.side_effect = ConversionError(
            "Сервис конвертации недоступен", status_code=0,
            error_code="CONVERTER_UNREACHABLE",
        )

        with pytest.raises(DocumentOpenError) as exc:
            translator.extract(pdf_file)

        assert "Сервис конвертации недоступен" in str(exc.value)
        # Failed conversion must not leak its temp dir / reference.
        assert translator._temp_dir is None

    def test_timeout_error_also_wrapped(self, pdf_file):
        translator, converter, _ = _make_translator()
        converter.convert_sync.side_effect = ConversionTimeoutError("Превышено время конвертации PDF")

        with pytest.raises(DocumentOpenError) as exc:
            translator.extract(pdf_file)

        assert "Превышено время конвертации PDF" in str(exc.value)
        assert translator._temp_dir is None


# ---------------------------------------------------------------------------
# 8.2 translate / save delegation
# ---------------------------------------------------------------------------

class TestDelegate:
    def test_translate_delegates(self):
        translator, _, docx = _make_translator()
        docx.translate.return_value = {"translations_applied": 3}

        result = translator.translate({"xliff_path": "x"}, {"p_0": "t"}, supports_tags=True)

        docx.translate.assert_called_once_with({"xliff_path": "x"}, {"p_0": "t"}, True)
        assert result == {"translations_applied": 3}

    def test_save_delegates_and_cleans_up(self, tmp_path):
        translator, _, docx = _make_translator()
        out = tmp_path / "out.docx"
        docx.save.return_value = out

        conv_dir = Path(tempfile.mkdtemp(prefix="pdf_convert_test_"))
        translator._temp_dir = conv_dir
        assert conv_dir.exists()

        result = translator.save({"xliff_path": "x"}, out)

        assert result == out
        docx.save.assert_called_once_with({"xliff_path": "x"}, out)
        assert not conv_dir.exists(), "conversion temp dir should be removed after save"
        assert translator._temp_dir is None
