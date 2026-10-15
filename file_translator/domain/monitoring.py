"""Job monitoring log domain models.

A durable, daily-rotated log of translation job lifecycle transitions — the
oversight counterpart to the processing journal. Kept deliberately separate
from ``domain/journal.py``: the journal tracks document *pipeline stages* for
debugging a translation, while this log tracks *job status transitions* for the
admin service's monitoring block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class MonitoringEntry:
    """A single job lifecycle transition recorded in the monitoring log."""

    job_id: str = ""
    owner_id: str = ""
    filename: str = ""
    format: str = ""
    status: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Serialize the entry to a JSON-friendly mapping."""
        return {
            "job_id": self.job_id,
            "owner_id": self.owner_id,
            "filename": self.filename,
            "format": self.format,
            "status": self.status,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MonitoringEntry":
        """Build an entry from its serialized form."""
        return cls(
            job_id=data.get("job_id", ""),
            owner_id=data.get("owner_id", ""),
            filename=data.get("filename", ""),
            format=data.get("format", ""),
            status=data.get("status", ""),
            timestamp=data.get("timestamp", ""),
        )


@dataclass
class MonitoringLog:
    """Monitoring entries for a single UTC calendar day.

    Daily rotation: a new log is created on the first write of each day.
    Retention: logs older than 30 days are deleted.
    """

    date: str = ""  # "YYYY-MM-DD"
    entries: list[MonitoringEntry] = field(default_factory=list)
    created_at: str = ""

    def add_entry(self, entry: MonitoringEntry) -> None:
        """Append an entry to this day's log."""
        self.entries.append(entry)

    @property
    def entry_count(self) -> int:
        """Number of entries in this log."""
        return len(self.entries)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the day's log to a JSON-friendly mapping."""
        return {
            "date": self.date,
            "created_at": self.created_at,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MonitoringLog":
        """Build a day's log from its serialized form."""
        log = cls(
            date=data.get("date", ""),
            created_at=data.get("created_at", ""),
        )
        for entry_data in data.get("entries", []):
            log.add_entry(MonitoringEntry.from_dict(entry_data))
        return log


def derive_format(filename: str) -> str:
    """Return the upper-cased extension of a file name, or "" if it has none."""
    suffix = Path(filename or "").suffix
    return suffix.lstrip(".").upper() if suffix else ""
