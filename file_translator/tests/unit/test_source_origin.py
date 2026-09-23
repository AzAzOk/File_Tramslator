"""Tests for the ``source_origin`` metadata contract.

Requirement: Source-origin metadata — every translation job records the origin
of its input in extracted metadata: ``native`` for DOCX/DOC inputs and
``pdf_converted`` for PDF inputs that were converted to DOCX before
translation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from file_translator.application.schemas import TranslationRequestSchema
from file_translator.application.service import TranslationService
from file_translator.domain.models import LanguageCode, TextUnit


def _native_translator():
    """DocxTranslator stand-in: extract() returns dict WITHOUT source_origin
    (as the real DocxTranslator does today — service must default to native)."""
    translator = MagicMock()
    translator.can_process.return_value = True
    translator.extract.return_value = {
        "text_units": [
            TextUnit(id="p_0", original_text="Hello", translated_text="Привет"),
        ],
        "temp_dir": "/tmp/docx_okapi_native",
    }
    translator.translate.return_value = {"translations_applied": 1}
    translator.save.return_value = MagicMock()
    return translator


async def _run_translate(service: TranslationService, file_path: str,
                         request: TranslationRequestSchema):
    with patch.object(service, "_find_translator") as find:
        find.return_value = _native_translator()
        return await service.translate_document(file_path, request, job_id="j1")


@pytest.mark.asyncio
async def test_docx_job_records_native_origin(tmp_path, mock_llm_provider):
    docx = tmp_path / "doc.docx"
    docx.write_bytes(b"dummy")
    service = TranslationService(
        provider=mock_llm_provider,
        journal_service=MagicMock(),
        job_manager=MagicMock(),
        validation_chain=MagicMock(),
    )
    service.job_manager.start_job = AsyncMock(return_value=MagicMock(job_id="j1"))
    service.job_manager.update_progress = AsyncMock()
    service.job_manager.is_cancelled = AsyncMock(return_value=False)
    service.job_manager.get_job = AsyncMock(return_value=MagicMock(metadata={}))
    service.job_manager.repository = MagicMock()
    service.job_manager.repository.update = AsyncMock()
    service.job_manager.complete_job = AsyncMock()
    service.validation_chain.validate_all = AsyncMock(
        return_value=MagicMock(passed=True, warnings=[], error_messages=[],
                               warning_messages=[])
    )
    service.journal_service.log_info = AsyncMock()
    service.journal_service.log_error = AsyncMock()
    service.journal_service.log_warning = AsyncMock()
    service.journal_service.cleanup_old_journals = AsyncMock()

    request = TranslationRequestSchema(
        source_language="en", target_language="ru", translation_style="technical",
        translation_mode="full", use_glossary=False, batch_size=50,
    )

    # Capture extracted_data before the metadata registration: patch
    # _register_temp_dirs to observe the dict after extract+default.
    captured = {}
    async def _capture(job_id, extracted_data):
        captured.update(extracted_data)
    service._register_temp_dirs = _capture

    result = await _run_translate(service, str(docx), request)
    assert result is not None
    assert captured.get("source_origin") == "native"


@pytest.mark.asyncio
async def test_pdf_job_records_pdf_converted_origin(tmp_path, mock_llm_provider):
    """PDF job: PdfTranslator sets pdf_converted; service must not overwrite."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    # PdfTranslator.extract returns source_origin=pdf_converted (see
    # test_pdf_translator.py for the unit-level contract). Here we emulate a
    # translator that returns it, and confirm service retains it.
    translator = MagicMock()
    translator.can_process.return_value = True
    translator.extract.return_value = {
        "text_units": [
            TextUnit(id="p_0", original_text="Hello", translated_text="Привет"),
        ],
        "temp_dir": "/tmp/docx_okapi_pdf",
        "pdf_convert_dir": "/tmp/pdf_convert_x",
        "converted_docx": "/tmp/pdf_convert_x/doc.docx",
        "source_origin": "pdf_converted",
    }
    translator.translate.return_value = {"translations_applied": 1}
    translator.save.return_value = MagicMock()

    service = TranslationService(
        provider=mock_llm_provider,
        journal_service=MagicMock(),
        job_manager=MagicMock(),
        validation_chain=MagicMock(),
    )
    service.job_manager.start_job = AsyncMock(return_value=MagicMock(job_id="j2"))
    service.job_manager.update_progress = AsyncMock()
    service.job_manager.is_cancelled = AsyncMock(return_value=False)
    service.job_manager.get_job = AsyncMock(return_value=MagicMock(metadata={}))
    service.job_manager.repository = MagicMock()
    service.job_manager.repository.update = AsyncMock()
    service.job_manager.complete_job = AsyncMock()
    service.validation_chain.validate_all = AsyncMock(
        return_value=MagicMock(passed=True, warnings=[], error_messages=[],
                               warning_messages=[])
    )
    service.journal_service.log_info = AsyncMock()
    service.journal_service.log_error = AsyncMock()
    service.journal_service.log_warning = AsyncMock()
    service.journal_service.cleanup_old_journals = AsyncMock()

    captured = {}
    async def _capture(job_id, extracted_data):
        captured.update(extracted_data)
    service._register_temp_dirs = _capture

    request = TranslationRequestSchema(
        source_language="en", target_language="ru", translation_style="technical",
        translation_mode="full", use_glossary=False, batch_size=50,
    )

    with patch.object(service, "_find_translator") as find:
        find.return_value = translator
        result = await service.translate_document(str(pdf), request, job_id="j2")

    assert result is not None
    assert captured.get("source_origin") == "pdf_converted"