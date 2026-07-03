"""File-based journal repository — stores journals as JSON files on disk.

Each journal file is named journal_YYYY-MM-DD.json and stored in a configurable
directory (default: ./logs). Journals survive server restarts.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from file_translator.domain.interfaces import JournalRepository
from file_translator.domain.journal import JournalEntry, JournalLevel, JournalStage, ProcessingJournal


class FileJournalRepository(JournalRepository):
    """Journal storage backed by JSON files on disk."""

    def __init__(self, storage_dir: str | Path = "./logs"):
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, date: str) -> Path:
        return self._storage_dir / f"journal_{date}.json"

    def _deserialize_entry(self, data: dict) -> JournalEntry:
        return JournalEntry(
            timestamp=data.get("timestamp", ""),
            level=JournalLevel(data.get("level", "INFO")),
            stage=JournalStage(data.get("stage", "received")),
            message=data.get("message", ""),
            filename=data.get("filename", ""),
            details=data.get("details", {}),
        )

    def _serialize_entry(self, entry: JournalEntry) -> dict:
        return {
            "timestamp": entry.timestamp,
            "level": entry.level.value,
            "stage": entry.stage.value,
            "message": entry.message,
            "filename": entry.filename,
            "details": entry.details,
        }

    def _load_journal(self, date: str) -> ProcessingJournal | None:
        path = self._file_path(date)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            journal = ProcessingJournal(
                date=data.get("date", date),
                created_at=data.get("created_at", ""),
            )
            for entry_data in data.get("entries", []):
                journal.add_entry(self._deserialize_entry(entry_data))
            return journal
        except (json.JSONDecodeError, OSError):
            return None

    def _save_journal(self, journal: ProcessingJournal) -> None:
        data = {
            "date": journal.date,
            "created_at": journal.created_at,
            "entries": [self._serialize_entry(e) for e in journal.entries],
        }
        path = self._file_path(journal.date)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def get_journal(self, date: str) -> Any | None:
        return self._load_journal(date)

    async def save_entry(self, date: str, entry: Any) -> None:
        if not isinstance(entry, JournalEntry):
            return
        journal = self._load_journal(date)
        if journal is None:
            journal = ProcessingJournal(
                date=date,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        journal.add_entry(entry)
        self._save_journal(journal)

    async def list_dates(self) -> list[str]:
        dates = []
        for path in self._storage_dir.glob("journal_*.json"):
            date = path.stem.replace("journal_", "", 1)
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
