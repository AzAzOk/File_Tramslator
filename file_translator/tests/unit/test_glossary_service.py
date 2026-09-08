"""Unit tests for glossary service language-mismatch override behavior.

Covers the `force_language` flag on `add_entry` / `update_entry`:
- default behavior rejects language mismatch with `LanguageMismatchError`
- `force_language=True` allows the mismatch but the duplicate check still blocks
- the two error types are distinct (`LANGUAGE_MISMATCH` vs `DUPLICATE`)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from file_translator.application.glossary_service import (
    DuplicateError,
    GlossaryError,
    GlossaryService,
    LanguageMismatchError,
)
from file_translator.domain.glossary import GlossaryEntry


class FakeGlossaryRepo:
    """Minimal GlossaryRepository fake recording add/update calls."""

    def __init__(self) -> None:
        self.added: list[GlossaryEntry] = []
        self.updated: list[GlossaryEntry] = []

    async def find_all(self, table_name: str = "glossary") -> list[Any]:
        return []

    async def find_by_id(self, entry_id: str, table_name: str = "glossary") -> Any | None:
        return None

    async def add(self, entry: Any, table_name: str = "glossary", created_by: str = "") -> Any:
        self.added.append(entry)
        return entry

    async def update(self, entry: Any, table_name: str = "glossary", updated_by: str = "") -> Any | None:
        self.updated.append(entry)
        return entry

    async def delete(self, entry_id: str, table_name: str = "glossary") -> bool:
        return True

    async def table_exists(self, table_name: str) -> bool:
        return True

    async def list_tables(self, pattern: str = "glossary_%") -> list[str]:
        return []

    async def import_from_file(self, file_path: Path) -> int:
        return 0

    async def export_to_file(self, file_path: Path) -> Path:
        return file_path


class FakeCollectionRepo:
    """Minimal GlossaryCollectionRepository fake returning pre-seeded entries."""

    def __init__(self, entries: list[GlossaryEntry] | None = None) -> None:
        self.entries = entries or []

    async def find_all(self) -> list[Any]:
        return []

    async def find_by_id(self, collection_id: str) -> Any | None:
        return None

    async def get_entries(self, collection_id: str) -> list[Any]:
        return self.entries


def make_service(existing: list[GlossaryEntry] | None = None) -> tuple[GlossaryService, FakeGlossaryRepo]:
    repo = FakeGlossaryRepo()
    coll_repo = FakeCollectionRepo(existing)
    svc = GlossaryService(repository=repo, collection_repository=coll_repo)
    return svc, repo


def mismatched_entry() -> GlossaryEntry:
    """en_word contains Cyrillic text → language mismatch (detected 'ru' ≠ 'en')."""
    return GlossaryEntry(
        id=0,
        ru_word="Привет",
        en_word="Здравствуйте",
        sb_word="Zdravo",
        ch_word="你好",
    )


def good_entry() -> GlossaryEntry:
    return GlossaryEntry(
        id=0,
        ru_word="Привет",
        en_word="hello",
        sb_word="Zdravo",
        ch_word="你好",
    )


def existing_entry() -> GlossaryEntry:
    return GlossaryEntry(
        id=1,
        ru_word="Существующее",
        en_word="existing",
        sb_word="Postojece",
        ch_word="现有",
    )


@pytest.mark.asyncio
async def test_default_rejects_language_mismatch_on_add() -> None:
    svc, repo = make_service()
    with pytest.raises(LanguageMismatchError):
        await svc.add_entry(mismatched_entry(), collection_id="default")
    assert repo.added == []


@pytest.mark.asyncio
async def test_default_allows_matching_entry() -> None:
    svc, repo = make_service()
    result = await svc.add_entry(good_entry(), collection_id="default")
    assert repo.added == [good_entry()]
    assert result == good_entry()


@pytest.mark.asyncio
async def test_force_language_allows_mismatch_on_add() -> None:
    svc, repo = make_service()
    entry = mismatched_entry()
    result = await svc.add_entry(entry, collection_id="default", force_language=True)
    assert result == entry
    assert repo.added == [entry]


@pytest.mark.asyncio
async def test_force_language_still_blocks_duplicate_on_add() -> None:
    existing = [
        GlossaryEntry(
            id=1,
            ru_word="Привет",
            en_word="hello",
            sb_word="Zdravo",
            ch_word="你好",
        )
    ]
    svc, repo = make_service(existing)
    dup = GlossaryEntry(
        id=0,
        ru_word="Привет",  # duplicates existing ru_word
        en_word="privet",
        sb_word="pozdrav",
        ch_word="普里韦特",
    )
    with pytest.raises(DuplicateError):
        await svc.add_entry(dup, collection_id="default", force_language=True)
    assert repo.added == []


@pytest.mark.asyncio
async def test_default_rejects_language_mismatch_on_update() -> None:
    svc, repo = make_service()
    with pytest.raises(LanguageMismatchError):
        await svc.update_entry(mismatched_entry(), collection_id="default")
    assert repo.updated == []


@pytest.mark.asyncio
async def test_force_language_allows_mismatch_on_update() -> None:
    svc, repo = make_service()
    entry = mismatched_entry()
    result = await svc.update_entry(entry, collection_id="default", force_language=True)
    assert result == entry
    assert repo.updated == [entry]


@pytest.mark.asyncio
async def test_force_language_still_blocks_duplicate_on_update() -> None:
    svc, repo = make_service([existing_entry()])
    dup = GlossaryEntry(
        id=2,
        ru_word="Существующее",  # duplicates existing ru_word
        en_word="new word",
        sb_word="nova rec",
        ch_word="新词",
    )
    with pytest.raises(DuplicateError):
        await svc.update_entry(dup, collection_id="default", force_language=True)
    assert repo.updated == []


def test_error_codes_are_distinct() -> None:
    assert LanguageMismatchError("x").code == "LANGUAGE_MISMATCH"
    assert DuplicateError("x").code == "DUPLICATE"
    assert LanguageMismatchError("x").code != DuplicateError("x").code


def test_glossary_error_base() -> None:
    err = GlossaryError("сообщение")
    assert err.message == "сообщение"
    assert err.detail == "сообщение"