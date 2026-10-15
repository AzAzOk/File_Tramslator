"""File-based monitoring log repository — durable job history as JSON files.

Each daily file is named ``monitoring_YYYY-MM-DD.json`` and stored in a
dedicated directory (default: ``./logs/monitoring``) so the monitoring log stays
separate from the processing journal. The files survive server restarts and
outlive the short-lived Redis job records.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from file_translator.domain.interfaces import MonitoringLogRepository
from file_translator.domain.monitoring import MonitoringEntry, MonitoringLog

_PREFIX = "monitoring_"


class FileMonitoringRepository(MonitoringLogRepository):
    """Monitoring-log storage backed by JSON files on disk."""

    def __init__(self, storage_dir: str | Path = "./logs/monitoring"):
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, date: str) -> Path:
        return self._storage_dir / f"{_PREFIX}{date}.json"

    def _load_log(self, date: str) -> MonitoringLog | None:
        path = self._file_path(date)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return MonitoringLog.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return None

    def _save_log(self, log: MonitoringLog) -> None:
        path = self._file_path(log.date)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log.to_dict(), f, ensure_ascii=False, indent=2)

    async def get_log(self, date: str) -> MonitoringLog | None:
        return self._load_log(date)

    async def save_entry(self, date: str, entry: Any) -> None:
        if not isinstance(entry, MonitoringEntry):
            return
        log = self._load_log(date)
        if log is None:
            log = MonitoringLog(
                date=date,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        log.add_entry(entry)
        self._save_log(log)

    async def list_dates(self) -> list[str]:
        dates = []
        for path in self._storage_dir.glob(f"{_PREFIX}*.json"):
            date = path.stem.replace(_PREFIX, "", 1)
            dates.append(date)
        return sorted(dates, reverse=True)

    async def delete_older_than(self, days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        deleted = 0
        for date in await self.list_dates():
            if date < cutoff_str:
                path = self._file_path(date)
                try:
                    path.unlink(missing_ok=True)
                    deleted += 1
                except OSError:
                    pass
        return deleted
