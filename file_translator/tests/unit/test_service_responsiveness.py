"""Tests for API/service responsiveness during blocking translation phases.

The service must offload blocking translator work (PDF conversion HTTP, Tikal
CLI) off the event loop so health checks and other requests keep answering
(fix-service-concurrency).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from file_translator.application.schemas import TranslationRequestSchema
from file_translator.application.service import TranslationService
from file_translator.application.user_queue import UserJobQueue
from file_translator.domain.models import TextUnit

BLOCK_SECONDS = 0.6


def _request() -> TranslationRequestSchema:
    return TranslationRequestSchema(
        source_language="ru", target_language="en", translation_style="mixed",
        translation_mode="full", use_glossary=False, batch_size=50,
    )


def _blocking_translator():
    """A translator whose extract() blocks (simulates PDF conversion/Tikal)."""
    t = MagicMock()
    t.can_process.return_value = True

    def extract(input_path, source_lang="en", target_lang="ru"):
        time.sleep(BLOCK_SECONDS)

        def export(file_path, source_lang="en", target_lang="ru"):
            pass
        return {
            "text_units": [TextUnit(id="p_0", original_text="Hello world")],
            "temp_dir": "/tmp/resp_test",
            "source_origin": "native",
        }

    t.extract.side_effect = extract
    t.translate.return_value = {"translations_applied": 1}

    def save(translated_data, output_path):
        Path(output_path).write_bytes(b"out")
        return output_path

    t.save.side_effect = save
    return t


def _service(mock_llm_provider) -> TranslationService:
    s = TranslationService(
        provider=mock_llm_provider,
        journal_service=MagicMock(),
        job_manager=MagicMock(),
        validation_chain=MagicMock(),
    )
    s.job_manager.start_job = AsyncMock(return_value=MagicMock(job_id="jr"))
    s.job_manager.update_progress = AsyncMock()
    s.job_manager.is_cancelled = AsyncMock(return_value=False)
    s.job_manager.get_job = AsyncMock(return_value=MagicMock(metadata={}))
    s.job_manager.repository = MagicMock()
    s.job_manager.repository.update = AsyncMock()
    s.job_manager.complete_job = AsyncMock()
    s.validation_chain.validate_all = AsyncMock(
        return_value=MagicMock(passed=True, warnings=[], error_messages=[],
                               warning_messages=[])
    )
    s.journal_service.log_info = AsyncMock()
    s.journal_service.log_error = AsyncMock()
    s.journal_service.log_warning = AsyncMock()
    s.journal_service.cleanup_old_journals = AsyncMock()
    async def _reg(job_id, extracted_data):
        pass
    s._register_temp_dirs = _reg
    return s


@pytest.mark.asyncio
async def test_loop_stays_free_during_blocking_extract(tmp_path, mock_llm_provider):
    """While extract() blocks (conversion), a probe await completes fast.

    Without the to_thread offload this test fails: the probe would wait for
    the full BLOCK_SECONDS because the loop is occupied.
    """
    docx = tmp_path / "doc.docx"
    docx.write_bytes(b"dummy")

    service = _service(mock_llm_provider)
    with patch.object(service, "_find_translator") as find:
        find.return_value = _blocking_translator()

        job_task = asyncio.create_task(
            service.translate_document(str(docx), _request(), job_id="jr")
        )
        await asyncio.sleep(0.1)  # let extract enter its blocking sleep
        t0 = time.monotonic()
        await asyncio.sleep(0.05)  # health-check-style probe on the loop
        probe_elapsed = time.monotonic() - t0

        result = await job_task

    assert probe_elapsed < 0.25, (
        f"loop was blocked: probe took {probe_elapsed:.2f}s"
    )
    assert result is not None and result.success
    assert result.duration_seconds >= BLOCK_SECONDS * 0.8


@pytest.mark.asyncio
async def test_concurrent_request_answers_during_job(tmp_path, mock_llm_provider):
    """Two 'requests' interleave: a health-style await runs during the job's
    blocking phase, then the job completes normally."""
    docx = tmp_path / "doc.docx"
    docx.write_bytes(b"dummy")

    service = _service(mock_llm_provider)
    with patch.object(service, "_find_translator") as find:
        find.return_value = _blocking_translator()

        job_task = asyncio.create_task(
            service.translate_document(str(docx), _request(), job_id="jr")
        )
        await asyncio.sleep(0.15)
        # Second request (e.g., another user polling /job or health):
        await asyncio.sleep(0.05)
        result = await job_task

    assert result is not None and result.success


@pytest.mark.asyncio
async def test_user_queue_fifo_order_preserved(tmp_path, mock_llm_provider):
    """Per-user FIFO serialization is unchanged by the offload."""
    order: list[str] = []

    async def process(job_id, file_path, request):
        order.append(job_id)

    queue = UserJobQueue(process_func=process)
    await queue.enqueue("u1", "j1", "f", None)
    await queue.enqueue("u1", "j2", "f", None)
    await queue.enqueue("u1", "j3", "f", None)

    for _ in range(100):
        if len(order) == 3:
            break
        await asyncio.sleep(0.01)

    assert order == ["j1", "j2", "j3"]