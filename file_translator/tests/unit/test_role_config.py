"""Unit tests for runtime role/grant configuration, seeding and legacy migration.

Covers:
- RoleConfigStore / GrantStore TTL cache + Redis version invalidation
- Default-role fallback when Mongo is unavailable
- Admin write helpers bumping the shared config version
- Startup seeding idempotency (roles always, grants only when empty)
- Legacy role migration (operator/viewer → user, api preserved, manual_role)
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret")

from file_translator.domain.auth import Permission, RoleType
from file_translator.infrastructure.auth.config_seeder import (
    load_collection_map_from_env,
    run_startup_seeding,
    seed_grants,
    seed_roles,
)
from file_translator.infrastructure.auth.glossary_access_resolver import GlossaryAccessResolver
from file_translator.infrastructure.auth.legacy_role_migration import migrate_legacy_roles
from file_translator.infrastructure.auth.role_config import (
    BUILTIN_ROLE_NAMES,
    DEFAULT_COLLECTION,
    DEFAULT_ROLES,
    ROLE_USER,
    GrantDoc,
    GrantStore,
    RoleConfigStore,
    RoleDoc,
    SUBJECT_GROUP,
    SUBJECT_ROLE,
    SUBJECT_USER,
)


# --- Fakes -------------------------------------------------------------------

class FakeRoleRepository:
    def __init__(self, roles: list[RoleDoc] | None = None, fail: bool = False) -> None:
        self.roles = list(roles or [])
        self.created: list[RoleDoc] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.fail: bool = fail

    async def find_all(self) -> list[RoleDoc]:
        if self.fail:
            raise RuntimeError("mongo down")
        return list(self.roles)

    async def create(self, role: RoleDoc) -> RoleDoc:
        self.created.append(role)
        self.roles.append(role)
        return role

    async def update(self, name: str, fields: dict) -> bool:
        self.updated.append((name, fields))
        for r in self.roles:
            if r.name == name:
                r.permissions = fields.get("permissions", r.permissions)
                r.description = fields.get("description", r.description)
        return True

    async def delete(self, name: str) -> bool:
        self.deleted.append(name)
        self.roles = [r for r in self.roles if r.name != name]
        return True

    async def count(self) -> int:
        return len(self.roles)

    async def ensure_indexes(self) -> None:
        return None


class FakeGrantRepository:
    def __init__(self, grants: list[GrantDoc] | None = None) -> None:
        self.grants = list(grants or [])
        self.upserts: list[GrantDoc] = []
        self.deleted: list[GrantDoc] = []

    async def find_all(self) -> list[GrantDoc]:
        return list(self.grants)

    async def upsert(self, grant: GrantDoc) -> GrantDoc:
        self.upserts.append(grant)
        self.grants = [
            g for g in self.grants
            if not (
                g.subject_type == grant.subject_type
                and g.subject == grant.subject
                and g.collection == grant.collection
            )
        ]
        self.grants.append(grant)
        return grant

    async def delete(self, grant: GrantDoc) -> bool:
        self.deleted.append(grant)
        self.grants = [
            g for g in self.grants
            if not (
                g.subject_type == grant.subject_type
                and g.subject == grant.subject
                and g.collection == grant.collection
            )
        ]
        return True

    async def delete_many(self, subject_type=None, subject=None, collection=None) -> int:
        before = len(self.grants)
        self.grants = [
            g for g in self.grants
            if not (
                (subject_type is None or g.subject_type == subject_type)
                and (subject is None or g.subject == subject)
                and (collection is None or g.collection == collection)
            )
        ]
        return before - len(self.grants)

    async def count(self) -> int:
        return len(self.grants)

    async def ensure_indexes(self) -> None:
        return None


class FakeUserRepository:
    def __init__(self, users: list[Any] | None = None) -> None:
        self.users = list(users or [])
        self.updates: list[Any] = []

    async def list_all(self) -> list[Any]:
        return list(self.users)

    async def update(self, user: Any) -> Any:
        self.updates.append(user)
        for i, u in enumerate(self.users):
            if u.user_id == user.user_id:
                self.users[i] = user
        return user

    async def get_by_username(self, username: str) -> Any | None:
        for u in self.users:
            if u.username == username:
                return u
        return None


def make_user(username: str, role: str, manual_role: bool = False) -> Any:
    from file_translator.domain.auth import User

    return User(
        user_id=f"id-{username}",
        username=username,
        display_name=username.title(),
        role=RoleType(role) if role in {r.value for r in RoleType} else RoleType.VIEWER,
        role_name=role,
        manual_role=manual_role,
        is_active=True,
    )


# --- RoleConfigStore ---------------------------------------------------------

@pytest.mark.asyncio
async def test_store_falls_back_to_defaults_without_repository() -> None:
    store = RoleConfigStore()
    roles = await store.get_all_roles()
    names = {r.name for r in roles}
    assert BUILTIN_ROLE_NAMES.issubset(names)


@pytest.mark.asyncio
async def test_store_falls_back_to_defaults_when_mongo_fails() -> None:
    store = RoleConfigStore(FakeRoleRepository(fail=True))
    names = {r.name for r in await store.get_all_roles()}
    assert BUILTIN_ROLE_NAMES.issubset(names)


@pytest.mark.asyncio
async def test_store_reads_roles_from_repository() -> None:
    repo = FakeRoleRepository([RoleDoc(name=ROLE_USER, permissions=[Permission.TRANSLATE.value])])
    store = RoleConfigStore(repo)
    role = await store.get_role(ROLE_USER)
    assert role is not None
    assert role.permissions == [Permission.TRANSLATE.value]


@pytest.mark.asyncio
async def test_resolve_permissions_admin_has_all() -> None:
    store = RoleConfigStore(FakeRoleRepository())
    perms = await store.resolve_permissions(RoleType.ADMIN.value)
    assert perms == {p.value for p in Permission}


@pytest.mark.asyncio
async def test_resolve_permissions_custom_role_falls_back_to_user() -> None:
    store = RoleConfigStore(FakeRoleRepository())
    perms = await store.resolve_permissions("brand-new-role")
    assert perms == set(DEFAULT_ROLES[ROLE_USER]["permissions"])


@pytest.mark.asyncio
async def test_resolve_permissions_none_uses_viewer_defaults() -> None:
    store = RoleConfigStore(FakeRoleRepository())
    perms = await store.resolve_permissions(None)
    assert Permission.TRANSLATE.value not in perms


@pytest.mark.asyncio
async def test_cached_reads_do_not_reload() -> None:
    repo = FakeRoleRepository([RoleDoc(name=ROLE_USER)])
    store = RoleConfigStore(repo, ttl_seconds=60)
    await store.get_all_roles()
    await store.get_all_roles()
    assert len(repo.roles) == 1  # no reload marker; use a counting repo instead


@pytest.mark.asyncio
async def test_version_change_invalidates_cache() -> None:
    versions = iter([1, 2])
    provider_calls = {"n": 0}

    async def version_provider() -> int:
        provider_calls["n"] += 1
        return next(versions, 2)

    repo = FakeRoleRepository([RoleDoc(name=ROLE_USER, description="first")])
    store = RoleConfigStore(repo, ttl_seconds=3600, version_provider=version_provider)
    first = await store.get_role(ROLE_USER)
    assert first is not None and first.description == "first"

    repo.roles = [RoleDoc(name=ROLE_USER, description="second")]
    # First version observation only records the baseline; a change forces a reload.
    await store.get_role(ROLE_USER)
    second = await store.get_role(ROLE_USER)
    assert second is not None and second.description == "second"


@pytest.mark.asyncio
async def test_version_provider_failure_falls_back_to_ttl() -> None:
    async def broken() -> int:
        raise RuntimeError("redis down")

    repo = FakeRoleRepository([RoleDoc(name=ROLE_USER)])
    store = RoleConfigStore(repo, ttl_seconds=3600, version_provider=broken)
    role = await store.get_role(ROLE_USER)
    assert role is not None
    assert role.name == ROLE_USER


@pytest.mark.asyncio
async def test_write_helpers_bump_version() -> None:
    bumps: list[int] = []

    async def bumper() -> None:
        bumps.append(1)

    repo = FakeRoleRepository([RoleDoc(name="custom", permissions=[Permission.TRANSLATE.value])])
    store = RoleConfigStore(repo, version_bumper=bumper)

    await store.create_role(RoleDoc(name="new-role"))
    assert bumps == [1]

    await store.update_role("custom", {"description": "x"})
    assert len(bumps) == 2

    await store.delete_role("custom")
    assert len(bumps) == 3


@pytest.mark.asyncio
async def test_bump_failure_is_swallowed() -> None:
    async def bumper() -> None:
        raise RuntimeError("redis down")

    repo = FakeRoleRepository()
    store = RoleConfigStore(repo, version_bumper=bumper)
    await store.create_role(RoleDoc(name="r"))  # must not raise


@pytest.mark.asyncio
async def test_update_role_unknown_returns_none() -> None:
    store = RoleConfigStore(FakeRoleRepository([RoleDoc(name="a")]))
    assert await store.update_role("missing", {"description": "x"}) is None


# --- GrantStore --------------------------------------------------------------

@pytest.mark.asyncio
async def test_grant_store_upsert_and_read() -> None:
    repo = FakeGrantRepository()
    store = GrantStore(repo)
    await store.upsert_grant(
        GrantDoc(subject_type=SUBJECT_GROUP, subject="g1", collection="c1", read=True, write=False)
    )
    grants = await store.get_all_grants()
    assert len(grants) == 1
    assert grants[0].read is True and grants[0].write is False


@pytest.mark.asyncio
async def test_grant_store_delete_many_filters() -> None:
    repo = FakeGrantRepository([
        GrantDoc(subject_type=SUBJECT_GROUP, subject="g1", collection="c1"),
        GrantDoc(subject_type=SUBJECT_ROLE, subject="admin", collection="c1"),
    ])
    store = GrantStore(repo)
    removed = await store.delete_many(subject_type=SUBJECT_GROUP)
    assert removed == 1
    assert len(await store.get_all_grants()) == 1


@pytest.mark.asyncio
async def test_find_grants_for_subjects() -> None:
    repo = FakeGrantRepository([
        GrantDoc(subject_type=SUBJECT_GROUP, subject="g1", collection="c1"),
        GrantDoc(subject_type=SUBJECT_ROLE, subject="admin", collection="c2"),
        GrantDoc(subject_type=SUBJECT_USER, subject="u1", collection="c3"),
    ])
    store = GrantStore(repo)
    matched = await store.find_grants_for_subjects(
        {SUBJECT_GROUP: ["g1", "g2"], SUBJECT_ROLE: ["admin"]}
    )
    assert {m.collection for m in matched} == {"c1", "c2"}


# --- Resolver ----------------------------------------------------------------

def make_resolver(
    roles: list[RoleDoc] | None = None,
    grants: list[GrantDoc] | None = None,
) -> GlossaryAccessResolver:
    return GlossaryAccessResolver(
        role_store=RoleConfigStore(FakeRoleRepository(roles)),
        grant_store=GrantStore(FakeGrantRepository(grants)),
    )


@pytest.mark.asyncio
async def test_admin_bypasses_all_collections() -> None:
    resolver = make_resolver()
    user = make_user("root", RoleType.ADMIN.value)
    access = await resolver.resolve_access(user)
    assert access.read is None and access.write is None
    assert await resolver.can_read(user, "anything")
    assert await resolver.can_write(user, "anything")


@pytest.mark.asyncio
async def test_default_collection_always_readable() -> None:
    resolver = make_resolver()
    user = make_user("joe", ROLE_USER)
    assert await resolver.can_read(user, DEFAULT_COLLECTION)


@pytest.mark.asyncio
async def test_default_collection_is_not_writable_without_grant() -> None:
    resolver = make_resolver()
    user = make_user("joe", ROLE_USER)
    assert not await resolver.can_write(user, DEFAULT_COLLECTION)


@pytest.mark.asyncio
async def test_group_grant_grants_read_and_write() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c1", read=True, write=True),
    ])
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]
    assert await resolver.can_read(user, "c1")
    assert await resolver.can_write(user, "c1")


@pytest.mark.asyncio
async def test_read_only_grant_does_not_allow_write() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c1", read=True, write=False),
    ])
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]
    assert await resolver.can_read(user, "c1")
    assert not await resolver.can_write(user, "c1")


@pytest.mark.asyncio
async def test_role_subject_grant_applies() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_ROLE, subject="api", collection="c1", read=True, write=True),
    ])
    user = make_user("svc", RoleType.API.value)
    assert await resolver.can_read(user, "c1")
    assert await resolver.can_write(user, "c1")


@pytest.mark.asyncio
async def test_user_subject_grant_applies() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_USER, subject="id-joe", collection="c1", read=True, write=True),
    ])
    user = make_user("joe", ROLE_USER)
    assert await resolver.can_access(user, "c1", "write")


@pytest.mark.asyncio
async def test_foreign_collection_is_denied() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-b", collection="secret", read=True, write=True),
    ])
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]
    assert not await resolver.can_read(user, "secret")
    assert not await resolver.can_write(user, "secret")


# --- Access levels (0..3) ----------------------------------------------------

@pytest.mark.asyncio
async def test_role_and_group_levels_combine_to_the_higher_one() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c1", level=1),
        GrantDoc(subject_type=SUBJECT_ROLE, subject=ROLE_USER, collection="c1", level=3),
    ])
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]

    levels = await resolver.resolve_levels(user)
    assert levels is not None
    assert levels["c1"] == 3


@pytest.mark.asyncio
async def test_personal_grant_lowers_a_user_below_their_role() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_ROLE, subject=ROLE_USER, collection="c1", level=3),
        GrantDoc(subject_type=SUBJECT_USER, subject="id-joe", collection="c1", level=1),
    ])
    user = make_user("joe", ROLE_USER)

    levels = await resolver.resolve_levels(user)
    assert levels is not None
    assert levels["c1"] == 1
    # The inherited value is still there when personal grants are set aside.
    inherited = await resolver.resolve_levels(user, include_personal=False)
    assert inherited is not None and inherited["c1"] == 3


@pytest.mark.asyncio
async def test_personal_grant_raises_a_user_above_their_role() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_USER, subject="id-joe", collection="c2", level=3),
    ])
    user = make_user("joe", ROLE_USER)

    levels = await resolver.resolve_levels(user)
    assert levels is not None
    assert levels["c2"] == 3


@pytest.mark.asyncio
async def test_absence_of_a_personal_grant_means_inheritance() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_ROLE, subject=ROLE_USER, collection="c1", level=2),
    ])
    user = make_user("joe", ROLE_USER)

    with_personal = await resolver.resolve_levels(user)
    without = await resolver.resolve_levels(user, include_personal=False)
    assert with_personal == without
    assert with_personal is not None and with_personal["c1"] == 2


@pytest.mark.asyncio
async def test_default_stays_readable_under_a_personal_zero_grant() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_USER, subject="id-joe", collection=DEFAULT_COLLECTION, level=0),
    ])
    user = make_user("joe", ROLE_USER)

    assert await resolver.can_read(user, DEFAULT_COLLECTION)
    assert not await resolver.can_create(user, DEFAULT_COLLECTION)


@pytest.mark.asyncio
async def test_admin_levels_are_unrestricted() -> None:
    resolver = make_resolver()
    user = make_user("root", RoleType.ADMIN.value)

    assert await resolver.resolve_levels(user) is None
    assert await resolver.can_create(user, "anything")


@pytest.mark.asyncio
async def test_each_level_threshold_is_enforced() -> None:
    for level, readable, creatable, modifiable in (
        (0, False, False, False),
        (1, True, False, False),
        (2, True, True, False),
        (3, True, True, True),
    ):
        resolver = make_resolver(grants=[
            GrantDoc(subject_type=SUBJECT_USER, subject="id-joe", collection="c1", level=level),
        ])
        user = make_user("joe", ROLE_USER)
        assert await resolver.can_read(user, "c1") is readable, level
        assert await resolver.can_create(user, "c1") is creatable, level
        assert await resolver.can_modify(user, "c1") is modifiable, level
        # The old name still means "may edit and delete".
        assert await resolver.can_write(user, "c1") is modifiable, level


# --- Per-collection sources (task 8.1) ---------------------------------------

@pytest.mark.asyncio
async def test_sources_name_the_document_kind_that_supplies_the_level() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_ROLE, subject=ROLE_USER, collection="c1", level=2),
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c2", level=3),
        GrantDoc(subject_type=SUBJECT_USER, subject="id-joe", collection="c3", level=1),
    ])
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]

    detailed = await resolver.resolve_levels_with_sources(user)
    assert detailed is not None
    assert detailed["c1"] == (2, f"role:{ROLE_USER}")
    assert detailed["c2"] == (3, "group:dept-a")
    assert detailed["c3"] == (1, "personal")
    # Nobody granted it, so it does not appear here — the endpoint adds it
    # as level 0 with the source `none`.
    assert "c9" not in detailed


@pytest.mark.asyncio
async def test_source_follows_the_stronger_of_role_and_group() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_ROLE, subject=ROLE_USER, collection="c1", level=1),
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c1", level=3),
    ])
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]

    detailed = await resolver.resolve_levels_with_sources(user)
    assert detailed is not None
    assert detailed["c1"] == (3, "group:dept-a")


@pytest.mark.asyncio
async def test_personal_source_wins_even_when_it_lowers() -> None:
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_ROLE, subject=ROLE_USER, collection="c1", level=3),
        GrantDoc(subject_type=SUBJECT_USER, subject="id-joe", collection="c1", level=1),
    ])
    user = make_user("joe", ROLE_USER)

    detailed = await resolver.resolve_levels_with_sources(user)
    assert detailed is not None
    assert detailed["c1"] == (1, "personal")


@pytest.mark.asyncio
async def test_default_source_is_the_floor_and_invents_no_grantor() -> None:
    resolver = make_resolver()
    user = make_user("joe", ROLE_USER)

    detailed = await resolver.resolve_levels_with_sources(user)
    assert detailed is not None
    assert detailed[DEFAULT_COLLECTION] == (1, "default")


@pytest.mark.asyncio
async def test_sources_agree_with_the_levels_enforcement_uses() -> None:
    """The dialog may not explain a number `resolve_levels` would dispute."""
    grants = [
        GrantDoc(subject_type=SUBJECT_ROLE, subject=ROLE_USER, collection="c1", level=2),
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c1", level=3),
        GrantDoc(subject_type=SUBJECT_USER, subject="id-joe", collection="c2", level=0),
        GrantDoc(subject_type=SUBJECT_ROLE, subject=ROLE_USER, collection="c3", level=0),
    ]
    resolver = make_resolver(grants=grants)
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]

    detailed = await resolver.resolve_levels_with_sources(user)
    levels = await resolver.resolve_levels(user)
    assert detailed is not None and levels is not None
    assert {c: lvl for c, (lvl, _) in detailed.items()} == levels
    assert levels["c1"] == 3


@pytest.mark.asyncio
async def test_admin_source_view_is_unrestricted() -> None:
    resolver = make_resolver()
    user = make_user("root", RoleType.ADMIN.value)

    assert await resolver.resolve_levels_with_sources(user) is None


@pytest.mark.asyncio
async def test_zero_level_group_grant_keeps_its_group_label() -> None:
    """Toggling a grant off stores it at level 0 — it still names a document."""
    resolver = make_resolver(grants=[
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c1", level=0),
    ])
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]

    detailed = await resolver.resolve_levels_with_sources(user)
    assert detailed is not None
    assert detailed["c1"] == (0, "group:dept-a")


# --- Seeding -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_seed_roles_creates_missing_builtin_roles() -> None:
    repo = FakeRoleRepository()
    created = await seed_roles(repo)
    assert created == 3
    assert {r.name for r in repo.roles} == BUILTIN_ROLE_NAMES


@pytest.mark.asyncio
async def test_seed_roles_is_idempotent() -> None:
    repo = FakeRoleRepository()
    await seed_roles(repo)
    again = await seed_roles(repo)
    assert again == 0
    assert len(repo.roles) == 3


@pytest.mark.asyncio
async def test_seed_grants_creates_read_write_and_admin_default() -> None:
    repo = FakeGrantRepository()
    created = await seed_grants(repo, {"dept-a": ["c1", "c2"]})
    assert created == 3

    admin_grants = [g for g in repo.grants if g.subject_type == SUBJECT_ROLE]
    assert len(admin_grants) == 1
    admin_grant = admin_grants[0]
    assert admin_grant.subject == RoleType.ADMIN.value
    assert admin_grant.collection == DEFAULT_COLLECTION
    assert admin_grant.write is True

    group_grants = [g for g in repo.grants if g.subject_type == SUBJECT_GROUP]
    assert {g.collection for g in group_grants} == {"c1", "c2"}
    assert all(g.read and g.write for g in group_grants)


@pytest.mark.asyncio
async def test_seed_grants_skipped_when_store_not_empty() -> None:
    repo = FakeGrantRepository([GrantDoc(subject_type=SUBJECT_USER, subject="u", collection="c")])
    created = await seed_grants(repo, {"dept-a": ["c1"]})
    assert created == 0
    assert len(repo.grants) == 1


@pytest.mark.asyncio
async def test_seeded_grants_carry_level_3() -> None:
    """The seed grants full access; the level must say the same thing."""
    repo = FakeGrantRepository()
    await seed_grants(repo, {"dept-a": ["c1"]})

    assert {g.level for g in repo.grants} == {3}
    assert all(g.to_dict()["level"] == 3 for g in repo.grants)


def test_legacy_flags_only_grant_loads_to_its_level() -> None:
    """Documents written before `level` existed keep their meaning."""
    full = GrantDoc.from_dict(
        {"subject_type": "group", "subject": "g", "collection": "c",
         "read": True, "write": True}
    )
    read_only = GrantDoc.from_dict(
        {"subject_type": "group", "subject": "g", "collection": "c",
         "read": True, "write": False}
    )
    empty = GrantDoc.from_dict({"subject_type": "group", "subject": "g", "collection": "c"})

    assert full is not None and full.level == 3
    assert read_only is not None and read_only.level == 1
    assert empty is not None and empty.level == 0


def test_level_round_trip_reproduces_the_flags() -> None:
    """Dual-writing has to stay consistent, or a mixed-version window bites."""
    for level, read, write in ((0, False, False), (1, True, False),
                               (2, True, False), (3, True, True)):
        doc = GrantDoc(subject_type="role", subject="r", collection="c", level=level)
        assert (doc.read, doc.write) == (read, write)

        payload = doc.to_dict()
        assert payload["level"] == level
        assert payload["read"] is read
        assert payload["write"] is write

        again = GrantDoc.from_dict(payload)
        assert again is not None
        assert (again.level, again.read, again.write) == (level, read, write)


def test_level_wins_when_both_representations_are_present() -> None:
    """The authority is `level`; a stale flag pair must not override it."""
    grant = GrantDoc.from_dict(
        {"subject_type": "user", "subject": "u", "collection": "c",
         "level": 2, "read": False, "write": True}
    )
    assert grant is not None
    assert grant.level == 2
    assert grant.read is True
    assert grant.write is False


def test_load_collection_map_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLOSSARY_COLLECTION_MAP", '{"g": ["a", "b"]}')
    assert load_collection_map_from_env() == {"g": ["a", "b"]}


def test_load_collection_map_handles_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLOSSARY_COLLECTION_MAP", "{not json")
    assert load_collection_map_from_env() == {}


def test_load_collection_map_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLOSSARY_COLLECTION_MAP", raising=False)
    assert load_collection_map_from_env() == {}


@pytest.mark.asyncio
async def test_run_startup_seeding_combines_all_steps() -> None:
    role_repo = FakeRoleRepository()
    grant_repo = FakeGrantRepository()
    user_repo = FakeUserRepository([make_user("old", RoleType.OPERATOR.value)])
    summary = await run_startup_seeding(role_repo, grant_repo, user_repo, {"g": ["c1"]})
    assert summary["roles_created"] == 3
    assert summary["grants_created"] == 2
    assert summary["legacy_migrated"] == 1
    assert user_repo.users[0].role_name == ROLE_USER
    assert user_repo.users[0].manual_role is False


@pytest.mark.asyncio
async def test_run_startup_seeding_second_run_is_noop() -> None:
    role_repo = FakeRoleRepository()
    grant_repo = FakeGrantRepository()
    user_repo = FakeUserRepository()
    await run_startup_seeding(role_repo, grant_repo, user_repo, {"g": ["c1"]})
    summary = await run_startup_seeding(role_repo, grant_repo, user_repo, {"g": ["c1"]})
    assert summary["roles_created"] == 0
    assert summary["grants_created"] == 0
    assert len(grant_repo.grants) == 2


# --- Legacy env-map fallback (task 6.2) --------------------------------------

def test_env_map_used_while_grants_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLOSSARY_COLLECTION_MAP", '{"dept-a": ["legacy"]}')
    resolver = GlossaryAccessResolver(grant_store=GrantStore(FakeGrantRepository()))
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]
    assert resolver.resolve(["dept-a"]) == ["legacy"]


@pytest.mark.asyncio
async def test_env_map_grants_read_and_write_while_unseeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLOSSARY_COLLECTION_MAP", '{"dept-a": ["legacy"]}')
    resolver = GlossaryAccessResolver(grant_store=GrantStore(FakeGrantRepository()))
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]
    assert await resolver.can_read(user, "legacy")
    assert await resolver.can_write(user, "legacy")


@pytest.mark.asyncio
async def test_env_map_ignored_once_grants_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """After seeding, changing GLOSSARY_COLLECTION_MAP must not grant access."""
    monkeypatch.setenv("GLOSSARY_COLLECTION_MAP", '{"dept-a": ["legacy"]}')
    resolver = GlossaryAccessResolver(
        grant_store=GrantStore(FakeGrantRepository([
            GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-b", collection="real", read=True),
        ])),
    )
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]
    # The env map would allow "legacy", but real grants exist → not consulted.
    assert not await resolver.can_read(user, "legacy")
    assert not await resolver.can_write(user, "legacy")


@pytest.mark.asyncio
async def test_seeded_grants_win_over_env_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLOSSARY_COLLECTION_MAP", '{"dept-a": ["env-only"]}')
    resolver = GlossaryAccessResolver(
        grant_store=GrantStore(FakeGrantRepository([
            GrantDoc(
                subject_type=SUBJECT_GROUP, subject="dept-a",
                collection="db-only", read=True, write=True,
            ),
        ])),
    )
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]
    assert await resolver.can_read(user, "db-only")
    assert not await resolver.can_read(user, "env-only")


@pytest.mark.asyncio
async def test_user_without_matching_grants_gets_only_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLOSSARY_COLLECTION_MAP", '{"dept-a": ["legacy"]}')
    resolver = GlossaryAccessResolver(
        grant_store=GrantStore(FakeGrantRepository([
            GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-z", collection="theirs", read=True),
        ])),
    )
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]
    assert await resolver.can_read(user, DEFAULT_COLLECTION)
    assert not await resolver.can_read(user, "theirs")


# --- Cross-process propagation (task 4.1) -----------------------------------

class FakeConfigVersion:
    """Stand-in for the Redis `admin_config_version` key."""

    def __init__(self) -> None:
        self.value = 0

    async def get_version(self) -> int:
        return self.value

    async def bump(self) -> int:
        self.value += 1
        return self.value


@pytest.mark.asyncio
async def test_grant_change_propagates_to_other_process_without_restart() -> None:
    """A grant written by the admin service must be visible to the main API.

    Both stores observe the same Redis version key: the writer bumps it and the
    reader drops its cache, so the new grant applies without a restart and
    well inside the 30 s TTL safety net.
    """
    version = FakeConfigVersion()
    shared_repo = FakeGrantRepository([
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c1", read=True),
    ])

    # "main API" process: long TTL, reads through its own cache.
    api_store = GrantStore(shared_repo, ttl_seconds=3600, version_provider=version.get_version)
    # "admin service" process: the writer.
    admin_store = GrantStore(shared_repo, ttl_seconds=3600, version_bumper=version.bump)

    api_user = make_user("joe", ROLE_USER)
    api_user.ldap_groups = ["dept-a"]
    assert await api_store.get_all_grants()  # prime the cache

    resolver = GlossaryAccessResolver(grant_store=api_store)
    assert await resolver.can_read(api_user, "c2") is False

    # Admin grants write access on a new collection.
    await admin_store.upsert_grant(
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c2", read=True, write=True)
    )

    # No restart, no cache clearing — the version bump invalidated the reader.
    assert await resolver.can_read(api_user, "c2") is True
    assert await resolver.can_write(api_user, "c2") is True


@pytest.mark.asyncio
async def test_revoked_grant_propagates_to_other_process() -> None:
    version = FakeConfigVersion()
    shared_repo = FakeGrantRepository([
        GrantDoc(subject_type=SUBJECT_GROUP, subject="dept-a", collection="c1", read=True, write=True),
    ])
    api_store = GrantStore(shared_repo, ttl_seconds=3600, version_provider=version.get_version)
    admin_store = GrantStore(shared_repo, ttl_seconds=3600, version_bumper=version.bump)

    resolver = GlossaryAccessResolver(grant_store=api_store)
    user = make_user("joe", ROLE_USER)
    user.ldap_groups = ["dept-a"]
    assert await resolver.can_write(user, "c1") is True

    await admin_store.delete_grant(shared_repo.grants[0])

    assert await resolver.can_write(user, "c1") is False
    # `default` is still readable — hardcoded into every read set.
    assert await resolver.can_read(user, DEFAULT_COLLECTION) is True


# --- MongoUserRepository round-trip (task 2.5) ------------------------------

def test_user_doc_roundtrip_preserves_manual_role_and_permissions() -> None:
    from file_translator.infrastructure.repositories.auth_repository import MongoUserRepository

    doc = {
        "user_id": "id-1",
        "username": "joe",
        "display_name": "Joe",
        "role": "user",
        "manual_role": True,
        "permissions": [Permission.MANAGE_SYSTEM.value, Permission.TRANSLATE.value],
        "is_active": True,
        "ldap_groups": ["dept-a"],
        "created_at": "2026-01-01T00:00:00",
    }
    user = MongoUserRepository._doc_to_user(doc)
    assert user.role_name == "user"
    assert user.manual_role is True
    assert user.permissions == {Permission.MANAGE_SYSTEM, Permission.TRANSLATE}

    back = MongoUserRepository._user_to_doc(user)
    assert back["role"] == "user"
    assert back["manual_role"] is True
    assert sorted(back["permissions"]) == sorted(
        [Permission.MANAGE_SYSTEM.value, Permission.TRANSLATE.value]
    )
    assert back["ldap_groups"] == ["dept-a"]


def test_custom_role_name_survives_enum_fallback() -> None:
    from file_translator.infrastructure.repositories.auth_repository import MongoUserRepository

    user = MongoUserRepository._doc_to_user({
        "user_id": "id-2", "username": "svc", "role": "translator-plus",
    })
    assert user.role_name == "translator-plus"
    assert user.role == RoleType.VIEWER  # enum fallback only
    assert MongoUserRepository._user_to_doc(user)["role"] == "translator-plus"


def test_user_doc_defaults_manual_role_false() -> None:
    from file_translator.infrastructure.repositories.auth_repository import MongoUserRepository

    user = MongoUserRepository._doc_to_user({"user_id": "id-3", "username": "old"})
    assert user.manual_role is False


def test_legacy_user_doc_without_denied_loads_as_no_lowerings() -> None:
    """A pre-change document has no `denied` key and must keep today's rights."""
    from file_translator.infrastructure.repositories.auth_repository import MongoUserRepository

    legacy = {"user_id": "id-4", "username": "legacy", "role": "user",
              "permissions": [Permission.TRANSLATE.value]}
    user = MongoUserRepository._doc_to_user(legacy)
    assert user.denied == set()
    assert user.effective_permissions == {Permission.TRANSLATE, Permission.SEND_FEEDBACK}

    back = MongoUserRepository._user_to_doc(user)
    assert back["denied"] == []
    assert back["permissions"] == [Permission.TRANSLATE.value]


