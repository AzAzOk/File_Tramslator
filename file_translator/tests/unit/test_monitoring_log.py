"""Unit tests for the durable job-monitoring log.

Covers the domain models, the file-backed repository, the monitoring service,
and the ``JobManager`` lifecycle hooks (change ``job-monitoring-history``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from file_translator.application.job_manager import JobManager
from file_translator.application.monitoring_log_service import MonitoringLogService
from file_translator.domain.job import Job, JobStatus
from file_translator.domain.monitoring import MonitoringEntry, MonitoringLog, derive_format
from file_translator.infrastructure.repositories.file_monitoring_repository import (
    FileMonitoringRepository,
)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# In-memory fakes
# --------------------------------------------------------------------------
class FakeJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    async def create(self, job: Job) -> Job:
        self.jobs[job.job_id] = job
        return job

    async def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def update(self, job: Job) -> Job | None:
        self.jobs[job.job_id] = job
        return job

    async def list_active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.is_active]

    async def list_recent(self, limit: int = 10) -> list[Job]:
        return list(self.jobs.values())[:limit]

    async def list_by_user(self, user_id: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.user_id == user_id]

    async def delete(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None


class RecordingMonitoring:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def log(self, **kwargs) -> None:
        self.calls.append(kwargs)


class RaisingMonitoring:
    async def log(self, **kwargs) -> None:
        raise RuntimeError("monitoring backend down")


class RaisingRepository:
    async def get_log(self, date: str):
        raise OSError("disk on fire")

    async def save_entry(self, date: str, entry) -> None:
        raise OSError("disk on fire")

    async def list_dates(self) -> list[str]:
        return []

    async def delete_older_than(self, days: int) -> int:
        return 0


# --------------------------------------------------------------------------
# 1.1 — domain models
# --------------------------------------------------------------------------
def test_entry_dict_round_trip():
    entry = MonitoringEntry(
        job_id="j-1",
        owner_id="u-1",
        filename="doc.docx",
        format="DOCX",
        status="completed",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    restored = MonitoringEntry.from_dict(entry.to_dict())
    assert restored == entry


def test_entry_stamps_a_timestamp_when_missing():
    entry = MonitoringEntry(job_id="j-2", status="pending")
    assert entry.timestamp


def test_log_dict_round_trip():
    log = MonitoringLog(date="2026-01-01", created_at="2026-01-01T00:00:00+00:00")
    log.add_entry(MonitoringEntry(job_id="j-3", status="running"))
    restored = MonitoringLog.from_dict(log.to_dict())
    assert restored.date == log.date
    assert restored.entry_count == 1
    assert restored.entries[0].job_id == "j-3"


def test_derive_format():
    assert derive_format("report.PDF") == "PDF"
    assert derive_format("noext") == ""
    assert derive_format("") == ""


# --------------------------------------------------------------------------
# 1.3 — file repository
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_repository_appends_and_reads_back(tmp_path):
    repo = FileMonitoringRepository(tmp_path)
    await repo.save_entry("2026-01-01", MonitoringEntry(job_id="j-1", status="pending"))
    await repo.save_entry("2026-01-01", MonitoringEntry(job_id="j-1", status="completed"))

    log = await repo.get_log("2026-01-01")
    assert log is not None
    assert [e.status for e in log.entries] == ["pending", "completed"]


@pytest.mark.asyncio
async def test_repository_list_dates_newest_first(tmp_path):
    repo = FileMonitoringRepository(tmp_path)
    await repo.save_entry("2026-01-01", MonitoringEntry(job_id="a", status="pending"))
    await repo.save_entry("2026-01-03", MonitoringEntry(job_id="b", status="pending"))
    await repo.save_entry("2026-01-02", MonitoringEntry(job_id="c", status="pending"))

    assert await repo.list_dates() == ["2026-01-03", "2026-01-02", "2026-01-01"]


@pytest.mark.asyncio
async def test_repository_creates_its_directory(tmp_path):
    target = tmp_path / "logs" / "monitoring"
    FileMonitoringRepository(target)
    assert target.exists()


# --------------------------------------------------------------------------
# 2.1 — service placement and best-effort writes
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_service_writes_to_todays_file(tmp_path):
    repo = FileMonitoringRepository(tmp_path)
    service = MonitoringLogService(repository=repo)

    await service.log(job_id="j-1", status="pending", owner_id="u-1",
                      filename="doc.docx", format="DOCX")

    log = await repo.get_log(_today())
    assert log is not None
    assert log.entries[0].job_id == "j-1"
    assert log.entries[0].status == "pending"
    assert log.entries[0].owner_id == "u-1"


@pytest.mark.asyncio
async def test_service_swallows_repository_failure():
    service = MonitoringLogService(repository=RaisingRepository())
    # Must not raise.
    await service.log(job_id="j-1", status="pending")


@pytest.mark.asyncio
async def test_service_returns_recent_entries_newest_first(tmp_path):
    repo = FileMonitoringRepository(tmp_path)
    service = MonitoringLogService(repository=repo)
    await repo.save_entry("2026-01-01", MonitoringEntry(job_id="old", status="pending"))
    await repo.save_entry("2026-01-02", MonitoringEntry(job_id="new-1", status="running"))
    await repo.save_entry("2026-01-02", MonitoringEntry(job_id="new-2", status="completed"))

    entries = await service.get_recent_entries(limit=10)
    assert [e.job_id for e in entries] == ["new-2", "new-1", "old"]


@pytest.mark.asyncio
async def test_service_recent_entries_respects_limit(tmp_path):
    repo = FileMonitoringRepository(tmp_path)
    service = MonitoringLogService(repository=repo)
    for i in range(5):
        await repo.save_entry("2026-01-02", MonitoringEntry(job_id=f"j-{i}", status="pending"))

    entries = await service.get_recent_entries(limit=2)
    assert len(entries) == 2


# --------------------------------------------------------------------------
# 2.2 — retention
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cleanup_removes_old_and_keeps_recent(tmp_path):
    repo = FileMonitoringRepository(tmp_path)
    service = MonitoringLogService(repository=repo)
    await repo.save_entry("2020-01-01", MonitoringEntry(job_id="old", status="pending"))
    await repo.save_entry(_today(), MonitoringEntry(job_id="recent", status="pending"))

    deleted = await service.cleanup_old_logs()
    assert deleted == 1
    assert await repo.get_log("2020-01-01") is None
    assert await repo.get_log(_today()) is not None


# --------------------------------------------------------------------------
# 3.1 — optional dependency
# --------------------------------------------------------------------------
def test_job_manager_constructs_without_monitoring():
    JobManager(repository=FakeJobRepository())  # no exception


# --------------------------------------------------------------------------
# 3.2 — one entry per transition
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_job_manager_emits_one_entry_per_transition():
    repo = FakeJobRepository()
    monitoring = RecordingMonitoring()
    manager = JobManager(repository=repo, monitoring_service=monitoring)

    job = await manager.create_job("doc.docx", "en", "ru", user_id="u-1")
    await manager.start_job(job.job_id)
    await manager.complete_job(job.job_id)

    assert [c["status"] for c in monitoring.calls] == ["pending", "running", "completed"]
    assert all(c["job_id"] == job.job_id for c in monitoring.calls)
    assert all(c["owner_id"] == "u-1" for c in monitoring.calls)
    assert all(c["filename"] == "doc.docx" for c in monitoring.calls)
    assert all(c["format"] == "DOCX" for c in monitoring.calls)


@pytest.mark.asyncio
async def test_job_manager_records_failure_and_cancellation():
    repo = FakeJobRepository()
    monitoring = RecordingMonitoring()
    manager = JobManager(repository=repo, monitoring_service=monitoring)

    failed = await manager.create_job("a.docx", "en", "ru", user_id="u-1")
    await manager.start_job(failed.job_id)
    await manager.fail_job(failed.job_id, "boom")

    cancelled = await manager.create_job("b.docx", "en", "ru", user_id="u-2")
    await manager.start_job(cancelled.job_id)
    await manager.cancel_job(cancelled.job_id)

    statuses = [c["status"] for c in monitoring.calls]
    assert statuses == ["pending", "running", "failed", "pending", "running", "cancelled"]


@pytest.mark.asyncio
async def test_cancel_of_non_running_job_records_nothing():
    repo = FakeJobRepository()
    monitoring = RecordingMonitoring()
    manager = JobManager(repository=repo, monitoring_service=monitoring)

    job = await manager.create_job("a.docx", "en", "ru", user_id="u-1")
    monitoring.calls.clear()
    # A pending job cannot be cancelled — no cancelled transition happens.
    await manager.cancel_job(job.job_id)
    assert monitoring.calls == []


# --------------------------------------------------------------------------
# 3.3 — logging failure never fails the job
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_job_manager_survives_raising_monitoring_service():
    repo = FakeJobRepository()
    manager = JobManager(repository=repo, monitoring_service=RaisingMonitoring())

    job = await manager.create_job("doc.docx", "en", "ru", user_id="u-1")
    assert job.status == JobStatus.PENDING
    started = await manager.start_job(job.job_id)
    assert started is not None and started.status == JobStatus.RUNNING

    completed = await manager.complete_job(job.job_id)
    assert completed is not None and completed.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_log_is_valid_json(tmp_path):
    repo = FileMonitoringRepository(tmp_path)
    await repo.save_entry("2026-01-01", MonitoringEntry(job_id="j-1", status="pending"))
    path = tmp_path / "monitoring_2026-01-01.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["date"] == "2026-01-01"
    assert data["entries"][0]["job_id"] == "j-1"
