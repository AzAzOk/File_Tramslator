"""In-memory implementation of JobRepository for development."""

from __future__ import annotations

from typing import Any

from file_translator.domain.interfaces import JobRepository
from file_translator.domain.job import Job


class InMemoryJobRepository(JobRepository):
    """In-memory job storage.
    
    Stores jobs in a dict keyed by job_id.
    NOT thread-safe — intended for development/testing only.
    """
    
    def __init__(self):
        self._jobs: dict[str, Job] = {}
    
    async def create(self, job: Any) -> Any:
        self._jobs[job.job_id] = job
        return job
    
    async def get(self, job_id: str) -> Any | None:
        return self._jobs.get(job_id)
    
    async def update(self, job: Any) -> Any | None:
        if job.job_id in self._jobs:
            self._jobs[job.job_id] = job
            return job
        return None
    
    async def list_active(self) -> list[Any]:
        return [
            j for j in self._jobs.values()
            if hasattr(j, 'status') and j.status.value in ("pending", "running")
        ]
    
    async def list_recent(self, limit: int = 10) -> list[Any]:
        sorted_jobs = sorted(
            self._jobs.values(),
            key=lambda j: getattr(j, 'created_at', ''),
            reverse=True,
        )
        return sorted_jobs[:limit]

    async def delete(self, job_id: str) -> bool:
        if job_id in self._jobs:
            del self._jobs[job_id]
            return True
        return False
