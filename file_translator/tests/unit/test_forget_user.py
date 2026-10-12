"""Forget-user support tests (change: forget-user, tasks 5.1-5.2).

Two seams are covered here:

* the repository helpers that enumerate/remove a user's jobs and tokens, driven
  by fakes that behave like Redis/Mongo (no live services);
* the main API's internal ``/internal/users/{id}/jobs/purge`` handler and its
  shared-token dependency, invoked directly with fakes the way the other
  job-lifecycle tests do.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

# Must be set before importing app (it fails at import without these).
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("GLOSSARY_DB_PASSWORD", "test-pass")

import file_translator.presentation.api.app as api_app  # noqa: E402
from file_translator.domain.job import Job, JobStatus  # noqa: E402
from file_translator.infrastructure.repositories.auth_repository import (  # noqa: E402
    MongoSessionRepository,
)
from file_translator.infrastructure.repositories.redis_job_repository import (  # noqa: E402
    RedisJobRepository,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeRedisConn:
    """Just enough of an async Redis connection for the list/scan methods."""

    def __init__(self, members: list[tuple[str, str | None]]) -> None:
        # (job_id, raw record or None). ``None`` models a stale index entry
        # whose ``job:<id>`` key already expired.
        self._members = members
        self.removed: list[str] = []

    async def zscan(self, key: str, cursor: str = "0", count: int = 100) -> Any:
        # One page: a returned cursor of 0 ends the ``while cursor != 0`` loop.
        return 0, [(job_id, 0.0) for job_id, _ in self._members]

    async def get(self, key: str) -> str | None:
        job_id = key[len("job:"):]
        for member_id, raw in self._members:
            if member_id == job_id:
                return raw
        return None

    async def zrem(self, key: str, member: str) -> int:
        self.removed.append(member)
        return 1


def make_job(job_id: str, user_id: str, status: JobStatus = JobStatus.PENDING) -> Job:
    return Job(job_id=job_id, user_id=user_id, status=status, filename=f"{job_id}.docx")


def repo_with(conn: FakeRedisConn) -> RedisJobRepository:
    repo = RedisJobRepository()

    async def _conn() -> Any:
        return conn

    repo._conn = _conn  # type: ignore[method-assign]
    return repo


class FakeCollection:
    def __init__(
        self, deleted_count: int = 0, distinct_values: list[Any] | None = None
    ) -> None:
        self.deleted_count = deleted_count
        self.filters: list[dict[str, Any]] = []
        self._distinct_values = list(distinct_values or [])
        self.distinct_fields: list[str] = []

    async def delete_many(self, flt: dict[str, Any]) -> Any:
        self.filters.append(flt)
        return SimpleNamespace(deleted_count=self.deleted_count)

    async def distinct(self, field: str) -> list[Any]:
        self.distinct_fields.append(field)
        return list(self._distinct_values)


class FakeDb:
    def __init__(self) -> None:
        self.sessions = FakeCollection()
        self.refresh_tokens = FakeCollection(deleted_count=7)


# --------------------------------------------------------------------------
# Task 5.1 — repository helpers
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_by_user_returns_only_that_users_jobs() -> None:
    seed = RedisJobRepository()
    a1 = make_job("a-1", "user-a")
    a2 = make_job("a-2", "user-a", JobStatus.RUNNING)
    b1 = make_job("b-1", "user-b")
    repo = repo_with(
        FakeRedisConn(
            [
                ("a-1", seed._serialize(a1)),
                ("a-2", seed._serialize(a2)),
                ("b-1", seed._serialize(b1)),
            ]
        )
    )

    owned = await repo.list_by_user("user-a")

    assert sorted(j.job_id for j in owned) == ["a-1", "a-2"]
    assert all(j.user_id == "user-a" for j in owned)


@pytest.mark.asyncio
async def test_list_by_user_prunes_stale_index_entries() -> None:
    seed = RedisJobRepository()
    a1 = make_job("a-1", "user-a")
    conn = FakeRedisConn(
        [
            ("a-1", seed._serialize(a1)),
            ("ghost", None),  # record expired; still listed in job:index
        ]
    )
    repo = repo_with(conn)

    owned = await repo.list_by_user("user-a")

    assert [j.job_id for j in owned] == ["a-1"]
    assert conn.removed == ["ghost"]


@pytest.mark.asyncio
async def test_delete_refresh_tokens_for_user_filters_and_returns_count() -> None:
    db = FakeDb()
    repo = MongoSessionRepository(db)  # type: ignore[arg-type]

    removed = await repo.delete_refresh_tokens_for_user("user-a")

    assert removed == 7
    assert db.refresh_tokens.filters == [{"user_id": "user-a"}]


@pytest.mark.asyncio
async def test_list_user_ids_returns_distinct_token_owners() -> None:
    db = FakeDb()
    db.refresh_tokens = FakeCollection(distinct_values=["user-a", "", None])
    repo = MongoSessionRepository(db)  # type: ignore[arg-type]

    ids = await repo.list_user_ids()

    # Blank/missing values are dropped; real ids survive.
    assert ids == ["user-a"]
    assert db.refresh_tokens.distinct_fields == ["user_id"]


# --------------------------------------------------------------------------
# Task 5.2 — internal token gate
# --------------------------------------------------------------------------
def request_with_headers(headers: dict[str, str]) -> Any:
    return SimpleNamespace(headers=headers)


@pytest.mark.asyncio
async def test_internal_token_unset_refuses_with_503(monkeypatch) -> None:
    monkeypatch.setattr(api_app, "INTERNAL_API_TOKEN", "")
    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.require_internal_token(request_with_headers({"X-Internal-Token": "x"}))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_internal_token_missing_or_wrong_refuses_with_401(monkeypatch) -> None:
    monkeypatch.setattr(api_app, "INTERNAL_API_TOKEN", "good")
    for headers in ({}, {"X-Internal-Token": "bad"}):
        with pytest.raises(api_app.HTTPException) as exc:
            await api_app.require_internal_token(request_with_headers(headers))
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_internal_token_correct_is_accepted(monkeypatch) -> None:
    monkeypatch.setattr(api_app, "INTERNAL_API_TOKEN", "good")
    assert (
        await api_app.require_internal_token(
            request_with_headers({"X-Internal-Token": "good"})
        )
        is None
    )


# --------------------------------------------------------------------------
# Task 5.2 — purge handler
# --------------------------------------------------------------------------
class FakePurgeJobManager:
    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs
        self.cancelled: list[str] = []
        self.deleted: list[str] = []

    async def list_jobs_by_user(self, user_id: str) -> list[Job]:
        return [j for j in self._jobs if j.user_id == user_id]

    async def cancel_job(self, job_id: str) -> Job | None:
        self.cancelled.append(job_id)
        return None

    async def delete_job(self, job_id: str) -> bool:
        self.deleted.append(job_id)
        return True


class FakeQueue:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel_pending(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


def patch_purge(monkeypatch, jobs: list[Job]):
    manager = FakePurgeJobManager(jobs)
    queue = FakeQueue()
    cleaned: list[str] = []
    monkeypatch.setattr(
        api_app, "translation_service", SimpleNamespace(job_manager=manager)
    )
    monkeypatch.setattr(api_app, "user_job_queue", queue)
    monkeypatch.setattr(
        api_app, "_cleanup_job_temp_dirs", lambda job, job_id: cleaned.append(job_id)
    )
    return manager, queue, cleaned


@pytest.mark.asyncio
async def test_purge_running_job_is_cancelled_record_kept(monkeypatch) -> None:
    jobs = [
        make_job("a-1", "user-a"),  # pending: dropped outright
        make_job("a-2", "user-a", JobStatus.RUNNING),
    ]
    manager, queue, cleaned = patch_purge(monkeypatch, jobs)

    result = await api_app.purge_user_jobs("user-a", _=None)

    assert result == {"cancelled": 1, "removed": 1}
    # The running job is stopped but its record is left for the pipeline.
    assert manager.cancelled == ["a-2"]
    assert manager.deleted == ["a-1"]
    assert queue.cancelled == ["a-1"]
    assert cleaned == ["a-1"]


@pytest.mark.asyncio
async def test_purge_pending_job_removes_record_and_temp_dirs(monkeypatch) -> None:
    manager, queue, _ = patch_purge(monkeypatch, [make_job("p-1", "user-a")])

    result = await api_app.purge_user_jobs("user-a", _=None)

    assert result == {"cancelled": 0, "removed": 1}
    assert manager.cancelled == []
    assert manager.deleted == ["p-1"]
    assert queue.cancelled == ["p-1"]


@pytest.mark.asyncio
async def test_purge_ignores_other_users_jobs(monkeypatch) -> None:
    manager = patch_purge(
        monkeypatch,
        [make_job("a-1", "user-a"), make_job("b-1", "user-b")],
    )[0]

    result = await api_app.purge_user_jobs("user-a", _=None)

    assert result == {"cancelled": 0, "removed": 1}
    assert manager.deleted == ["a-1"]


@pytest.mark.asyncio
async def test_purge_mixed_statuses_and_other_users(monkeypatch) -> None:
    jobs = [
        make_job("run-1", "user-a", JobStatus.RUNNING),
        make_job("pend-1", "user-a", JobStatus.PENDING),
        make_job("done-1", "user-a", JobStatus.COMPLETED),
        make_job("other", "user-b", JobStatus.RUNNING),
    ]
    manager, queue, cleaned = patch_purge(monkeypatch, jobs)

    result = await api_app.purge_user_jobs("user-a", _=None)

    # Running stopped; pending + terminal removed; the other user untouched.
    assert result == {"cancelled": 1, "removed": 2}
    assert manager.cancelled == ["run-1"]
    assert sorted(manager.deleted) == ["done-1", "pend-1"]
    assert sorted(queue.cancelled) == ["done-1", "pend-1"]
    assert sorted(cleaned) == ["done-1", "pend-1"]
