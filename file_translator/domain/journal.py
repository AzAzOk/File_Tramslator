"""Processing journal domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class JournalLevel(Enum):
    """Log severity level."""
    
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    DEBUG = "DEBUG"


class JournalStage(Enum):
    """Stage of document processing for journal entries."""
    
    RECEIVED = "received"
    VALIDATION = "validation"
    EXTRACTION = "extraction"
    GLOSSARY = "glossary"
    TRANSLATION = "translation"
    SAVE = "save"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class JournalEntry:
    """A single entry in the processing journal.
    
    Captures a timestamped event during document processing
    with severity level, processing stage, and descriptive message.
    """
    
    timestamp: str = ""
    level: JournalLevel = JournalLevel.INFO
    stage: JournalStage = JournalStage.RECEIVED
    message: str = ""
    filename: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class ProcessingJournal:
    """Journal for a single day's processing events.
    
    Daily rotation: a new journal is created on the first request of each day.
    Retention: journals older than 30 days are deleted.
    """
    
    date: str = ""  # "YYYY-MM-DD"
    entries: list[JournalEntry] = field(default_factory=list)
    created_at: str = ""

    def add_entry(self, entry: JournalEntry) -> None:
        """Add a journal entry."""
        self.entries.append(entry)

    def add_event(self, level: JournalLevel, stage: JournalStage,
                  message: str, filename: str = "", details: dict[str, Any] | None = None) -> None:
        """Create and add a journal entry in one call."""
        entry = JournalEntry(
            timestamp=datetime.now().isoformat(),
            level=level,
            stage=stage,
            message=message,
            filename=filename,
            details=details or {},
        )
        self.entries.append(entry)

    @property
    def entry_count(self) -> int:
        """Number of entries in this journal."""
        return len(self.entries)

    @property
    def has_errors(self) -> bool:
        """Check if any error entries exist."""
        return any(e.level == JournalLevel.ERROR for e in self.entries)
