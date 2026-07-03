"""MySQL-backed GlossaryRepository using pymysql."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from file_translator.domain.glossary import GlossaryEntry
from file_translator.domain.interfaces import GlossaryRepository

logger = logging.getLogger(__name__)

_TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_table_name(table_name: str) -> str:
    """Validate table name contains only safe characters (letters, digits, underscores).

    Raises ValueError if the name is invalid — prevents SQL injection via table name.
    """
    if not _TABLE_NAME_RE.match(table_name):
        raise ValueError(
            f"Invalid table name: '{table_name}'. "
            f"Only alphanumeric characters and underscores are allowed."
        )
    return table_name


class MySQLGlossaryRepository(GlossaryRepository):
    """Glossary storage backed by MySQL.

    Reads glossary credentials from environment variables:
      GLOSSARY_DB_HOST, GLOSSARY_DB_PORT, GLOSSARY_DB_USER,
      GLOSSARY_DB_PASSWORD, GLOSSARY_DB_NAME.

    The expected table ``glossary`` has columns:
      id, ru_word, en_word, sb_word, ch_word
    """

    def __init__(self, **kwargs):
        password = os.environ.get("GLOSSARY_DB_PASSWORD", "")
        if not password:
            logger.warning(
                "GLOSSARY_DB_PASSWORD not set — connection will likely fail. "
                "Set GLOSSARY_DB_PASSWORD environment variable."
            )
        self._connect_args = kwargs or {
            "host": os.environ.get("GLOSSARY_DB_HOST", "dbserver"),
            "port": int(os.environ.get("GLOSSARY_DB_PORT", "3306")),
            "user": os.environ.get("GLOSSARY_DB_USER", "glossary"),
            "password": password,
            "database": os.environ.get("GLOSSARY_DB_NAME", "glossary"),
            "cursorclass": DictCursor,
        }

    async def _run(self, query: str, params: tuple = ()) -> list[dict]:
        def _sync() -> list[dict]:
            conn = pymysql.connect(**self._connect_args)
            try:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    conn.commit()
                    return cur.fetchall()
            finally:
                conn.close()

        return await asyncio.to_thread(_sync)

    async def _run_insert(self, query: str, params: tuple = ()) -> int:
        def _sync() -> int:
            conn = pymysql.connect(**self._connect_args)
            try:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    conn.commit()
                    return cur.lastrowid
            finally:
                conn.close()

        return await asyncio.to_thread(_sync)

    async def _run_execute(self, query: str, params: tuple = ()) -> None:
        def _sync() -> None:
            conn = pymysql.connect(**self._connect_args)
            try:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    conn.commit()
            finally:
                conn.close()

        return await asyncio.to_thread(_sync)

    def _row_to_entry(self, row: dict) -> GlossaryEntry:
        return GlossaryEntry(
            id=int(row.get("id", 0)),
            ru_word=str(row.get("ru_word", "") or ""),
            en_word=str(row.get("en_word", "") or ""),
            sb_word=str(row.get("sb_word", "") or ""),
            ch_word=str(row.get("ch_word", "") or ""),
            created_by=str(row.get("created_by", "") or ""),
            created_at=row.get("created_at"),
            updated_by=str(row.get("updated_by", "") or ""),
            updated_at=row.get("updated_at"),
        )

    async def find_all(self, table_name: str = "glossary") -> list[Any]:
        _validate_table_name(table_name)
        rows = await self._run(f"SELECT * FROM {table_name} ORDER BY id")
        return [self._row_to_entry(r) for r in rows]

    async def find_by_id(self, entry_id: str, table_name: str = "glossary") -> Any | None:
        _validate_table_name(table_name)
        rows = await self._run(f"SELECT * FROM {table_name} WHERE id = %s", (entry_id,))
        if not rows:
            return None
        return self._row_to_entry(rows[0])

    async def add(self, entry: Any, table_name: str = "glossary", created_by: str = "") -> Any:
        _validate_table_name(table_name)
        if isinstance(entry, GlossaryEntry):
            ru, en, sb, ch = entry.ru_word, entry.en_word, entry.sb_word, entry.ch_word
        elif isinstance(entry, dict):
            ru, en, sb, ch = entry.get("ru_word", ""), entry.get("en_word", ""), entry.get("sb_word", ""), entry.get("ch_word", "")
        else:
            ru, en, sb, ch = getattr(entry, "ru_word", ""), getattr(entry, "en_word", ""), getattr(entry, "sb_word", ""), getattr(entry, "ch_word", "")

        now = datetime.now()
        new_id = await self._run_insert(
            f"INSERT INTO {table_name} (ru_word, en_word, sb_word, ch_word, created_by, created_at, updated_by, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (ru, en, sb, ch, created_by, now, "", now),
        )
        return GlossaryEntry(id=new_id, ru_word=ru, en_word=en, sb_word=sb, ch_word=ch, created_by=created_by, created_at=now, updated_by="", updated_at=now)

    async def update(self, entry: Any, table_name: str = "glossary", updated_by: str = "") -> Any | None:
        _validate_table_name(table_name)
        if isinstance(entry, GlossaryEntry):
            eid, ru, en, sb, ch = entry.id, entry.ru_word, entry.en_word, entry.sb_word, entry.ch_word
        elif isinstance(entry, dict):
            eid, ru, en, sb, ch = entry.get("id"), entry.get("ru_word"), entry.get("en_word"), entry.get("sb_word"), entry.get("ch_word")
        else:
            eid, ru, en, sb, ch = getattr(entry, "id", 0), getattr(entry, "ru_word", ""), getattr(entry, "en_word", ""), getattr(entry, "sb_word", ""), getattr(entry, "ch_word", "")

        now = datetime.now()
        await self._run_execute(
            f"UPDATE {table_name} SET ru_word=%s, en_word=%s, sb_word=%s, ch_word=%s, updated_by=%s, updated_at=%s WHERE id=%s",
            (ru, en, sb, ch, updated_by, now, eid),
        )
        return await self.find_by_id(str(eid), table_name=table_name)

    async def delete(self, entry_id: str, table_name: str = "glossary") -> bool:
        _validate_table_name(table_name)
        await self._run_execute(f"DELETE FROM {table_name} WHERE id = %s", (entry_id,))
        return True

    async def table_exists(self, table_name: str) -> bool:
        db = self._connect_args.get("database", "glossary")
        rows = await self._run(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (db, table_name),
        )
        return len(rows) > 0

    async def list_tables(self, pattern: str = "glossary_%") -> list[str]:
        db = self._connect_args.get("database", "glossary")
        rows = await self._run(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME LIKE %s",
            (db, pattern),
        )
        return [r["TABLE_NAME"] for r in rows]

    async def import_from_file(self, file_path: Path) -> int:
        raise NotImplementedError("Import from file — will be implemented later")

    async def export_to_file(self, file_path: Path) -> Path:
        raise NotImplementedError("Export to file — will be implemented later")
