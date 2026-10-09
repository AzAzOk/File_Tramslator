"""Collection-access enforcement at the glossary/job API boundary.

Verifies the HTTP contract of the runtime grant model:

- read endpoints (`/glossary`, `/glossary/{id}`, `/glossary/export`) return 403
  for a collection the user cannot read;
- write endpoints (`POST`, `PUT`, `DELETE`, `/glossary/import`) return 403 for a
  collection the user cannot write;
- export is checked unconditionally — a user with no AD groups is denied, not
  granted;
- jobs/translate silently fall back to `default` for an unreadable collection
  and record a journal warning instead of failing the job.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("GLOSSARY_DB_PASSWORD", "test-pass")

import file_translator.presentation.api.app as api_app
from file_translator.application.glossary_service import GlossaryService
from file_translator.application.schemas import GlossaryCreateSchema
from file_translator.domain.glossary import GlossaryEntry

from tests.integration.test_glossary_api import (  # noqa: F401 — shared fakes
    FakeAccessResolver,
    FakeCollectionRepo,
    FakeGlossaryRepo,
)


class DenyResolver(FakeAccessResolver):
    """Denies every collection except `default` (read-only)."""

    def __init__(self) -> None:
        super().__init__(readable={"default"}, writable=set())


def build(resolver: Any, existing: list[GlossaryEntry] | None = None) -> GlossaryService:
    return GlossaryService(
        repository=FakeGlossaryRepo(existing),
        collection_repository=FakeCollectionRepo(existing),
        access_resolver=resolver,
    )


def make_request(username: str = "tester", groups: list[str] | None = None) -> SimpleNamespace:
    user = SimpleNamespace(username=username, ldap_groups=groups, user_id=f"id-{username}")
    return SimpleNamespace(state=SimpleNamespace(auth=SimpleNamespace(user=user, username=username)))


def patch_service(monkeypatch: pytest.MonkeyPatch, svc: GlossaryService) -> None:
    journal = SimpleNamespace(log_info=AsyncMock(), log_warning=AsyncMock(), log_error=AsyncMock())
    monkeypatch.setattr(
        api_app,
        "translation_service",
        SimpleNamespace(
            get_glossary_service=AsyncMock(return_value=svc),
            journal_service=journal,
        ),
    )


ENTRY = GlossaryEntry(id=1, ru_word="Привет", en_word="hello", sb_word="Zdravo", ch_word="你好")
GOOD = GlossaryCreateSchema(ru_word="Тест", en_word="test", sb_word="test", ch_word="测试")


# --- Read endpoints ----------------------------------------------------------

@pytest.mark.asyncio
async def test_list_entries_denies_unreadable_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.list_glossary_entries(make_request(), "secret", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_entries_allows_readable_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    resp = await api_app.list_glossary_entries(make_request(), "default", None)
    assert resp.total == 1


@pytest.mark.asyncio
async def test_get_entry_denies_unreadable_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = GlossaryEntry(
        id=1, ru_word="Привет", en_word="hello", sb_word="Zdravo", ch_word="你好",
        collection_id="secret",
    )
    patch_service(monkeypatch, build(DenyResolver(), [entry]))
    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.get_glossary_entry(make_request(), "1", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_entry_allows_readable_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    resp = await api_app.get_glossary_entry(make_request(), "1", None)
    assert resp.id == 1


@pytest.mark.asyncio
async def test_export_denies_user_without_ad_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: export used to skip the check when the user had no groups."""
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.export_glossary(make_request(groups=[]), "secret", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_export_succeeds_for_readable_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    resp = await api_app.export_glossary(make_request(), "default", None)
    assert resp.media_type == "text/csv"
    assert "glossary_default.csv" in resp.headers["content-disposition"]


# --- Write endpoints ---------------------------------------------------------

@pytest.mark.asyncio
async def test_create_denies_unwritable_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver()))
    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.create_glossary_entry(make_request(), GOOD, "default", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_allows_writable_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(FakeAccessResolver()))
    resp = await api_app.create_glossary_entry(make_request(), GOOD, "default", None)
    assert resp.en_word == "test"


