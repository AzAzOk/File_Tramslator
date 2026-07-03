"""Journal service - Processing event logging with daily rotation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from file_translator.domain.interfaces import JournalRepository
from file_translator.domain.journal import (
    JournalEntry,
    JournalStage,
    JournalLevel,
    ProcessingJournal,
)

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 30


class JournalService:
    """Service for logging processing events with daily rotation.
    
    - A new journal is created on the first request of each new day.
    - Journals are retained for 30 days; older journals are cleaned up.
    """
    
    def __init__(self, repository: JournalRepository | None = None):
        self._repository = repository
        self._current_journal: ProcessingJournal | None = None
        self._current_date: str = ""
    
    @property
    def repository(self) -> JournalRepository:
        """Get the journal repository instance."""
        if not self._repository:
            raise RuntimeError("JournalRepository is not configured")
        return self._repository
    
    async def ensure_today_journal(self) -> ProcessingJournal:
        """Get or create the journal for today's date."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        if self._current_date == today and self._current_journal is not None:
            return self._current_journal
        
        journal_data = await self.repository.get_journal(today)
        if journal_data:
            self._current_journal = journal_data
        else:
            self._current_journal = ProcessingJournal(
                date=today,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        
        self._current_date = today
        return self._current_journal
    
    async def log(
        self,
        level: JournalLevel,
        stage: JournalStage,
        message: str,
        filename: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log an event to today's journal."""
        try:
            journal = await self.ensure_today_journal()
            entry = JournalEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                level=level,
                stage=stage,
                message=message,
                filename=filename,
                details=details or {},
            )
            journal.add_entry(entry)
            await self.repository.save_entry(journal.date, entry)
            logger.debug(f"[Journal] {level.value} | {stage.value} | {message}")
        except Exception as e:
            logger.warning(f"Failed to write journal entry: {e}")
    
    async def log_info(self, stage: JournalStage, message: str,
                       filename: str = "", details: dict[str, Any] | None = None) -> None:
        """Log an INFO-level event."""
        await self.log(JournalLevel.INFO, stage, message, filename, details)
    
    async def log_warning(self, stage: JournalStage, message: str,
                          filename: str = "", details: dict[str, Any] | None = None) -> None:
        """Log a WARNING-level event."""
        await self.log(JournalLevel.WARNING, stage, message, filename, details)
    
    async def log_error(self, stage: JournalStage, message: str,
                        filename: str = "", details: dict[str, Any] | None = None) -> None:
        """Log an ERROR-level event."""
        await self.log(JournalLevel.ERROR, stage, message, filename, details)
    
    async def cleanup_old_journals(self) -> int:
        """Delete journals older than retention period. Returns count deleted."""
        try:
            deleted = await self.repository.delete_older_than(_RETENTION_DAYS)
            if deleted:
                logger.info(f"Cleaned up {deleted} old journal(s) (>{_RETENTION_DAYS} days)")
            return deleted
        except Exception as e:
            logger.warning(f"Journal cleanup failed: {e}")
            return 0
    
    async def get_recent_journals(self, limit: int = 5) -> list[ProcessingJournal]:
        """Get the most recent journals."""
        try:
            dates = await self.repository.list_dates()
            journals = []
            for date in dates[:limit]:
                journal = await self.repository.get_journal(date)
                if journal:
                    journals.append(journal)
            return journals
        except Exception as e:
            logger.warning(f"Failed to list recent journals: {e}")
            return []
    
    async def get_journal_for_date(self, date: str) -> ProcessingJournal | None:
        """Get the journal for a specific date."""
        try:
            return await self.repository.get_journal(date)
        except Exception as e:
            logger.warning(f"Failed to get journal for {date}: {e}")
            return None
