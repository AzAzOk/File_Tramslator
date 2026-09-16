"""API-level tests for PDF acceptance (no live server).

Handlers are invoked directly with fake auth state and patched module-level
singletons, asserting the HTTP contract:

- ``POST /jobs`` accepts ``.pdf`` and reaches job creation;
- ``POST /jobs/batch`` accepts ``.pdf``;
- ``POST /jobs`` still rejects unsupported extensions;
- ``GET /supported-formats`` lists ``pdf`` under ``formats`` (not ``coming_soon``).
"""

from __future__ import annotations

import io
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Must be set before importing app (it fails at import without these).
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("GLOSSARY_DB_PASSWORD", "test-pass")

import file_translator.presentation.api.app as api_app
from file_translator.domain.job import Job, JobStatus
from starlette.datastructures import Headers, UploadFile


def _make_request(user_id: str = "u1") -> SimpleNamespace:
    auth = SimpleNamespace(
        user=SimpleNamespace(user_id=user_id, username="tester", ldap_groups=["all"])
    )
    return SimpleNamespace(state=SimpleNamespace(auth=auth))


def _make_upload(filename: str, content: bytes = b"%PDF-1.4 fake") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": "application/pdf"}),
    )


def _patch_service(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    job = Job(job_id="job-pdf-1", user_id="u1", status=JobStatus.PENDING)
    job_manager = SimpleNamespace(
        create_job=AsyncMock(return_value=job),
        repository=SimpleNamespace(update=AsyncMock()),
    )
    monkeypatch.setattr(
        api_app, "translation_service", SimpleNamespace(job_manager=job_manager)
    )
    queue = SimpleNamespace(enqueue=AsyncMock(return_value=0))
    monkeypatch.setattr(api_app, "user_job_queue", queue)
    return queue


_JOBS_KWARGS = dict(
    source_language="en",
    target_language="ru",
    translation_style="technical",
    translation_mode="full",
    use_glossary=False,
    collection_id="",
    batch_size=50,
)


@pytest.mark.asyncio
async def test_post_jobs_accepts_pdf_and_reaches_job_creation(monkeypatch):
    queue = _patch_service(monkeypatch)

    resp = await api_app.create_translation_job(
        request=_make_request(), _=None, file=_make_upload("document.pdf"), **_JOBS_KWARGS
    )

    assert resp.job_id == "job-pdf-1"
    assert resp.status == "pending"
    queue.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_jobs_rejects_unsupported_extension(monkeypatch):
    _patch_service(monkeypatch)

    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.create_translation_job(
            request=_make_request(), _=None, file=_make_upload("notes.txt"), **_JOBS_KWARGS
        )

    assert exc.value.status_code == 400
    assert "PDF" in exc.value.detail


@pytest.mark.asyncio
async def test_post_jobs_batch_accepts_pdf(monkeypatch):
    queue = _patch_service(monkeypatch)

    resp = await api_app.create_batch_translation_jobs(
        request=_make_request(), _=None, files=[_make_upload("a.pdf")], **_JOBS_KWARGS
    )

    assert resp.total == 1
    assert resp.jobs[0].filename == "a.pdf"
    queue.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_supported_formats_lists_pdf():
    result = await api_app.supported_formats()

    assert "pdf" in result["formats"]
    assert "pdf" not in result["coming_soon"]
