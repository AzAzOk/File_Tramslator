"""API-level tests for glossary language-mismatch override.

These tests invoke the FastAPI route handlers directly (no live server) with
the real `GlossaryService` backed by fake repositories, and patch the
module-level `translation_service` singleton. They assert the HTTP contract:
`code="LANGUAGE_MISMATCH"` / `code="DUPLICATE"` in the error body, and that
`force_language` reaches the service.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

# Must be set before importing app (it fails at import without these).
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("GLOSSARY_DB_PASSWORD", "test-pass")

import file_translator.presentation.api.app as api_app
from file_translator.application.glossary_service import GlossaryService
from file_translator.application.schemas import GlossaryCreateSchema, GlossaryUpdateSchema
from file_translator.domain.glossary import GlossaryEntry


class FakeAccessResolver:
    """Allows every collection."""

    def is_collection_allowed(self, collection_id: str, groups: list[str] | None = None) -> bool:
        return True

    def resolve(self, groups: list[str] | None = None) -> list[str]:
        return ["default"]


class FakeGlossaryRepo:
    """Minimal GlossaryRepository fake for handler-level tests."""

    def __init__(self, existing: list[GlossaryEntry] | None = None) -> None:
        self.existing = existing or []
        self.added: list[GlossaryEntry] = []
        self.updated: list[GlossaryEntry] = []

    async def find_all(self, table_name: str = "glossary") -> list[Any]:
        return self.existing

    async def find_by_id(self, entry_id: str, table_name: str = "glossary") -> Any | None:
        for e in self.existing:
            if str(e.id) == str(entry_id):
                return e
        return None

    async def add(self, entry: Any, table_name: str = "glossary", created_by: str = "") -> Any:
        self.added.append(entry)
        # Emulate persistence: the real repo returns a stored entry with an id.
        return GlossaryEntry(
            id=len(self.existing) + len(self.added),
            ru_word=getattr(entry, "ru_word", "") or "",
            en_word=getattr(entry, "en_word", "") or "",
            sb_word=getattr(entry, "sb_word", "") or "",
            ch_word=getattr(entry, "ch_word", "") or "",
        )

    async def update(self, entry: Any, table_name: str = "glossary", updated_by: str = "") -> Any | None:
        self.updated.append(entry)
        return entry

    async def delete(self, entry_id: str, table_name: str = "glossary") -> bool:
        return True

    async def table_exists(self, table_name: str) -> bool:
        return True

    async def list_tables(self, pattern: str = "glossary_%") -> list[str]:
        return []

    async def import_from_file(self, file_path: Any) -> int:
        return 0

    async def export_to_file(self, file_path: Any) -> Any:
        return file_path


class FakeCollectionRepo:
    """Minimal GlossaryCollectionRepository fake."""

    def __init__(self, entries: list[GlossaryEntry] | None = None) -> None:
        self.entries = entries or []

    async def find_all(self) -> list[Any]:
        return []

    async def find_by_id(self, collection_id: str) -> Any | None:
        return None

    async def get_entries(self, collection_id: str) -> list[Any]:
        return self.entries


def build_service(existing: list[GlossaryEntry] | None = None) -> GlossaryService:
    return GlossaryService(
        repository=FakeGlossaryRepo(existing),
        collection_repository=FakeCollectionRepo(existing),
        access_resolver=FakeAccessResolver(),
    )


def make_request(username: str = "tester") -> SimpleNamespace:
    auth = SimpleNamespace(user=SimpleNamespace(username=username, ldap_groups=["all"]))
    return SimpleNamespace(state=SimpleNamespace(auth=auth))


def make_translation_service(glossary_service: GlossaryService) -> SimpleNamespace:
    return SimpleNamespace(
        get_glossary_service=AsyncMock(return_value=glossary_service),
        journal_service=SimpleNamespace(log_info=AsyncMock()),
    )


def parse_error_body(response: Any) -> dict:
    return json.loads(response.body)


def mismatched_create() -> GlossaryCreateSchema:
    return GlossaryCreateSchema(
        ru_word="Привет",
        en_word="Здравствуйте",  # Cyrillic in English column → mismatch
        sb_word="Zdravo",
        ch_word="你好",
    )


def good_create() -> GlossaryCreateSchema:
    return GlossaryCreateSchema(
        ru_word="Привет",
        en_word="hello",
        sb_word="Zdravo",
        ch_word="你好",
    )


@pytest.fixture
def patched_translation_service(monkeypatch: pytest.MonkeyPatch) -> GlossaryService:
    svc = build_service()
    monkeypatch.setattr(api_app, "translation_service", make_translation_service(svc))
    return svc


@pytest.mark.asyncio
async def test_post_language_mismatch_returns_structured_code(patched_translation_service) -> None:
    resp = await api_app.create_glossary_entry(
        make_request(), mismatched_create(), "default", None
    )
    assert resp.status_code == 400
    body = parse_error_body(resp)
    assert body["code"] == "LANGUAGE_MISMATCH"
    assert isinstance(body["detail"], str)
    assert body["detail"]


@pytest.mark.asyncio
async def test_post_duplicate_returns_structured_code(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = [GlossaryEntry(id=1, ru_word="Привет", en_word="hello", sb_word="Zdravo", ch_word="你好")]
    svc = build_service(existing)
    monkeypatch.setattr(api_app, "translation_service", make_translation_service(svc))

    resp = await api_app.create_glossary_entry(
        make_request(),
        GlossaryCreateSchema(
            ru_word="Привет",  # duplicates existing
            en_word="privet",
            sb_word="pozdrav",
            ch_word="普里韦特",
        ),
        "default",
        None,
    )
    assert resp.status_code == 400
    body = parse_error_body(resp)
    assert body["code"] == "DUPLICATE"
    assert isinstance(body["detail"], str)
    assert body["detail"]


@pytest.mark.asyncio
async def test_post_force_language_saves_mismatch(patched_translation_service) -> None:
    resp = await api_app.create_glossary_entry(
        make_request(), mismatched_create().model_copy(update={"force_language": True}), "default", None
    )
    assert isinstance(resp, api_app.GlossaryEntrySchema)
    assert resp.ru_word == "Привет"
    assert resp.en_word == "Здравствуйте"


@pytest.mark.asyncio
async def test_post_matching_entry_saves_without_override(patched_translation_service) -> None:
    resp = await api_app.create_glossary_entry(make_request(), good_create(), "default", None)
    assert isinstance(resp, api_app.GlossaryEntrySchema)
    assert resp.en_word == "hello"


@pytest.mark.asyncio
async def test_put_language_mismatch_returns_structured_code(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = [GlossaryEntry(id=1, ru_word="Привет", en_word="hello", sb_word="Zdravo", ch_word="你好")]
    svc = build_service(existing)
    monkeypatch.setattr(api_app, "translation_service", make_translation_service(svc))

    resp = await api_app.update_glossary_entry(
        make_request(),
        "1",
        GlossaryUpdateSchema(
            ru_word="Привет",
            en_word="Здравствуйте",  # mismatch
            sb_word="Zdravo",
            ch_word="你好",
        ),
        "default",
        None,
    )
    assert resp.status_code == 400
    body = parse_error_body(resp)
    assert body["code"] == "LANGUAGE_MISMATCH"
    assert isinstance(body["detail"], str)
    assert body["detail"]


@pytest.mark.asyncio
async def test_put_duplicate_returns_structured_code(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = [
        GlossaryEntry(id=1, ru_word="Привет", en_word="hello", sb_word="Zdravo", ch_word="你好"),
        GlossaryEntry(id=2, ru_word="Привет", en_word="x", sb_word="y", ch_word="z"),
    ]
    svc = build_service(existing)
    monkeypatch.setattr(api_app, "translation_service", make_translation_service(svc))

    # Updating id=1 with the SAME ru_word as id=2 → duplicate (id=1 excluded).
    resp = await api_app.update_glossary_entry(
        make_request(),
        "1",
        GlossaryUpdateSchema(
            ru_word="Привет",
            en_word="hello",
            sb_word="Zdravo",
            ch_word="你好",
        ),
        "default",
        None,
    )
    assert resp.status_code == 400
    body = parse_error_body(resp)
    assert body["code"] == "DUPLICATE"
    assert isinstance(body["detail"], str)
    assert body["detail"]


@pytest.mark.asyncio
async def test_put_force_language_saves_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = [GlossaryEntry(id=1, ru_word="Привет", en_word="hello", sb_word="Zdravo", ch_word="你好")]
    svc = build_service(existing)
    monkeypatch.setattr(api_app, "translation_service", make_translation_service(svc))

    resp = await api_app.update_glossary_entry(
        make_request(),
        "1",
        GlossaryUpdateSchema(
            ru_word="Привет",
            en_word="Здравствуйте",
            sb_word="Zdravo",
            ch_word="你好",
            force_language=True,
        ),
        "default",
        None,
    )
    assert isinstance(resp, api_app.GlossaryEntrySchema)
    assert resp.en_word == "Здравствуйте"