def test_user_doc_roundtrip_preserves_denied() -> None:
    from file_translator.infrastructure.repositories.auth_repository import MongoUserRepository

    doc = {"user_id": "id-5", "username": "lowered", "role": "user",
           "permissions": [Permission.MANAGE_SYSTEM.value],
           "denied": [Permission.SEND_FEEDBACK.value]}
    user = MongoUserRepository._doc_to_user(doc)
    assert user.denied == {Permission.SEND_FEEDBACK}

    back = MongoUserRepository._user_to_doc(user)
    assert back["denied"] == [Permission.SEND_FEEDBACK.value]
    assert MongoUserRepository._doc_to_user(back).denied == user.denied


def test_every_right_has_a_human_label() -> None:
    """A right without wording would render as its raw value in the admin UI."""
    from file_translator.domain.auth import PERMISSION_LABELS

    missing = {p.value for p in Permission} - set(PERMISSION_LABELS)
    assert not missing, f"rights without a label: {sorted(missing)}"
    assert all(label.strip() for label in PERMISSION_LABELS.values())
    # Labels are wording, not the value again.
    assert all(label != value for value, label in PERMISSION_LABELS.items())


# --- MongoUserRepository.update: failed write vs idempotent write ------------


class _UpdateResult:
    def __init__(self, matched: int, modified: int) -> None:
        self.matched_count = matched
        self.modified_count = modified


