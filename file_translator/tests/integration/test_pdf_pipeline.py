"""Integration test for the PDF → DOCX pipeline.

The First PDF converter service is mocked (no live VM), while the Okapi DOCX
half is exercised through a mocked ``OkapiService`` exactly like
``test_docx_pipeline.py`` — a real Tikal merge would require the CLI, which is
not present in CI. The test asserts the whole composition:

    PdfTranslator.extract → conversion stage progress → DocxTranslator
    translate/save → valid .docx output → temp dirs cleaned up.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from file_translator.domain.job import Job, ProcessingStage, _STAGE_WEIGHTS
from file_translator.infrastructure.translators.docx_translator import DocxTranslator
from file_translator.infrastructure.translators.okapi_service import XliffUnit
from file_translator.infrastructure.translators.pdf_translator import PdfTranslator


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
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:body>
</w:document>"""


def _write_minimal_docx(path: Path, text: str = "Hello World") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _WORD_RELS)
        z.writestr("word/document.xml", _DOC_BODY % text)
    return path


@pytest.fixture
def mock_okapi():
    okapi = MagicMock()
    okapi.check_available.return_value = True
    okapi.load_xliff.return_value = [
        XliffUnit(id="p_0", source_text="Hello World"),
        XliffUnit(id="p_1", source_text="Second paragraph"),
    ]
    return okapi


@pytest.fixture
def converted_docx(tmp_path) -> Path:
    return _write_minimal_docx(tmp_path / "converted.docx")


@pytest.fixture
def pdf_path(tmp_path) -> Path:
    p = tmp_path / "input.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def _make_pdf_translator(converted_docx: Path, mock_okapi) -> PdfTranslator:
    converter = MagicMock()
    converter.convert_sync.return_value = converted_docx
    docx_translator = DocxTranslator(okapi_service=mock_okapi)
    return PdfTranslator(converter=converter, docx_translator=docx_translator)


class TestPdfPipeline:
    def test_extract_returns_units_and_records_temp_dirs(self, converted_docx, mock_okapi, pdf_path):
        translator = _make_pdf_translator(converted_docx, mock_okapi)

        extracted = translator.extract(pdf_path, source_lang="en", target_lang="ru")

        assert len(extracted["text_units"]) == 2
        assert extracted["text_units"][0].original_text == "Hello World"

        pdf_dir = Path(extracted["pdf_convert_dir"])
        docx_dir = Path(extracted["temp_dir"])
        assert pdf_dir.exists() and pdf_dir.name.startswith("pdf_convert_")
        assert docx_dir.exists() and docx_dir.name.startswith("docx_okapi_")
        assert extracted["converted_docx"] == str(converted_docx)

    def test_conversion_stage_progress_is_interpolated(self):
        """The service sets CONVERSION with (0, deadline); progress lands
        between the VALIDATION and EXTRACTION weights."""
        job = Job(job_id="job-pdf")
        job.update_progress(ProcessingStage.CONVERSION, 0, 120)

        assert job.current_stage == ProcessingStage.CONVERSION
        assert _STAGE_WEIGHTS[ProcessingStage.VALIDATION] <= job.progress
        assert job.progress <= _STAGE_WEIGHTS[ProcessingStage.EXTRACTION]

    def test_full_pipeline_produces_valid_docx_and_cleans_temp_dirs(
        self, converted_docx, mock_okapi, pdf_path, tmp_path
    ):
        # Make the mocked merge actually emit a DOCX so post-processing and the
        # validity assertion below operate on a real archive.
        def _merge(xliff_path, output_path, original_path=None):
            _write_minimal_docx(Path(output_path), "Привет мир")
            return Path(output_path)

        mock_okapi.merge_from_xliff.side_effect = _merge

        translator = _make_pdf_translator(converted_docx, mock_okapi)

        extracted = translator.extract(pdf_path, source_lang="en", target_lang="ru")
        pdf_dir = Path(extracted["pdf_convert_dir"])
        docx_dir = Path(extracted["temp_dir"])

        translated = translator.translate(
            extracted, {"p_0": "Привет мир", "p_1": "Второй абзац"}
        )
        output = tmp_path / "translated.docx"
        translator.save(translated, output)

        # Output is a valid DOCX archive containing the document part.
        assert output.exists()
        assert zipfile.is_zipfile(output)
        with zipfile.ZipFile(output) as z:
            assert "word/document.xml" in z.namelist()

        # Both translator temp dirs are gone after save.
        assert not pdf_dir.exists()
        assert not docx_dir.exists()