@pytest.mark.asyncio
async def test_update_denies_unwritable_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    from file_translator.application.schemas import GlossaryUpdateSchema

    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.update_glossary_entry(
            make_request(), "1",
            GlossaryUpdateSchema(ru_word="a", en_word="b", sb_word="c", ch_word="d"),
            "default", None,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_denies_unwritable_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.delete_glossary_entry(make_request(), "1", "default", None)
    assert exc.value.status_code == 403


# --- Jobs / translate fallback ----------------------------------------------

@pytest.mark.asyncio
async def test_job_collection_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    result = await api_app._resolve_job_collection_id(
        make_request().state.auth.user, "secret", "doc.docx",
    )
    assert result == "default"


@pytest.mark.asyncio
async def test_job_collection_kept_when_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    result = await api_app._resolve_job_collection_id(
        make_request().state.auth.user, "default", "doc.docx",
    )
    assert result == "default"


@pytest.mark.asyncio
async def test_job_collection_empty_stays_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    assert await api_app._resolve_job_collection_id(
        make_request().state.auth.user, "", "doc.docx",
    ) == ""


@pytest.mark.asyncio
async def test_job_fallback_logs_journal_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    await api_app._resolve_job_collection_id(
        make_request(username="joe").state.auth.user, "secret", "doc.docx",
    )
    api_app.translation_service.journal_service.log_warning.assert_awaited()


@pytest.mark.asyncio
async def test_job_fallback_survives_journal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A journal outage must not fail the translation job."""
    patch_service(monkeypatch, build(DenyResolver(), [ENTRY]))
    api_app.translation_service.journal_service.log_warning = AsyncMock(
        side_effect=RuntimeError("journal down")
    )
    result = await api_app._resolve_job_collection_id(
        make_request().state.auth.user, "secret", "doc.docx",
    )
    assert result == "default"


# --- Level thresholds (change user-rights-collection-levels, tasks 7.1 / 7.2) -
#
# The fakes above model access as one read/write switch; these tests drive the
# *real* resolver so each endpoint is hit at levels 0..3 of one collection.

from file_translator.infrastructure.auth.glossary_access_resolver import (  # noqa: E402
    GlossaryAccessResolver,
)
from file_translator.infrastructure.auth.role_config import (  # noqa: E402
    GrantDoc,
    GrantStore,
)


class FakeGrantRepo:
    def __init__(self, grants: list[GrantDoc]) -> None:
        self.grants = grants

    async def find_all(self) -> list[GrantDoc]:
        return list(self.grants)


class KnownCollectionRepo(FakeCollectionRepo):
    """Lists its collections — the import endpoint checks existence first."""

    def __init__(self, ids: list[str], entries: list[GlossaryEntry] | None = None) -> None:
        super().__init__(entries)
        self.ids = ids

    async def find_all(self) -> list[Any]:
        return [SimpleNamespace(id=i) for i in self.ids]


def levelled_service(
    level: int,
    *,
    existing: list[GlossaryEntry] | None = None,
    collections: tuple[str, ...] = ("dtd",),
) -> GlossaryService:
    """Real resolver + one personal grant of ``level`` on collection ``dtd``."""
    grant = GrantDoc(subject_type="user", subject="id-tester", collection="dtd", level=level)
    resolver = GlossaryAccessResolver(grant_store=GrantStore(FakeGrantRepo([grant])))
    return GlossaryService(
        repository=FakeGlossaryRepo(existing),
        collection_repository=KnownCollectionRepo(list(collections), existing),
        access_resolver=resolver,
    )


def upload_csv() -> SimpleNamespace:
    payload = "ru_word,en_word,sb_word,ch_word\nпривет,hello,zdravo,你好\n".encode("utf-8")
    return SimpleNamespace(read=AsyncMock(return_value=payload))


@pytest.mark.asyncio
async def test_create_denied_at_level_one(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, levelled_service(1))
    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.create_glossary_entry(make_request(), GOOD, "dtd", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_allowed_at_level_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Level 2 may add entries — that is the whole point of the middle level."""
    patch_service(monkeypatch, levelled_service(2))
    resp = await api_app.create_glossary_entry(make_request(), GOOD, "dtd", None)
    assert resp.en_word == "test"


@pytest.mark.asyncio
async def test_update_denied_below_level_three(monkeypatch: pytest.MonkeyPatch) -> None:
    from file_translator.application.schemas import GlossaryUpdateSchema

    payload = GlossaryUpdateSchema(ru_word="Привет", en_word="hello", sb_word="Zdravo", ch_word="你好")
    for level in (1, 2):
        patch_service(monkeypatch, levelled_service(level, existing=[ENTRY]))
        with pytest.raises(api_app.HTTPException) as exc:
            await api_app.update_glossary_entry(make_request(), "1", payload, "dtd", None)
        assert exc.value.status_code == 403, f"level {level} must not be able to update"


@pytest.mark.asyncio
async def test_update_allowed_at_level_three(monkeypatch: pytest.MonkeyPatch) -> None:
    from file_translator.application.schemas import GlossaryUpdateSchema

    patch_service(monkeypatch, levelled_service(3, existing=[ENTRY]))
    resp = await api_app.update_glossary_entry(
        make_request(),
        "1",
        GlossaryUpdateSchema(ru_word="Привет", en_word="hello", sb_word="Zdravo", ch_word="你好"),
        "dtd",
        None,
    )
    assert resp.id == 1


@pytest.mark.asyncio
async def test_delete_denied_below_level_three(monkeypatch: pytest.MonkeyPatch) -> None:
    for level in (1, 2):
        patch_service(monkeypatch, levelled_service(level, existing=[ENTRY]))
        with pytest.raises(api_app.HTTPException) as exc:
            await api_app.delete_glossary_entry(make_request(), "1", "dtd", None)
        assert exc.value.status_code == 403, f"level {level} must not be able to delete"


@pytest.mark.asyncio
async def test_delete_allowed_at_level_three(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, levelled_service(3, existing=[ENTRY]))
    resp = await api_app.delete_glossary_entry(make_request(), "1", "dtd", None)
    assert "deleted" in resp["detail"]


@pytest.mark.asyncio
async def test_import_denied_below_level_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """An import rewrites existing rows, so level 2 is not enough."""
    for level in (1, 2):
        patch_service(monkeypatch, levelled_service(level))
        with pytest.raises(api_app.HTTPException) as exc:
            await api_app.import_glossary(make_request(), upload_csv(), "dtd", "", None)
        assert exc.value.status_code == 403, f"level {level} must not be able to import"


@pytest.mark.asyncio
async def test_import_allowed_at_level_three(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, levelled_service(3))
    resp = await api_app.import_glossary(make_request(), upload_csv(), "dtd", "", None)
    assert resp.imported == 1
    assert resp.errors == []


@pytest.mark.asyncio
async def test_read_denied_at_level_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Level 0 is "no access": list, get and export all refuse."""
    dtd_entry = GlossaryEntry(
        id=1, ru_word="Привет", en_word="hello", sb_word="Zdravo", ch_word="你好",
        collection_id="dtd",
    )
    patch_service(monkeypatch, levelled_service(0, existing=[dtd_entry]))
    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.list_glossary_entries(make_request(), "dtd", None)
    assert exc.value.status_code == 403

    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.get_glossary_entry(make_request(), "1", None)
    assert exc.value.status_code == 403

    with pytest.raises(api_app.HTTPException) as exc:
        await api_app.export_glossary(make_request(), "dtd", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_read_allowed_at_level_one(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(monkeypatch, levelled_service(1, existing=[ENTRY]))
    resp = await api_app.list_glossary_entries(make_request(), "dtd", None)
    assert resp.total == 1


@pytest.mark.asyncio
async def test_job_read_threshold_keeps_level_one_and_falls_back_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_service(monkeypatch, levelled_service(1))
    kept = await api_app._resolve_job_collection_id(
        make_request().state.auth.user, "dtd", "doc.docx",
    )
    assert kept == "dtd"

    patch_service(monkeypatch, levelled_service(0))
    fallback = await api_app._resolve_job_collection_id(
        make_request().state.auth.user, "dtd", "doc.docx",
    )
    assert fallback == "default"
    api_app.translation_service.journal_service.log_warning.assert_awaited()