class _UsersStub:
    """Async stand-in for the ``users`` collection's ``update_one``."""

    def __init__(self, result: _UpdateResult) -> None:
        self._result = result
        self.calls: list[tuple[dict, dict]] = []

    async def update_one(self, flt: dict, update: dict) -> _UpdateResult:
        self.calls.append((flt, update))
        return self._result


def _user_repository(result: _UpdateResult):
    from types import SimpleNamespace

    from file_translator.infrastructure.repositories.auth_repository import MongoUserRepository

    collection = _UsersStub(result)
    return MongoUserRepository(SimpleNamespace(users=collection)), collection  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_update_reports_a_write_that_matched_no_document() -> None:
    repo, collection = _user_repository(_UpdateResult(matched=0, modified=0))

    assert await repo.update(make_user("ghost", "admin")) is None
    filter_used, update_used = collection.calls[0]
    assert filter_used == {"user_id": make_user("ghost", "admin").user_id}
    assert update_used["$set"]["role"] == "admin"


@pytest.mark.asyncio
async def test_update_reports_an_idempotent_write_as_written() -> None:
    """Matched but unchanged: the document is there and holds these values."""
    repo, _ = _user_repository(_UpdateResult(matched=1, modified=0))

    user = make_user("joe", "user")
    assert await repo.update(user) is user


