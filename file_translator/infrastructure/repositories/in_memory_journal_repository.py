"""In-memory implementation of JournalRepository for development."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from file_translator.domain.interfaces import JournalRepository
from file_translator.domain.journal import JournalEntry, ProcessingJournal


class InMemoryJournalRepository(JournalRepository):
    """In-memory journal storage.
    
    Stores journals as dict[date_str, ProcessingJournal].
    NOT thread-safe — intended for development/testing only.
    """
    
    def __init__(self):
        self._journals: dict[str, ProcessingJournal] = {}
    
    async def get_journal(self, date: str) -> Any | None:
        return self._journals.get(date)
    
    async def save_entry(self, date: str, entry: Any) -> None:
        if date not in self._journals:
            self._journals[date] = ProcessingJournal(
                date=date,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        if isinstance(entry, JournalEntry):
            self._journals[date].add_entry(entry)
    
    async def list_dates(self) -> list[str]:
        return sorted(self._journals.keys(), reverse=True)
    
    async def delete_older_than(self, days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        to_delete = [d for d in self._journals if d < cutoff_str]
        for d in to_delete:
            del self._journals[d]
        return len(to_delete)
