"""Glossary collection repository implementations."""

from __future__ import annotations

import logging
from typing import Any

from file_translator.domain.glossary import GlossaryCollection, GlossaryEntry
from file_translator.domain.interfaces import GlossaryCollectionRepository, GlossaryRepository

logger = logging.getLogger(__name__)


class InMemoryGlossaryCollectionRepository(GlossaryCollectionRepository):
    """Glossary collection storage backed by MySQL per-group tables.
    
    Each collection with ID != "default" maps to a MySQL table named
    ``glossary_{collection_id}``. The "default" collection uses the
    existing ``glossary`` table for backward compatibility.
    """

    _TABLE_PATTERN = "glossary_%"

    def __init__(self, glossary_repository: GlossaryRepository):
        self._glossary_repository = glossary_repository

    @staticmethod
    def _table_for(collection_id: str) -> str:
        return "glossary" if collection_id == "default" else f"glossary_{collection_id}"

    async def find_all(self) -> list[GlossaryCollection]:
        collections: list[GlossaryCollection] = [
            GlossaryCollection(
                id="default",
                name="Default Glossary",
                description="Single shared glossary (backward compatible)",
            ),
        ]
        try:
            tables = await self._glossary_repository.list_tables(self._TABLE_PATTERN)
            for table in tables:
                cid = table[len("glossary_"):]
                collections.append(GlossaryCollection(
                    id=cid,
                    name=f"Glossary ({cid})",
                    description=f"Glossary entries for group '{cid}'",
                ))
        except Exception as e:
            logger.warning(f"Failed to list glossary tables: {e}")
        return collections

    async def find_by_id(self, collection_id: str) -> GlossaryCollection | None:
        if collection_id == "default":
            return GlossaryCollection(
                id="default",
                name="Default Glossary",
                description="Single shared glossary (backward compatible)",
            )
        table = self._table_for(collection_id)
        try:
            exists = await self._glossary_repository.table_exists(table)
            if exists:
                return GlossaryCollection(
                    id=collection_id,
                    name=f"Glossary ({collection_id})",
                    description=f"Glossary entries for group '{collection_id}'",
                )
        except Exception:
            pass
        return None

    async def get_entries(self, collection_id: str) -> list[Any]:
        table = self._table_for(collection_id)
        try:
            exists = await self._glossary_repository.table_exists(table)
            if not exists:
                logger.warning(f"Table '{table}' for collection '{collection_id}' not found — falling back to default")
                table = "glossary"
        except Exception:
            logger.warning(f"Could not check table '{table}' for collection '{collection_id}' — falling back to default")
            table = "glossary"
        return await self._glossary_repository.find_all(table_name=table)