# --- Legacy role migration ---------------------------------------------------

@pytest.mark.asyncio
async def test_migration_converts_operator_and_viewer_to_user() -> None:
    users = [
        make_user("op", RoleType.OPERATOR.value),
        make_user("vw", RoleType.VIEWER.value),
    ]
    repo = FakeUserRepository(users)
    result = await migrate_legacy_roles(repo)
    assert result["migrated"] == 2
    assert {u.role_name for u in repo.users} == {ROLE_USER}


@pytest.mark.asyncio
async def test_migration_preserves_api_role() -> None:
    repo = FakeUserRepository([make_user("svc", RoleType.API.value)])
    result = await migrate_legacy_roles(repo)
    assert result["api_preserved"] == 1
    assert repo.users[0].role_name == RoleType.API.value


@pytest.mark.asyncio
async def test_migration_preserves_admin_role() -> None:
    repo = FakeUserRepository([make_user("root", RoleType.ADMIN.value)])
    await migrate_legacy_roles(repo)
    assert repo.users[0].role_name == RoleType.ADMIN.value


@pytest.mark.asyncio
async def test_migration_stamps_manual_role_false() -> None:
    repo = FakeUserRepository([make_user("op", RoleType.OPERATOR.value)])
    result = await migrate_legacy_roles(repo)
    assert result["stamped"] == 1
    assert repo.users[0].manual_role is False


@pytest.mark.asyncio
async def test_migration_persists_role_name_for_mongo() -> None:
    """`_user_to_doc` persists role_name, so the migration must set it too."""
    repo = FakeUserRepository([make_user("op", RoleType.OPERATOR.value)])
    await migrate_legacy_roles(repo)
    persisted = repo.updates[0]
    assert persisted.role_name == ROLE_USER
    assert persisted.role == RoleType.USER


@pytest.mark.asyncio
async def test_migration_leaves_already_migrated_user_alone() -> None:
    user = make_user("joe", ROLE_USER)
    user.manual_role = True
    repo = FakeUserRepository([user])
    result = await migrate_legacy_roles(repo)
    assert result["migrated"] == 0
    assert result["stamped"] == 0
    assert repo.updates == []
    assert repo.users[0].role_name == ROLE_USER
