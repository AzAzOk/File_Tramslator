"""Redis-backed JobRepository with automatic TTL for terminal jobs."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis

from file_translator.domain.interfaces import JobRepository
from file_translator.domain.job import Job, JobStatus, ProcessingStage


def _default_serializer(obj: Any) -> str:
    if isinstance(obj, (ProcessingStage,)):
        return obj.value
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


class RedisJobRepository(JobRepository):
    """Job storage backed by Redis with automatic TTL for terminal states.

    Active (pending/running) jobs live indefinitely.
    Terminal (completed/failed/cancelled) jobs get a configurable TTL
    after which they are automatically evicted by Redis.

    A sorted set (``job:index``) tracks all job IDs by creation timestamp
    for efficient ordered listing. Stale index entries are pruned lazily.
    """

    _KEY_PREFIX = "job:"
    _INDEX_KEY = "job:index"
    _TERMINAL_TTL = int(os.environ.get("JOB_TTL_SECONDS", "3600"))  # default 1 hour for terminal jobs
    _MAX_TTL = int(os.environ.get("JOB_MAX_TTL_SECONDS", "3600"))   # safety net: 1 hour for any job

    def __init__(self, redis: Redis | None = None):
        self._redis = redis

    async def _conn(self) -> Redis:
        if self._redis is None:
            self._redis = Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                db=0,
                decode_responses=True,
            )
        return self._redis

    def _job_key(self, job_id: str) -> str:
        return f"{self._KEY_PREFIX}{job_id}"

    def _serialize(self, job: Job) -> str:
        data = {
            "job_id": job.job_id,
            "user_id": job.user_id,
            "status": job.status.value,
            "filename": job.filename,
            "source_language": job.source_language,
            "target_language": job.target_language,
            "translation_style": job.translation_style,
            "current_stage": job.current_stage.value,
            "progress": job.progress,
            "total_batches": job.total_batches,
            "completed_batches": job.completed_batches,
            "total_text_units": job.total_text_units,
            "translated_text_units": job.translated_text_units,
            "eta_seconds": job.eta_seconds,
            "elapsed_seconds": job.elapsed_seconds,
            "error_message": job.error_message,
            "output_file_path": job.output_file_path,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "metadata": job.metadata,
        }
        return json.dumps(data, ensure_ascii=False)

    def _deserialize(self, raw: str) -> Job:
        data = json.loads(raw)
        return Job(
            job_id=data["job_id"],
            user_id=data.get("user_id", ""),
            status=JobStatus(data["status"]),
            filename=data.get("filename", ""),
            source_language=data.get("source_language", ""),
            target_language=data.get("target_language", ""),
            translation_style=data.get("translation_style", ""),
            current_stage=ProcessingStage(data.get("current_stage", ProcessingStage.QUEUED.value)),
            progress=data.get("progress", 0.0),
            total_batches=data.get("total_batches", 0),
            completed_batches=data.get("completed_batches", 0),
            total_text_units=data.get("total_text_units", 0),
            translated_text_units=data.get("translated_text_units", 0),
            eta_seconds=data.get("eta_seconds", 0.0),
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
            error_message=data.get("error_message", ""),
            output_file_path=data.get("output_file_path", ""),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            metadata=data.get("metadata", {}),
        )

    def _is_terminal(self, status: str) -> bool:
        return status in ("completed", "failed", "cancelled")

    async def _add_to_index(self, job: Job) -> None:
        conn = await self._conn()
        score = time.time()
        await conn.zadd(self._INDEX_KEY, {job.job_id: score})

    async def _prune_index(self) -> None:
        """Remove stale entries from the sorted index (keys that expired)."""
        conn = await self._conn()
        cursor, keys = "0", None
        stale_ids: list[str] = []
        while cursor != 0:
            cursor, keys = await conn.zscan(self._INDEX_KEY, cursor=cursor, count=100)
            for member, _ in keys:
                exists = await conn.exists(self._job_key(member))
                if not exists:
                    stale_ids.append(member)
        if stale_ids:
            await conn.zrem(self._INDEX_KEY, *stale_ids)

    # ── TTL logic ──────────────────────────────────────────────
    # _MAX_TTL (30 days) is a safety net applied on every write so
    # orphaned jobs never live forever even if the server crashes.
    # _TERMINAL_TTL (7 days) replaces it once the job reaches a
    # terminal state so completed/failed/cancelled jobs are cleaned
    # up sooner.

    async def _set_ttl(self, key: str, status: str) -> None:
        conn = await self._conn()
        if self._is_terminal(status):
            await conn.expire(key, self._TERMINAL_TTL)
        else:
            await conn.expire(key, self._MAX_TTL)

    async def create(self, job: Any) -> Any:
        conn = await self._conn()
        raw = self._serialize(job)
        key = self._job_key(job.job_id)
        await conn.set(key, raw)
        await self._set_ttl(key, job.status.value)
        await self._add_to_index(job)
        return job

    async def get(self, job_id: str) -> Any | None:
        conn = await self._conn()
        raw = await conn.get(self._job_key(job_id))
        if raw is None:
            # Lazy prune of stale index entry
            await conn.zrem(self._INDEX_KEY, job_id)
            return None
        return self._deserialize(raw)

    async def update(self, job: Any) -> Any | None:
        conn = await self._conn()
        raw = self._serialize(job)
        key = self._job_key(job.job_id)

        await conn.set(key, raw)
        await self._set_ttl(key, job.status.value)

        return job

    async def list_active(self) -> list[Any]:
        conn = await self._conn()
        cursor, keys = "0", None
        active: list[Job] = []
        while cursor != 0:
            cursor, keys = await conn.zscan(self._INDEX_KEY, cursor=cursor, count=100)
            for member, _ in keys:
                raw = await conn.get(self._job_key(member))
                if raw is None:
                    continue
                job = self._deserialize(raw)
                if job.is_active:
                    active.append(job)
        return active

    async def list_recent(self, limit: int = 10) -> list[Any]:
        conn = await self._conn()
        # Get newest-first from the sorted index
        ids = await conn.zrevrange(self._INDEX_KEY, 0, limit - 1)

        jobs: list[Job] = []
        for job_id in ids:
            raw = await conn.get(self._job_key(job_id))
            if raw is None:
                # Lazy prune of stale index entry
                await conn.zrem(self._INDEX_KEY, job_id)
                continue
            jobs.append(self._deserialize(raw))
        return jobs

    async def delete(self, job_id: str) -> bool:
        conn = await self._conn()
        key = self._job_key(job_id)
        if not await conn.exists(key):
            return False
        await conn.delete(key)
        await conn.zrem(self._INDEX_KEY, job_id)
        return True

    async def cleanup_terminal(self, max_age_seconds: int = 86400) -> int:
        """Force-delete terminal jobs older than max_age_seconds.

        Note: Redis TTL handles automatic cleanup for terminal jobs.
        This is a safety net for any edge cases where TTL was missed.
        """
        conn = await self._conn()
        cursor, keys = "0", None
        now = time.time()
        deleted = 0

        while cursor != 0:
            cursor, keys = await conn.zscan(self._INDEX_KEY, cursor=cursor, count=100)
            for member, score in keys:
                raw = await conn.get(self._job_key(member))
                if raw is None:
                    await conn.zrem(self._INDEX_KEY, member)
                    continue
                job = self._deserialize(raw)
                if job.is_terminal and (now - score) > max_age_seconds:
                    await conn.delete(self._job_key(member))
                    await conn.zrem(self._INDEX_KEY, member)
                    deleted += 1
        return deleted
