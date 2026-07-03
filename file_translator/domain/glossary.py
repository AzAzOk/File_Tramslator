from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from file_translator.domain.models import LanguageCode

# Maps LanguageCode enum values to actual MySQL column names
_GLOSSARY_COLUMN_MAP = {
    LanguageCode.RU: "ru_word",
    LanguageCode.EN: "en_word",
    LanguageCode.SR: "sb_word",
    LanguageCode.ZH: "ch_word",
}


@dataclass
class GlossaryEntry:
    """A single glossary entry with translations in all supported languages.
    
    Column names match the actual MySQL table structure:
      - ru_word : Russian
      - en_word : English
      - sb_word : Serbian
      - ch_word : Chinese
      
    collection_id identifies which glossary collection this entry belongs to.
    Defaults to "default" for backward compatibility.
    """
    
    id: int = 0
    ru_word: str = ""
    en_word: str = ""
    sb_word: str = ""
    ch_word: str = ""
    collection_id: str = "default"
    created_by: str = ""
    created_at: datetime | None = None
    updated_by: str = ""
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_text(self, lang: LanguageCode) -> str:
        """Get text for a specific language column."""
        column = _GLOSSARY_COLUMN_MAP.get(lang)
        if not column:
            return ""
        return getattr(self, column, "")

    def set_text(self, lang: LanguageCode, text: str) -> None:
        """Set text for a specific language column."""
        column = _GLOSSARY_COLUMN_MAP.get(lang)
        if column:
            setattr(self, column, text)


@dataclass
class GlossaryCollection:
    """A named collection of glossary entries.
    
    Each collection groups entries belonging to a specific domain,
    department, or AD group. The "default" collection is always
    available and corresponds to the existing single MySQL glossary.
    """
    
    id: str = "default"
    name: str = ""
    description: str = ""


@dataclass
class Glossary:
    """Collection of glossary entries with lookup logic.
    
    Provides case-insensitive matching of source terms and
    replacement with target-language equivalents.
    """
    
    entries: list[GlossaryEntry] = field(default_factory=list)
    name: str = "default"
    description: str = ""

    def add_entry(self, entry: GlossaryEntry) -> None:
        """Add an entry to the glossary."""
        self.entries.append(entry)

    def remove_entry(self, entry_id: int) -> None:
        """Remove an entry by ID."""
        self.entries = [e for e in self.entries if e.id != entry_id]

    def find_entry(self, entry_id: int) -> GlossaryEntry | None:
        """Find an entry by ID."""
        for e in self.entries:
            if e.id == entry_id:
                return e
        return None

    def find_matches(self, text: str, source_lang: LanguageCode) -> list[GlossaryEntry]:
        """Find entries whose source-language text matches the given text (case-insensitive)."""
        text_lower = text.lower()
        matches = []
        for entry in self.entries:
            source_text = entry.get_text(source_lang)
            if source_text and source_text.lower() in text_lower:
                matches.append(entry)
        return matches
