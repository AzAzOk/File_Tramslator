"""Tests for AD-group → collection view grants.

Covers:
- GroupGrantProvisioner: case-insensitive matching, verbatim storage, several
  matches, no match, not lowering an existing grant, and idempotence.
- provision_all_users: backfilling from the stored users' AD groups.
- The collection-id lister that maps glossary tables to collection ids.
- AuthService._handle_successful_login: provisioning on every login (new and
  existing users), and a failing provisioner never blocking a login.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret")

from file_translator.application.auth_service import AuthService
from file_translator.domain.auth import AuthToken, RoleType, User
from file_translator.infrastructure.auth.group_grants import (
    GroupGrantProvisioner,
    build_mysql_collection_lister,
    to_collection_id,
)
from file_translator.infrastructure.auth.role_config import (
    ACCESS_LEVEL_MODIFY,
    ACCESS_LEVEL_VIEW,
    DEFAULT_COLLECTION,
    GrantDoc,
    GrantStore,
    SUBJECT_GROUP,
)


# --- Fakes -------------------------------------------------------------------

class FakeGrantRepository:
    def __init__(self, grants: list[GrantDoc] | None = None) -> None:
        self.grants = list(grants or [])
        self.upserts: list[GrantDoc] = []

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
    def __init__(self, users: list[User] | None = None) -> None:
        self.users = list(users or [])

    async def get_by_username(self, username: str) -> User | None:
        return next((u for u in self.users if u.username == username), None)

    async def create(self, user: User) -> User:
        self.users.append(user)
        return user

    async def update(self, user: User) -> User:
        for i, u in enumerate(self.users):
            if u.user_id == user.user_id:
                self.users[i] = user
        return user

    async def list_all(self) -> list[User]:
        return list(self.users)


class FakeAuthProvider:
    async def create_token(self, user_id: str) -> AuthToken:
        return AuthToken(token="t", token_type="bearer", user_id=user_id)


class FakeProvisioner:
    def __init__(self, result: int = 0) -> None:
        self.calls: list[User] = []
        self._result = result

    async def provision(self, user: User) -> int:
        self.calls.append(user)
        return self._result


class FailingProvisioner:
    async def provision(self, user: User) -> int:
        raise RuntimeError("boom")


class FakeTableRepository:
    def __init__(self, tables: list[str]) -> None:
        self.tables = list(tables)
        self.patterns: list[str] = []

    async def list_tables(self, pattern: str = "glossary_%") -> list[str]:
        self.patterns.append(pattern)
        return list(self.tables)


def make_lister(collections: list[str]):
    async def _list() -> list[str]:
        return list(collections)

    return _list


def ad_user(groups: list[str] | None, user_id: str = "u1") -> User:
    return User(user_id=user_id, username="kulikov", ldap_groups=groups, is_active=True)


# --- Provisioner -------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_insensitive_match_stores_grants_verbatim() -> None:
    store = GrantStore(FakeGrantRepository())
    provisioner = GroupGrantProvisioner(make_lister(["dtd"]), store)

    created = await provisioner.provision(ad_user(["DTD"]))

    assert created == 1
    grants = await store.get_all_grants()
    assert len(grants) == 1
    grant = grants[0]
    assert grant.subject_type == SUBJECT_GROUP
    assert grant.subject == "DTD"
    assert grant.collection == "dtd"
    assert grant.level == ACCESS_LEVEL_VIEW


@pytest.mark.asyncio
async def test_every_matching_collection_is_covered() -> None:
    store = GrantStore(FakeGrantRepository())
    provisioner = GroupGrantProvisioner(make_lister(["dtd", "oup"]), store)

    created = await provisioner.provision(ad_user(["DTD", "OUP", "Finance"]))

    assert created == 2
    grants = await store.get_all_grants()
    assert {(g.subject, g.collection) for g in grants} == {("DTD", "dtd"), ("OUP", "oup")}
    assert all(g.level == ACCESS_LEVEL_VIEW for g in grants)


@pytest.mark.asyncio
async def test_no_match_yields_no_grant() -> None:
    store = GrantStore(FakeGrantRepository())
    provisioner = GroupGrantProvisioner(make_lister(["dtd"]), store)

    created = await provisioner.provision(ad_user(["Finance"]))

    assert created == 0
    assert await store.get_all_grants() == []


@pytest.mark.asyncio
async def test_no_groups_skips_listing() -> None:
    called = {"n": 0}

    async def lister() -> list[str]:
        called["n"] += 1
        return ["dtd"]

    store = GrantStore(FakeGrantRepository())
    provisioner = GroupGrantProvisioner(lister, store)

    assert await provisioner.provision(ad_user(None)) == 0
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_existing_grant_is_not_downgraded() -> None:
    repo = FakeGrantRepository([
        GrantDoc(subject_type=SUBJECT_GROUP, subject="DTD", collection="dtd", level=ACCESS_LEVEL_MODIFY),
    ])
    store = GrantStore(repo)
    provisioner = GroupGrantProvisioner(make_lister(["dtd"]), store)

    created = await provisioner.provision(ad_user(["DTD"]))

    assert created == 0
    assert repo.upserts == []
    grants = await store.get_all_grants()
    assert len(grants) == 1
    assert grants[0].level == ACCESS_LEVEL_MODIFY


@pytest.mark.asyncio
async def test_existing_level_zero_grant_is_left_alone() -> None:
    repo = FakeGrantRepository([
        GrantDoc(subject_type=SUBJECT_GROUP, subject="DTD", collection="dtd", level=0),
    ])
    store = GrantStore(repo)
    provisioner = GroupGrantProvisioner(make_lister(["dtd"]), store)

    assert await provisioner.provision(ad_user(["DTD"])) == 0
    assert repo.upserts == []


@pytest.mark.asyncio
async def test_provision_is_idempotent() -> None:
    repo = FakeGrantRepository()
    store = GrantStore(repo)
    provisioner = GroupGrantProvisioner(make_lister(["dtd"]), store)

    assert await provisioner.provision_groups(["DTD"]) == 1
    assert await provisioner.provision_groups(["DTD"]) == 0
    assert len(repo.upserts) == 1


# --- provision_all_users -----------------------------------------------------

@pytest.mark.asyncio
async def test_provision_all_users_backfills_stored_users() -> None:
    repo = FakeGrantRepository()
    store = GrantStore(repo)
    provisioner = GroupGrantProvisioner(make_lister(["dtd", "oup"]), store)
    users = FakeUserRepository([
        ad_user(["DTD"], user_id="u1"),
        ad_user(["oup", "Finance"], user_id="u2"),
    ])

    created = await provisioner.provision_all_users(users)

    assert created == 2
    grants = await store.get_all_grants()
    assert {(g.subject, g.collection) for g in grants} == {("DTD", "dtd"), ("oup", "oup")}


@pytest.mark.asyncio
async def test_provision_all_users_without_users_creates_nothing() -> None:
    store = GrantStore(FakeGrantRepository())
    provisioner = GroupGrantProvisioner(make_lister(["dtd"]), store)

    assert await provisioner.provision_all_users(FakeUserRepository()) == 0
    assert await store.get_all_grants() == []


def test_to_collection_id_mapping() -> None:
    assert to_collection_id("glossary") == DEFAULT_COLLECTION
    assert to_collection_id("glossary_dtd") == "dtd"
    assert to_collection_id("other") == "other"


@pytest.mark.asyncio
async def test_mysql_lister_maps_tables() -> None:
    repo = FakeTableRepository(["glossary", "glossary_dtd"])
    lister = build_mysql_collection_lister(repo)

    assert await lister() == [DEFAULT_COLLECTION, "dtd"]
    assert repo.patterns == ["glossary%"]


# --- AuthService integration -------------------------------------------------

def make_service(provisioner: Any, user_repo: FakeUserRepository | None = None) -> AuthService:
    return AuthService(
        auth_provider=FakeAuthProvider(),
        user_repository=user_repo or FakeUserRepository(),
        group_grant_provisioner=provisioner,
    )


@pytest.mark.asyncio
async def test_first_login_creates_user_and_provisions() -> None:
    repo = FakeUserRepository()
    provisioner = FakeProvisioner(result=1)
    service = make_service(provisioner, repo)

    token = await service._handle_successful_login(
        "kulikov", "Kulikov", RoleType.VIEWER, ["DTD", "Finance"],
    )

    assert token is not None
    assert len(provisioner.calls) == 1
    created = provisioner.calls[0]
    assert created.username == "kulikov"
    assert created.ldap_groups == ["DTD", "Finance"]
    assert [u.username for u in repo.users] == ["kulikov"]


@pytest.mark.asyncio
async def test_existing_user_is_provisioned_on_login() -> None:
    repo = FakeUserRepository([
        User(
            user_id="id-1",
            username="kulikov",
            display_name="Kulikov",
            role=RoleType.VIEWER,
            role_name="viewer",
            ldap_groups=["DTD"],
            is_active=True,
        ),
    ])
    provisioner = FakeProvisioner()
    service = make_service(provisioner, repo)

    token = await service._handle_successful_login(
        "kulikov", "Kulikov", RoleType.VIEWER, ["DTD"],
    )

    assert token is not None
    assert len(provisioner.calls) == 1
    assert provisioner.calls[0].user_id == "id-1"


@pytest.mark.asyncio
async def test_provisioner_failure_does_not_block_login() -> None:
    repo = FakeUserRepository()
    service = make_service(FailingProvisioner(), repo)

    token = await service._handle_successful_login(
        "kulikov", "Kulikov", RoleType.VIEWER, ["DTD"],
    )

    assert token is not None
    assert [u.username for u in repo.users] == ["kulikov"]


@pytest.mark.asyncio
async def test_login_without_provisioner_succeeds() -> None:
    repo = FakeUserRepository()
    service = AuthService(auth_provider=FakeAuthProvider(), user_repository=repo)

    token = await service._handle_successful_login(
        "kulikov", "Kulikov", RoleType.VIEWER, ["DTD"],
    )

    assert token is not None
    assert [u.username for u in repo.users] == ["kulikov"]
