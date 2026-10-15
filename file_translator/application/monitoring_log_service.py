"""Monitoring log service — durable job lifecycle history with daily rotation.

Mirrors :class:`~file_translator.application.journal_service.JournalService`:
daily UTC rotation, best-effort writes that never break a job, and 30-day
retention. Cleanup is exposed as an explicit method so a periodic task can call
it without depending on a translation job being processed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from file_translator.domain.interfaces import MonitoringLogRepository
from file_translator.domain.monitoring import MonitoringEntry, MonitoringLog

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 30


class MonitoringLogService:
    """Service for recording job lifecycle transitions with daily rotation."""

    def __init__(self, repository: MonitoringLogRepository | None = None):
        self._repository = repository
        self._current_log: MonitoringLog | None = None
        self._current_date: str = ""

    @property
    def repository(self) -> MonitoringLogRepository:
        """Get the monitoring repository instance."""
        if not self._repository:
            raise RuntimeError("MonitoringLogRepository is not configured")
        return self._repository

    async def ensure_today_log(self) -> MonitoringLog:
        """Get or create the monitoring log for today's UTC date."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._current_date == today and self._current_log is not None:
            return self._current_log
        log = await self.repository.get_log(today)
        if log is None:
            log = MonitoringLog(
                date=today,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        self._current_log = log
        self._current_date = today
        return log

    async def log(
        self,
        job_id: str,
        status: str,
        owner_id: str = "",
        filename: str = "",
        format: str = "",
    ) -> None:
        """Record a job status transition. Never raises — a write failure is
        logged and swallowed so it can never fail the job itself."""
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry = MonitoringEntry(
                job_id=job_id,
                owner_id=owner_id,
                filename=filename,
                format=format,
                status=status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            log = await self.ensure_today_log()
            log.add_entry(entry)
            await self.repository.save_entry(today, entry)
            logger.debug(f"[Monitoring] {status} | {job_id} | {filename}")
        except Exception as e:
            logger.warning(f"Failed to write monitoring entry: {e}")

    async def cleanup_old_logs(self) -> int:
        """Delete monitoring logs older than the retention period."""
        try:
            deleted = await self.repository.delete_older_than(_RETENTION_DAYS)
            if deleted:
                logger.info(
                    f"Cleaned up {deleted} old monitoring log(s) (>{_RETENTION_DAYS} days)"
                )
            return deleted
        except Exception as e:
            logger.warning(f"Monitoring log cleanup failed: {e}")
            return 0

    async def get_recent_entries(self, limit: int = 100) -> list[MonitoringEntry]:
        """Return the most recent entries, newest first, bounded by ``limit``."""
        try:
            dates = await self.repository.list_dates()
            collected: list[MonitoringEntry] = []
            for date in dates:
                log = await self.repository.get_log(date)
                if not log:
                    continue
                for entry in reversed(log.entries):
                    collected.append(entry)
                    if len(collected) >= limit:
                        return collected
            return collected
        except Exception as e:
            logger.warning(f"Failed to list monitoring entries: {e}")
            return []
