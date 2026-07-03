"""In-memory implementation of GlossaryRepository for development."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from file_translator.domain.glossary import GlossaryEntry
from file_translator.domain.interfaces import GlossaryRepository


class InMemoryGlossaryRepository(GlossaryRepository):
    """In-memory glossary storage.
    
    Stores glossary entries as GlossaryEntry domain objects.
    NOT thread-safe — intended for development/testing only.
    """
    
    def __init__(self):
        self._entries: list[GlossaryEntry] = []
        self._next_id: int = 1
    
    async def find_all(self, table_name: str = "glossary") -> list[Any]:
        return list(self._entries)
    
    async def find_by_id(self, entry_id: str, table_name: str = "glossary") -> Any | None:
        for e in self._entries:
            if str(e.id) == str(entry_id):
                return e
        return None
    
    async def add(self, entry_data: Any, table_name: str = "glossary") -> Any:
        entry = self._to_entry(entry_data)
        entry.id = self._next_id
        self._next_id += 1
        self._entries.append(entry)
        return entry
    
    async def update(self, entry_data: Any, table_name: str = "glossary") -> Any | None:
        new_entry = self._to_entry(entry_data)
        for i, e in enumerate(self._entries):
            if e.id == new_entry.id:
                self._entries[i] = new_entry
                return new_entry
        return None
    
    async def delete(self, entry_id: str, table_name: str = "glossary") -> bool:
        for i, e in enumerate(self._entries):
            if str(e.id) == str(entry_id):
                self._entries.pop(i)
                return True
        return False

    async def table_exists(self, table_name: str) -> bool:
        return True

    async def list_tables(self, pattern: str = "glossary_%") -> list[str]:
        return []
    
    async def import_from_file(self, file_path: Path) -> int:
        raise NotImplementedError("Import not implemented for in-memory repo")
    
    async def export_to_file(self, file_path: Path) -> Path:
        raise NotImplementedError("Export not implemented for in-memory repo")
    
    def _to_entry(self, data: Any) -> GlossaryEntry:
        """Convert various input types (dict, Pydantic model, GlossaryEntry) to GlossaryEntry."""
        if isinstance(data, GlossaryEntry):
            return GlossaryEntry(
                id=data.id,
                ru_word=data.ru_word,
                en_word=data.en_word,
                sb_word=data.sb_word,
                ch_word=data.ch_word,
                metadata=data.metadata.copy(),
            )
        if isinstance(data, dict):
            return GlossaryEntry(
                id=int(data.get("id", 0)),
                ru_word=str(data.get("ru_word", "")),
                en_word=str(data.get("en_word", "")),
                sb_word=str(data.get("sb_word", "")),
                ch_word=str(data.get("ch_word", "")),
            )
        return GlossaryEntry(
            id=int(getattr(data, "id", 0)),
            ru_word=str(getattr(data, "ru_word", "")),
            en_word=str(getattr(data, "en_word", "")),
            sb_word=str(getattr(data, "sb_word", "")),
            ch_word=str(getattr(data, "ch_word", "")),
        )
