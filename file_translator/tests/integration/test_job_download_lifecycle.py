"""API-level lifecycle tests for the download endpoint.

Covers the completed-job lifecycle change (fix-ghost-completed-jobs):
after a result is downloaded, the completed Redis record is removed after a
short grace so the job does not reappear as a ghost card on reload; and a
stale completed record whose output file is already gone yields 404 and is
deleted. Tests invoke the FastAPI route handlers directly (no live server)
with a fake `translation_service` / job manager, mirroring the
failed-job-handling grace tests pattern.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Must be set before importing app (it fails at import without these).
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("GLOSSARY_DB_PASSWORD", "test-pass")

from fastapi import BackgroundTasks

import file_translator.presentation.api.app as api_app
from file_translator.domain.job import Job, JobStatus


class FakeJobRepository:
    """Minimal JobRepository fake tracking deletions."""

    def __init__(self, jobs: list[Job]) -> None:
        self.jobs = {job.job_id: job for job in jobs}
        self.deleted: list[str] = []

    async def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def update(self, job: Job) -> Job | None:
        self.jobs[job.job_id] = job
        return job

    async def delete(self, job_id: str) -> bool:
        if job_id in self.jobs:
            del self.jobs[job_id]
            self.deleted.append(job_id)
            return True
        return False

    async def recent(self, limit: int = 50) -> list[Job]:
        return list(self.jobs.values())[:limit]


class FakeJobManager:
    """Fake translation-service job manager backed by FakeJobRepository."""

    def __init__(self, jobs: list[Job]) -> None:
        self.repository = FakeJobRepository(jobs)

    async def get_job(self, job_id: str) -> Job | None:
        return await self.repository.get(job_id)

    async def delete_job(self, job_id: str) -> bool:
        return await self.repository.delete(job_id)

    async def get_recent_jobs(self, limit: int = 50) -> list[Job]:
        return await self.repository.recent(limit)


def make_job(job_id: str, output_file_path: str) -> Job:
    return Job(
        job_id=job_id,
        user_id="admin",
        status=JobStatus.COMPLETED,
        filename=f"{job_id}.docx",
        output_file_path=output_file_path,
    )


def make_admin_request() -> SimpleNamespace:
    """Request whose auth is an admin (bypasses the owner check)."""
    user = SimpleNamespace(
        username="admin",
        user_id="admin",
        has_permission=lambda perm: True,
    )
    auth = SimpleNamespace(user=user)
    return SimpleNamespace(state=SimpleNamespace(auth=auth))


def patch_translation_service(monkeypatch: pytest.MonkeyPatch, jobs: list[Job]) -> FakeJobManager:
    manager = FakeJobManager(jobs)
    monkeypatch.setattr(api_app, "translation_service", SimpleNamespace(job_manager=manager))
    return manager


def patch_short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the grace sleep so tests do not wait the production 60 s."""
    real = api_app._delete_job_after_grace

    async def _fast(job_id: str, grace: float = 0) -> None:
        await real(job_id, grace=0)

    monkeypatch.setattr(api_app, "_delete_job_after_grace", _fast)


@pytest.mark.asyncio
async def test_download_schedules_record_deletion_after_grace(tmp_path, monkeypatch) -> None:
    out = tmp_path / "translated.docx"
    out.write_bytes(b"downloaded content")
    manager = patch_translation_service(monkeypatch, [make_job("job-1", str(out))])
    patch_short_grace(monkeypatch)

    resp = await api_app.download_job_result(
        "job-1", make_admin_request(), BackgroundTasks(), MagicMock()  # type: ignore[arg-type]
    )

    # Response contract unchanged: file is served.
    assert isinstance(resp, api_app.FileResponse)
    assert resp.background is not None

    # The new task only removes the Redis record — the served file is untouched.
    await asyncio.sleep(0.2)
    assert "job-1" in manager.repository.deleted
    assert await manager.repository.get("job-1") is None
    assert out.exists()

    # The job is no longer returned by GET /jobs (ghost card impossible).
    recent = await api_app.list_jobs(MagicMock())
    assert all(schema.job_id != "job-1" for schema in recent)


@pytest.mark.asyncio
async def test_download_stale_record_returns_404_and_deletes(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "already-deleted.docx"  # file is gone, record remains
    manager = patch_translation_service(monkeypatch, [make_job("job-2", str(missing))])

    with pytest.raises(api_app.HTTPException) as exc_info:
        await api_app.download_job_result(
            "job-2", make_admin_request(), BackgroundTasks(), MagicMock()  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 404
    assert "не найден" in str(exc_info.value.detail)
    assert "job-2" in manager.repository.deleted
    assert await manager.repository.get("job-2") is None