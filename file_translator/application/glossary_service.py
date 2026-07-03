"""Glossary service - Term substitution before LLM translation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from file_translator.domain.errors import TranslationError
from file_translator.domain.glossary import Glossary, GlossaryCollection, GlossaryEntry
from file_translator.domain.interfaces import GlossaryCollectionRepository, GlossaryRepository
from file_translator.domain.models import LanguageCode, TextUnit
from file_translator.infrastructure.auth.glossary_access_resolver import GlossaryAccessResolver
from file_translator.infrastructure.language_validator import validate_glossary_value

logger = logging.getLogger(__name__)


class GlossaryService:
    """Service for applying glossary term substitutions to text units.
    
    Before sending text to the LLM, known terms are replaced with their
    target-language equivalents so the model preserves them as-is.
    
    Supports multi-collection glossaries scoped by AD group membership.
    """
    
    def __init__(
        self,
        repository: GlossaryRepository | None = None,
        collection_repository: GlossaryCollectionRepository | None = None,
        access_resolver: GlossaryAccessResolver | None = None,
    ):
        self._repository = repository
        self._collection_repository = collection_repository
        self._access_resolver = access_resolver or GlossaryAccessResolver()
    
    @property
    def repository(self) -> GlossaryRepository:
        """Get the glossary repository instance."""
        if not self._repository:
            raise RuntimeError("GlossaryRepository is not configured")
        return self._repository

    @property
    def collection_repository(self) -> GlossaryCollectionRepository:
        """Get the glossary collection repository instance."""
        if not self._collection_repository:
            raise RuntimeError("GlossaryCollectionRepository is not configured")
        return self._collection_repository
    
    async def load_glossary(self, glossary_id: str = "") -> Glossary:
        """Load glossary entries from the repository.
        
        If glossary_id is specified, loads entries for that collection.
        Otherwise loads all entries the default repository returns.
        """
        if glossary_id:
            entries_data = await self.collection_repository.get_entries(glossary_id)
        else:
            entries_data = await self.repository.find_all()
        entries = [
            GlossaryEntry(
                id=int(getattr(e, "id", 0)),
                ru_word=str(getattr(e, "ru_word", "")),
                en_word=str(getattr(e, "en_word", "")),
                sb_word=str(getattr(e, "sb_word", "")),
                ch_word=str(getattr(e, "ch_word", "")),
                collection_id=str(getattr(e, "collection_id", "default")),
            )
            for e in entries_data
        ]
        return Glossary(entries=entries, name=glossary_id or "default")
    
    def apply_glossary(
        self,
        glossary: Glossary,
        text_units: list[TextUnit],
        source_lang: LanguageCode,
        target_lang: LanguageCode,
    ) -> list[TextUnit]:
        """Apply glossary substitutions to text units.
        
        For each text unit, finds matching glossary entries and replaces
        source-language terms with target-language equivalents (case-insensitive).
        
        Args:
            glossary: Loaded glossary with entries.
            text_units: Text units to process.
            source_lang: Source language code.
            target_lang: Target language code.
            
        Returns:
            Modified text units with glossary terms replaced.
        """
        applied_count = 0
        
        for idx, unit in enumerate(text_units):
            original = unit.original_text
            if not original:
                continue
            
            modified = original
            matches = glossary.find_matches(original, source_lang)
            
            for entry in matches:
                source_text = entry.get_text(source_lang)
                target_text = entry.get_text(target_lang)
                
                if source_text and target_text:
                    # Case-insensitive replacement preserving original casing
                    pos = modified.lower().find(source_text.lower())
                    if pos != -1:
                        modified = modified[:pos] + target_text + modified[pos + len(source_text):]
                        applied_count += 1
            
            if modified != original:
                logger.debug(
                    f"Glossary applied to unit {unit.id}: "
                    f"{original[:50]}... -> {modified[:50]}..."
                )
                text_units[idx] = self._replace_text_in_unit(unit, modified)
        
        logger.info(f"Glossary substitutions applied: {applied_count}")
        return text_units
    
    def _replace_text_in_unit(self, unit: TextUnit, new_text: str) -> TextUnit:
        """Replace original_text in a frozen TextUnit by recreating it."""
        from dataclasses import replace
        return replace(unit, original_text=new_text)
    
    # --- Collection-aware methods ---

    async def get_accessible_collections(self, groups: list[str] | None) -> list[GlossaryCollection]:
        """Get glossary collections accessible by user's AD groups."""
        allowed_ids = self._access_resolver.resolve(groups)
        all_collections = await self.collection_repository.find_all()
        return [c for c in all_collections if c.id in allowed_ids]

    @staticmethod
    def _table_for(collection_id: str) -> str:
        return "glossary" if collection_id == "default" else f"glossary_{collection_id}"

    async def get_all_entries(self, collection_id: str = "") -> list[Any]:
        """Get all glossary entries, optionally filtered by collection."""
        if collection_id:
            return await self.collection_repository.get_entries(collection_id)
        return await self.repository.find_all()

    async def add_entry(self, entry_data: Any, collection_id: str = "default", created_by: str = "") -> Any:
        """Add a new glossary entry with uniqueness and language checks."""
        self._validate_language(entry_data)
        await self._check_duplicate(entry_data, collection_id)
        table = self._table_for(collection_id)
        return await self.repository.add(entry_data, table_name=table, created_by=created_by)
    
    async def update_entry(self, entry_data: Any, collection_id: str = "default", updated_by: str = "") -> Any | None:
        """Update an existing glossary entry with uniqueness and language checks."""
        entry_id = entry_data.id if isinstance(entry_data, GlossaryEntry) else int(getattr(entry_data, "id", 0))
        self._validate_language(entry_data)
        await self._check_duplicate(entry_data, collection_id, exclude_id=entry_id)
        table = self._table_for(collection_id)
        return await self.repository.update(entry_data, table_name=table, updated_by=updated_by)

    async def _check_duplicate(
        self,
        entry_data: Any,
        collection_id: str = "default",
        exclude_id: int | None = None,
    ) -> None:
        """Check for duplicate values across all 4 language columns within a collection.

        Exact match is used — case-sensitive, spaces and punctuation matter.
        Empty strings are skipped (not considered duplicates).

        Raises ValueError if any column has a duplicate.
        """
        existing = await self.get_all_entries(collection_id)

        ru = str(getattr(entry_data, "ru_word", "") or "")
        en = str(getattr(entry_data, "en_word", "") or "")
        sb = str(getattr(entry_data, "sb_word", "") or "")
        ch = str(getattr(entry_data, "ch_word", "") or "")

        for entry in existing:
            if exclude_id is not None and entry.id == exclude_id:
                continue

            if ru and getattr(entry, "ru_word", "") == ru:
                raise ValueError(
                    f"Русское слово '{ru}' уже существует в коллекции '{collection_id}' (id: {entry.id})"
                )
            if en and getattr(entry, "en_word", "") == en:
                raise ValueError(
                    f"Английское слово '{en}' уже существует в коллекции '{collection_id}' (id: {entry.id})"
                )
            if sb and getattr(entry, "sb_word", "") == sb:
                raise ValueError(
                    f"Сербское слово '{sb}' уже существует в коллекции '{collection_id}' (id: {entry.id})"
                )
            if ch and getattr(entry, "ch_word", "") == ch:
                raise ValueError(
                    f"Китайское слово '{ch}' уже существует в коллекции '{collection_id}' (id: {entry.id})"
                )
    
    async def delete_entry(self, entry_id: str, collection_id: str = "default") -> bool:
        """Delete a glossary entry."""
        table = self._table_for(collection_id)
        return await self.repository.delete(entry_id, table_name=table)
    
    @staticmethod
    def _validate_language(entry_data: Any) -> None:
        """Validate each populated column matches its expected language.

        Raises ValueError on the first mismatch.
        """
        for col in ("ru_word", "en_word", "sb_word", "ch_word"):
            value = str(getattr(entry_data, col, "") or "").strip()
            if not value:
                continue
            error = validate_glossary_value(col, value)
            if error:
                raise ValueError(error)

    async def import_from_file(self, file_path: Path) -> int:
        """Import glossary entries from a file."""
        return await self.repository.import_from_file(file_path)
    
    async def export_to_file(self, file_path: Path) -> Path:
        """Export glossary entries to a file."""
        return await self.repository.export_to_file(file_path)
