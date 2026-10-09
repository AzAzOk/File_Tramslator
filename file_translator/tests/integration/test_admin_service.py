"""Endpoint tests for the standalone admin service (tasks 5.1-5.7).

The whole admin container is wired to in-memory fakes, but the password store
uses the real bcrypt implementation so the "new password is required next
login" requirement is genuinely exercised.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from admin_service.app import create_app  # noqa: E402
from admin_service.config import AdminConfig  # noqa: E402
from admin_service.deps import AdminContainer, AdminPasswordStore, SessionTokens  # noqa: E402
from file_translator.domain.auth import Permission, RoleType, User  # noqa: E402
from file_translator.infrastructure.auth.role_config import (  # noqa: E402
    DEFAULT_ROLES,
    GrantDoc,
    GrantStore,
    RoleConfigStore,
    RoleDoc,
)

ADMIN_USER = "root"
ADMIN_PASSWORD = "s3cret-admin"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeRoleRepository:
    def __init__(self, roles: list[RoleDoc] | None = None) -> None:
        self.roles: dict[str, RoleDoc] = {r.name: r for r in (roles or [])}
        self.indexes_ready = False

    async def ensure_indexes(self) -> None:
        self.indexes_ready = True

    async def find_all(self) -> list[RoleDoc]:
        return list(self.roles.values())

    async def find_by_name(self, name: str) -> RoleDoc | None:
        return self.roles.get(name)

    async def create(self, role: RoleDoc) -> RoleDoc:
        self.roles[role.name] = role
        return role

    async def update(self, name: str, fields: dict[str, Any]) -> bool:
        role = self.roles.get(name)
        if role is None:
            return False
        for key, value in fields.items():
            setattr(role, key, value)
        return True

    async def delete(self, name: str) -> bool:
        return self.roles.pop(name, None) is not None

    async def count(self) -> int:
        return len(self.roles)


class FakeGrantRepository:
    def __init__(self, grants: list[GrantDoc] | None = None) -> None:
        self.grants: dict[tuple[str, str, str], GrantDoc] = {
            (g.subject_type, g.subject, g.collection): g for g in (grants or [])
        }
        self.indexes_ready = False

    async def ensure_indexes(self) -> None:
        self.indexes_ready = True

    async def find_all(self) -> list[GrantDoc]:
        return list(self.grants.values())

    async def find_by_subject(self, subject_type: str, subject: str) -> list[GrantDoc]:
        return [g for g in self.grants.values() if g.subject_type == subject_type and g.subject == subject]

    async def find_by_subject_types(self, subjects_by_type: dict[str, list[str]]) -> list[GrantDoc]:
        out: list[GrantDoc] = []
        for subject_type, subjects in subjects_by_type.items():
            for subject in subjects:
                out.extend(await self.find_by_subject(subject_type, subject))
        return out

    async def upsert(self, grant: GrantDoc) -> GrantDoc:
        self.grants[(grant.subject_type, grant.subject, grant.collection)] = grant
        return grant

    async def delete(self, grant: GrantDoc) -> bool:
        return self.grants.pop((grant.subject_type, grant.subject, grant.collection), None) is not None

    async def delete_many(
        self,
        subject_type: str | None = None,
        subject: str | None = None,
        collection: str | None = None,
    ) -> int:
        victims = [
            key
            for key in self.grants
            if (subject_type is None or key[0] == subject_type)
            and (subject is None or key[1] == subject)
            and (collection is None or key[2] == collection)
        ]
        for key in victims:
            del self.grants[key]
        return len(victims)

    async def count(self) -> int:
        return len(self.grants)


class FakeUserRepository:
    """Stands in for the Mongo ``users`` collection.

    Reads and writes hand out copies, like a database does. A live-object fake
    would hide exactly the failure this suite has to catch: a mutation made in
    memory is visible to the next read even when the write never happened.
    """

    def __init__(self, users: list[User] | None = None) -> None:
        self.users: dict[str, User] = {u.user_id: u for u in (users or [])}

    async def get_by_id(self, user_id: str) -> User | None:
        stored = self.users.get(user_id)
        return replace(stored) if stored else None

    async def get_by_username(self, username: str) -> User | None:
        return next((u for u in self.users.values() if u.username == username), None)

    async def create(self, user: User) -> User:
        self.users[user.user_id] = replace(user)
        return user

    async def update(self, user: User) -> User | None:
        if user.user_id not in self.users:
            return None
        self.users[user.user_id] = replace(user)
        return user

    async def delete(self, user_id: str) -> bool:
        return self.users.pop(user_id, None) is not None

    async def list_all(self) -> list[User]:
        return list(self.users.values())


class FakeEventRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def ensure_indexes(self) -> None:
        return None

    async def log(
        self,
        actor: str,
        action: str,
        target_type: str = "",
        target: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "actor": actor,
            "action": action,
            "target_type": target_type,
            "target": target,
            "details": details or {},
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.events.append(event)
        return event

    async def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(reversed(self.events))[:limit]

    async def count(self) -> int:
        return len(self.events)

    def actions(self) -> set[str]:
        return {e["action"] for e in self.events}


class FakeSettingsCollection:
    """Minimal stand-in for the Mongo ``admin_settings`` collection."""

    def __init__(self) -> None:
        self.docs: dict[Any, dict[str, Any]] = {}

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return self.docs.get(query.get("_id"))

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False):
        key = query.get("_id")
        self.docs.setdefault(key, {"_id": key}).update(update.get("$set", {}))
        return None


class FakeDb:
    def __init__(self) -> None:
        self.admin_settings = FakeSettingsCollection()


class FakeCollectionSource:
    def __init__(self, collections: list[str]) -> None:
        self.collections = collections
        self.fail = False

    async def list_collections(self) -> list[str]:
        if self.fail:
            raise RuntimeError("MySQL is down")
        return list(self.collections)


class FakeConfigVersion:
    """In-memory stand-in for the Redis propagation key."""

    def __init__(self) -> None:
        self.value = 0
        self.bumps = 0

    async def get_version(self) -> int:
        return self.value

    async def bump(self) -> int:
        self.bumps += 1
        self.value += 1
        return self.value


def make_user(
    user_id: str,
    username: str,
    role: str = "user",
    manual: bool = False,
    groups: list[str] | None = None,
    active: bool = True,
) -> User:
    user = User(user_id=user_id, username=username, display_name=username.title())
    user.role_name = role
    user.manual_role = manual
    user.ldap_groups = groups
    user.is_active = active
    try:
        user.role = RoleType(role)
    except ValueError:
        user.role = RoleType.VIEWER
    return user


def builtin_roles() -> list[RoleDoc]:
    return [RoleDoc.from_dict(dict(doc)) for doc in DEFAULT_ROLES.values()]  # type: ignore[misc]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def version() -> FakeConfigVersion:
    return FakeConfigVersion()


@pytest.fixture
def role_repository() -> FakeRoleRepository:
    return FakeRoleRepository(builtin_roles())


@pytest.fixture
def grant_repository() -> FakeGrantRepository:
    return FakeGrantRepository(
        [
            GrantDoc(subject_type="role", subject="admin", collection="default", read=True, write=True),
            GrantDoc(subject_type="group", subject="DTD", collection="dtd", read=True, write=True),
        ]
    )


@pytest.fixture
def user_repository() -> FakeUserRepository:
    return FakeUserRepository(
        [
            make_user("u-1", "ivanov", role="user", manual=False, groups=["DTD"]),
            make_user("u-2", "petrova", role="user", manual=True, groups=["HR"]),
            make_user("u-3", "service", role="api"),
        ]
    )


@pytest.fixture
def event_repository() -> FakeEventRepository:
    return FakeEventRepository()


@pytest.fixture
def collection_source() -> FakeCollectionSource:
    return FakeCollectionSource(["default", "dtd", "oup"])


@pytest.fixture
def container(
    version, role_repository, grant_repository, user_repository, event_repository, collection_source
) -> AdminContainer:
    role_store = RoleConfigStore(
        role_repository,
        version_provider=version.get_version,
        version_bumper=version.bump,
    )
    grant_store = GrantStore(
        grant_repository,
        version_provider=version.get_version,
        version_bumper=version.bump,
    )
    config = AdminConfig(
        username=ADMIN_USER,
        password=ADMIN_PASSWORD,
        mongo_uri="mongodb://unused",
        mongo_db="unused",
        redis_host="unused",
        redis_port=6379,
        redis_password="",
        mysql_host="unused",
        mysql_port=3306,
        mysql_user="unused",
        mysql_password="unused",
        mysql_db="glossary",
        host="127.0.0.1",
        port=8011,
        log_level="INFO",
    )
    return AdminContainer(
        config=config,
        role_store=role_store,
        grant_store=grant_store,
        event_repo=event_repository,
        user_repo=user_repository,
        collection_source=collection_source,
        config_version=version,
        passwords=AdminPasswordStore(FakeDb(), ADMIN_PASSWORD),
        sessions=SessionTokens("test-secret", ttl_hours=1),
    )


@pytest.fixture
def client(container: AdminContainer):
    app = create_app(container=container)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(client: TestClient):
    response = client.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


# --------------------------------------------------------------------------
# 5.1 / 5.6 — login and session cookie
# --------------------------------------------------------------------------
def test_login_rejects_wrong_password(client: TestClient):
    response = client.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": "wrong"}
    )
    assert response.status_code == 401


def test_login_rejects_wrong_username(client: TestClient):
    response = client.post("/api/auth/login", json={"username": "nope", "password": ADMIN_PASSWORD})
    assert response.status_code == 401


def test_login_opens_session_and_sets_cookie(client: TestClient):
    response = client.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert "admin_session" in response.cookies or client.cookies.get("admin_session")
    assert client.get("/api/auth/session").json()["authenticated"] is True


def test_endpoints_require_session(client: TestClient):
    assert client.get("/api/users").status_code == 401
    assert client.get("/api/roles").status_code == 401
    assert client.get("/api/access/matrix").status_code == 401
    assert client.get("/api/settings").status_code == 401


def test_logout_clears_session(auth_client: TestClient):
    assert auth_client.post("/api/auth/logout").status_code == 200
    assert auth_client.get("/api/users").status_code == 401


# --------------------------------------------------------------------------
# 5.2 — health check
# --------------------------------------------------------------------------
def test_health_is_public_and_reports_counts_and_collections(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mongo"]["users"] == 3
    assert body["mongo"]["roles"] == len(DEFAULT_ROLES)
    assert body["mongo"]["grants"] == 2
    assert body["collections"] == ["default", "dtd", "oup"]


def test_health_degrades_when_mysql_unavailable(auth_client: TestClient, collection_source):
    collection_source.fail = True
    body = auth_client.get("/api/health").json()
    assert body["collections"] == []
    assert body["status"] == "ok"


# --------------------------------------------------------------------------
# 5.3 — user management
# --------------------------------------------------------------------------
def test_list_users_exposes_roles_and_manual_flag(auth_client: TestClient):
    users = {u["user_id"]: u for u in auth_client.get("/api/users").json()}
    assert users["u-1"]["role"] == "user"
    assert users["u-1"]["manual_role"] is False
    assert users["u-2"]["manual_role"] is True
    assert users["u-2"]["ldap_groups"] == ["HR"]
    assert users["u-1"]["role_permissions"]  # role pool is resolved


def test_list_users_reports_effective_state_and_denials(
    auth_client: TestClient, user_repository
):
    """The dialog renders `effective`; it must be the role pool minus denials."""
    user_repository.users["u-1"].denied = {Permission.SEND_FEEDBACK}
    user_repository.users["u-1"].permissions = {Permission.MANAGE_SYSTEM}

    users = {u["user_id"]: u for u in auth_client.get("/api/users").json()}
    lowered = users["u-1"]
    plain = users["u-2"]

    assert lowered["denied"] == [Permission.SEND_FEEDBACK.value]
    assert Permission.SEND_FEEDBACK.value not in lowered["effective"]
    assert Permission.MANAGE_SYSTEM.value in lowered["effective"]
    # The formula the client shows, recomputed from what the endpoint reports.
    assert set(lowered["effective"]) == (
        set(lowered["role_permissions"]) - set(lowered["denied"])
    ) | set(lowered["permissions"])

    # A user with no deviations reports exactly their role's pool.
    assert plain["denied"] == []
    assert set(plain["effective"]) == set(plain["role_permissions"])


def test_assign_role_sets_manual_flag(auth_client):
    response = auth_client.post("/api/users/u-1/role", json={"role": "admin"})
    assert response.status_code == 200
    assert response.json()["manual_role"] is True
    assert response.json()["role"] == "admin"


def test_assign_role_persists_manual_flag(auth_client: TestClient, user_repository):
    auth_client.post("/api/users/u-1/role", json={"role": "admin"})
    stored = user_repository.get_by_id  # repository is the source of truth
    assert stored is not None
    assert user_repository.users["u-1"].manual_role is True
    assert user_repository.users["u-1"].role_name == "admin"


def test_assign_unknown_role_is_rejected(auth_client: TestClient):
    assert auth_client.post("/api/users/u-1/role", json={"role": "ghost"}).status_code == 404


def test_assign_api_role_is_rejected(auth_client: TestClient):
    response = auth_client.post("/api/users/u-1/role", json={"role": "api"})
    assert response.status_code == 400


def test_assign_role_for_missing_user_is_404(auth_client: TestClient):
    assert auth_client.post("/api/users/nope/role", json={"role": "user"}).status_code == 404


def role_pool(name: str = "user") -> list[str]:
    """Permission values of a built-in role, as the endpoint resolves them."""
    return sorted(DEFAULT_ROLES[name]["permissions"])


def test_set_permission_overrides(auth_client: TestClient, user_repository):
    response = auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": ["system:manage"]}
    )
    assert response.status_code == 200
    assert response.json()["user"]["permissions"] == ["system:manage"]
    assert user_repository.users["u-1"].permissions


def test_saving_the_role_pool_writes_no_personal_sets(
    auth_client: TestClient, user_repository
):
    """The dialog resubmits what it displayed — that must be a no-op on disk."""
    response = auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": role_pool()}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["raised"] == 0
    assert body["lowered"] == 0

    stored = user_repository.users["u-1"]
    assert stored.permissions == set()
    assert stored.denied == set()


def test_raising_one_right_stores_only_that_right(
    auth_client: TestClient, user_repository
):
    response = auth_client.post(
        "/api/users/u-1/permissions",
        json={"permissions": role_pool() + ["system:manage"]},
    )
    body = response.json()
    assert body["raised"] == 1
    assert body["lowered"] == 0
    assert body["user"]["effective"] == sorted(set(role_pool()) | {"system:manage"})

    stored = user_repository.users["u-1"]
    assert stored.permissions == {Permission.MANAGE_SYSTEM}
    assert stored.denied == set()


def test_lowering_one_right_stores_only_that_denial(
    auth_client: TestClient, user_repository
):
    desired = [p for p in role_pool() if p != Permission.SEND_FEEDBACK.value]
    response = auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": desired}
    )
    body = response.json()
    assert body["raised"] == 0
    assert body["lowered"] == 1
    assert Permission.SEND_FEEDBACK.value not in body["user"]["effective"]

    stored = user_repository.users["u-1"]
    assert stored.denied == {Permission.SEND_FEEDBACK}
    assert stored.permissions == set()
    # Other members of the same role are untouched.
    assert user_repository.users["u-2"].denied == set()


def test_set_unknown_permission_is_rejected(auth_client: TestClient, event_repository):
    response = auth_client.post("/api/users/u-1/permissions", json={"permissions": ["nope:all"]})
    assert response.status_code == 400
    assert "user.permissions_set" not in event_repository.actions()


def test_permissions_audit_names_both_counts(auth_client: TestClient, event_repository):
    desired = [p for p in role_pool() if p != Permission.SEND_FEEDBACK.value] + ["system:manage"]
    auth_client.post("/api/users/u-1/permissions", json={"permissions": desired})

    audit = [e for e in event_repository.events if e["action"] == "user.permissions_set"]
    assert len(audit) == 1  # one record for a save, not one per changed right
    details = audit[0]["details"]
    assert details["raised_count"] == 1
    assert details["lowered_count"] == 1
    assert details["raised"] == ["system:manage"]
    assert details["lowered"] == [Permission.SEND_FEEDBACK.value]


def test_reset_rights_clears_every_deviation(auth_client: TestClient, user_repository):
    auth_client.post(
        "/api/users/u-1/permissions",
        json={"permissions": role_pool() + ["system:manage"]},
    )
    auth_client.post(
        "/api/users/u-1/permissions",
        json={"permissions": [p for p in role_pool() if p != Permission.SEND_FEEDBACK.value]
              + ["system:manage"]},
    )
    stored = user_repository.users["u-1"]
    assert stored.permissions and stored.denied

    response = auth_client.post("/api/users/u-1/reset-rights")
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["permissions"] == []
    assert body["user"]["denied"] == []
    assert set(body["user"]["effective"]) == set(body["user"]["role_permissions"])

    stored = user_repository.users["u-1"]
    assert stored.permissions == set()
    assert stored.denied == set()


# --- Role matching on save (change user-rights-collection-levels, task 3.x) ---


def test_full_match_save_offers_the_matching_role(auth_client: TestClient):
    """Rights lowered to exactly another role's pool → the offer names it."""
    response = auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": role_pool("viewer")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["raised"] == 0
    assert body["lowered"] == 1  # translate (the only right `user` has over `viewer`)
    assert body["suggestions"] == ["viewer"]


def test_save_without_deviations_never_offers_a_role(auth_client: TestClient):
    """`user` and `operator` share a pool — an identical pool alone is not an offer."""
    body = auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": role_pool()}
    ).json()
    assert body["raised"] == 0
    assert body["lowered"] == 0
    assert body["suggestions"] == []


def test_permissions_match_without_levels_is_not_offered(
    auth_client: TestClient, grant_repository
):
    """The role's pool matches, but accepting would move this person's level."""
    auth_client.put(
        "/api/access/grant",
        json={
            "subject_type": "user",
            "subject": "u-1",
            "collection": "oup",
            "read": True,
            "write": True,
        },
    )
    body = auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": role_pool("viewer")}
    ).json()
    assert body["lowered"] > 0  # rights do match the viewer pool
    assert body["suggestions"] == []  # ...but the personal level would drop


# --- Per-collection access view (change user-rights-collection-levels, task 8.1) ---


def _grant(auth_client: TestClient, **grant) -> None:
    response = auth_client.put("/api/access/grant", json=grant)
    assert response.status_code == 200, response.text


def test_access_view_names_role_group_personal_and_default(auth_client: TestClient):
    """Every collection lists its level and the document kind that grants it."""
    _grant(auth_client, subject_type="role", subject="user", collection="oup", level=2)
    _grant(auth_client, subject_type="user", subject="u-1", collection="dtd", level=1)

    body = auth_client.get("/api/users/u-1/access").json()
    assert body["user_id"] == "u-1"
    assert body["unrestricted"] is False
    assert [c["collection"] for c in body["collections"]] == ["default", "dtd", "oup"]

    by_name = {c["collection"]: c for c in body["collections"]}
    assert by_name["oup"] == {"collection": "oup", "level": 2, "source": "role:user"}
    # The personal grant wins over the seeded group:DTD@dtd=3 and owns the label.
    assert by_name["dtd"] == {"collection": "dtd", "level": 1, "source": "personal"}
    # Nobody grants `default` to role `user` — it is the floor, with no grantor.
    assert by_name["default"] == {"collection": "default", "level": 1, "source": "default"}


def test_access_view_labels_a_group_grant(auth_client: TestClient):
    _grant(auth_client, subject_type="group", subject="HR", collection="oup", level=3)

    body = auth_client.get("/api/users/u-2/access").json()
    by_name = {c["collection"]: c for c in body["collections"]}
    assert by_name["oup"] == {"collection": "oup", "level": 3, "source": "group:HR"}
    assert by_name["dtd"]["source"] == "none"  # HR is not in the DTD group


def test_access_view_reports_unrestricted_admin(auth_client: TestClient):
    """A plain `unrestricted: true` — an empty list would read as no access."""
    assert auth_client.post("/api/users/u-3/role", json={"role": "admin"}).status_code == 200

    body = auth_client.get("/api/users/u-3/access").json()
    assert body == {"user_id": "u-3", "unrestricted": True, "collections": []}


def test_access_view_404s_for_unknown_user(auth_client: TestClient):
    assert auth_client.get("/api/users/ghost/access").status_code == 404


def test_saved_personal_level_reloads_as_personal_and_leaves_role_peers(
    auth_client: TestClient,
):
    """A saved level is labelled `personal` on reload and touches one user."""
    _grant(auth_client, subject_type="role", subject="user", collection="oup", level=2)
    _grant(auth_client, subject_type="user", subject="u-1", collection="oup", level=3)

    saved = auth_client.get("/api/users/u-1/access").json()
    oup = next(c for c in saved["collections"] if c["collection"] == "oup")
    assert oup == {"collection": "oup", "level": 3, "source": "personal"}

    # u-2 shares the role: the personal grant of u-1 must not have moved them.
    peer = auth_client.get("/api/users/u-2/access").json()
    peer_oup = next(c for c in peer["collections"] if c["collection"] == "oup")
    assert peer_oup == {"collection": "oup", "level": 2, "source": "role:user"}


def test_accepting_the_offer_keeps_access_unchanged(
    auth_client: TestClient, user_repository
):
    offer = auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": role_pool("viewer")}
    ).json()
    assert offer["suggestions"] == ["viewer"]
    before = set(offer["user"]["effective"])

    response = auth_client.post(
        "/api/users/u-1/role", json={"role": "viewer", "clear_deviations": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "viewer"
    assert set(body["effective"]) == before  # the promise: access does not move
    assert body["permissions"] == []
    assert body["denied"] == []

    stored = user_repository.users["u-1"]
    assert stored.permissions == set()
    assert stored.denied == set()
    assert stored.manual_role is True


def test_accepting_the_offer_removes_personal_levels(
    auth_client: TestClient, grant_repository, event_repository
):
    """Half of "nothing changes" is the personal collection levels."""
    auth_client.put(
        "/api/access/grant",
        json={
            "subject_type": "user",
            "subject": "u-1",
            "collection": "oup",
            "read": True,
            "write": False,
        },
    )
    auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": role_pool("viewer")}
    )
    response = auth_client.post(
        "/api/users/u-1/role", json={"role": "viewer", "clear_deviations": True}
    )
    assert response.status_code == 200
    assert ("user", "u-1", "oup") not in grant_repository.grants

    assigned = [e for e in event_repository.events if e["action"] == "user.role_assigned"]
    assert assigned[-1]["details"]["clear_deviations"] is True
    assert assigned[-1]["details"]["cleared_grants"] == 1
    assert assigned[-1]["details"]["cleared_lowered"] == 1


def test_role_change_without_the_offer_keeps_deviations(
    auth_client: TestClient, user_repository
):
    """A plain role assignment is not the offer — it leaves deviations alone."""
    auth_client.post(
        "/api/users/u-1/permissions",
        json={"permissions": role_pool() + ["system:manage"]},
    )
    response = auth_client.post("/api/users/u-1/role", json={"role": "viewer"})
    assert response.status_code == 200

    stored = user_repository.users["u-1"]
    assert stored.role_name == "viewer"
    assert stored.permissions == {Permission.MANAGE_SYSTEM}


def test_reset_to_ad_role_keeps_deviations(auth_client: TestClient, user_repository):
    """Handing the role back to AD is not «Вернуть к роли» — deviations stay."""
    auth_client.post(
        "/api/users/u-2/permissions",
        json={"permissions": role_pool() + ["system:manage"]},
    )
    response = auth_client.post("/api/users/u-2/reset-role")
    assert response.status_code == 200
    assert response.json()["role"] == "user"

    stored = user_repository.users["u-2"]
    assert stored.role_name == "user"
    assert stored.manual_role is False
    assert stored.permissions == {Permission.MANAGE_SYSTEM}


def test_multiple_matching_roles_are_reported_together(
    auth_client: TestClient, role_repository
):
    """Two roles with the same pool are both named; no silent pick."""
    created = auth_client.post(
        "/api/roles", json={"name": "reviewer", "permissions": role_pool("viewer")}
    )
    assert created.status_code == 201, created.text

    body = auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": role_pool("viewer")}
    ).json()
    assert body["suggestions"] == ["reviewer", "viewer"]


def test_activate_and_deactivate(auth_client: TestClient, user_repository):
    response = auth_client.post("/api/users/u-1/active", json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert user_repository.users["u-1"].is_active is False
    auth_client.post("/api/users/u-1/active", json={"is_active": True})
    assert user_repository.users["u-1"].is_active is True


def test_reset_role_to_ldap_clears_manual_flag(auth_client: TestClient, user_repository, monkeypatch):
    monkeypatch.setenv("LDAP_GROUP_ADMIN", "ADMINS")
    user_repository.users["u-2"].ldap_groups = ["ADMINS"]
    response = auth_client.post("/api/users/u-2/reset-role")
    assert response.status_code == 200
    assert response.json()["manual_role"] is False
    assert response.json()["role"] == "admin"


def test_reset_role_without_admin_group_maps_to_user(auth_client: TestClient, monkeypatch):
    monkeypatch.setenv("LDAP_GROUP_ADMIN", "ADMINS")
    response = auth_client.post("/api/users/u-2/reset-role")
    assert response.json()["role"] == "user"


def test_role_assignment_answers_with_the_stored_role(auth_client: TestClient, user_repository):
    """The answer is the state that was stored, not the object handed to update()."""
    response = auth_client.post("/api/users/u-1/role", json={"role": "admin"})
    assert response.status_code == 200
    assert response.json()["role"] == user_repository.users["u-1"].role_name


def test_role_assignment_that_did_not_persist_is_not_reported_as_success(
    auth_client: TestClient, user_repository, monkeypatch
):
    async def swallow(user):  # the write goes nowhere
        return None

    monkeypatch.setattr(user_repository, "update", swallow)
    response = auth_client.post("/api/users/u-1/role", json={"role": "admin"})

    assert response.status_code == 409
    assert user_repository.users["u-1"].role_name == "user"
    assert user_repository.users["u-1"].manual_role is False


def test_role_reset_that_did_not_persist_is_not_reported_as_success(
    auth_client: TestClient, user_repository, monkeypatch
):
    monkeypatch.setenv("LDAP_GROUP_ADMIN", "ADMINS")
    user_repository.users["u-2"].ldap_groups = ["ADMINS"]
    user_repository.users["u-2"].manual_role = True

    async def swallow(user):
        return None

    monkeypatch.setattr(user_repository, "update", swallow)
    response = auth_client.post("/api/users/u-2/reset-role")

    assert response.status_code == 409
    assert user_repository.users["u-2"].manual_role is True


def test_payload_reports_the_role_ad_would_assign(
    auth_client: TestClient, user_repository, monkeypatch
):
    """The reset confirmation names the consequence, so it needs it up front."""
    monkeypatch.setenv("LDAP_GROUP_ADMIN", "ADMINS")
    user_repository.users["u-2"].ldap_groups = ["ADMINS"]

    listed = {u["user_id"]: u for u in auth_client.get("/api/users").json()}
    assert listed["u-2"]["ldap_role"] == "admin"   # in the AD admin group
    assert listed["u-1"]["ldap_role"] == "user"    # not in it


# --------------------------------------------------------------------------
# 5.4 — role management
# --------------------------------------------------------------------------
def test_permission_labels_cover_the_whole_pool(auth_client: TestClient):
    """Every right the pool offers can be shown as words, not as a raw value."""
    pool = set(auth_client.get("/api/roles/permissions-pool").json())
    labels = auth_client.get("/api/roles/permission-labels").json()

    assert pool <= set(labels)
    assert all(labels[value] and labels[value] != value for value in pool)


def test_permission_labels_require_a_session(client: TestClient):
    assert client.get("/api/roles/permission-labels").status_code == 401


def test_list_roles_hides_api_role(auth_client: TestClient):
    names = [r["name"] for r in auth_client.get("/api/roles").json()]
    assert "admin" in names and "user" in names
    assert "api" not in names


def test_list_roles_reports_member_counts(auth_client: TestClient):
    roles = {r["name"]: r for r in auth_client.get("/api/roles").json()}
    assert roles["user"]["member_count"] == 2
    assert roles["admin"]["member_count"] == 0


def test_create_custom_role(auth_client: TestClient, role_repository):
    response = auth_client.post(
        "/api/roles",
        json={
            "name": "Translator",
            "description": "Переводчик",
            "permissions": ["feedback:send", "translate"],
            "grants": ["dtd"],
        },
    )
    assert response.status_code == 201
    assert response.json()["builtin"] is False
    assert response.json()["permissions"] == ["feedback:send", "translate"]
    assert "Translator" in role_repository.roles


def test_create_duplicate_role_conflicts(auth_client: TestClient):
    assert auth_client.post("/api/roles", json={"name": "admin"}).status_code == 409


def test_create_role_with_unknown_permission_rejected(auth_client: TestClient):
    response = auth_client.post("/api/roles", json={"name": "Bad", "permissions": ["nope"]})
    assert response.status_code == 400


def test_update_custom_role(auth_client: TestClient, role_repository):
    auth_client.post("/api/roles", json={"name": "Translator", "permissions": ["translate"]})
    response = auth_client.patch(
        "/api/roles/Translator", json={"permissions": ["translate", "feedback:send"]}
    )
    assert response.status_code == 200
    assert response.json()["permissions"] == ["feedback:send", "translate"]
    assert role_repository.roles["Translator"].permissions == ["feedback:send", "translate"]


def test_update_role_drops_stale_stored_permissions(
    auth_client: TestClient, role_repository
):
    """A role written by an older build carries keys that no longer exist.

    Saving it (here: just a description edit, no permission payload) must drop
    the stale keys from storage rather than 400 or keep them forever.
    """
    role_repository.roles["Legacy"] = RoleDoc(
        name="Legacy",
        description="old",
        builtin=False,
        protected=False,
        permissions=["translate", "jobs:view", "users:manage"],
        grants=[],
    )
    response = auth_client.patch("/api/roles/Legacy", json={"description": "renamed"})
    assert response.status_code == 200, response.text
    assert response.json()["permissions"] == ["translate"]
    assert role_repository.roles["Legacy"].permissions == ["translate"]


def test_removed_rights_are_rejected_on_every_save_path(auth_client: TestClient):
    """An explicit removed right is "cannot be granted" — a 400, not silent drop."""
    user_save = auth_client.post(
        "/api/users/u-1/permissions", json={"permissions": ["journal:view"]}
    )
    assert user_save.status_code == 400
    assert "Unknown permissions" in user_save.json()["detail"]

    role_save = auth_client.post(
        "/api/roles", json={"name": "Bad", "permissions": ["journal:view"]}
    )
    assert role_save.status_code == 400
    assert "Unknown permissions" in role_save.json()["detail"]


def test_builtin_admin_role_cannot_be_modified(auth_client: TestClient):
    response = auth_client.patch("/api/roles/admin", json={"description": "hacked"})
    assert response.status_code == 403
    assert "protected" in response.json()["detail"].lower() or "built-in" in response.json()["detail"].lower()


def test_builtin_api_role_cannot_be_deleted(auth_client: TestClient):
    assert auth_client.delete("/api/roles/api").status_code == 403


def test_builtin_admin_role_cannot_be_deleted(auth_client: TestClient):
    assert auth_client.delete("/api/roles/admin").status_code == 403


def test_builtin_user_role_cannot_be_deleted(auth_client: TestClient):
    # The built-in `user` role is protected exactly like admin/api.
    assert auth_client.delete("/api/roles/user").status_code == 403


def test_delete_role_with_members_is_blocked(auth_client: TestClient, user_repository):
    auth_client.post("/api/roles", json={"name": "Translator"})
    auth_client.post("/api/users/u-1/role", json={"role": "Translator"})
    response = auth_client.delete("/api/roles/Translator")
    assert response.status_code == 409
    assert "u-1" in response.json()["detail"]


def test_delete_unused_custom_role_removes_its_grants(auth_client: TestClient, grant_repository):
    auth_client.post("/api/roles", json={"name": "Translator"})
    auth_client.put(
        "/api/access/grant",
        json={
            "subject_type": "role",
            "subject": "Translator",
            "collection": "dtd",
            "read": True,
            "write": False,
        },
    )
    response = auth_client.delete("/api/roles/Translator")
    assert response.status_code == 200
    assert response.json()["grants_removed"] == 1
    assert ("role", "Translator", "dtd") not in grant_repository.grants
    # Unrelated grants survive.
    assert ("group", "DTD", "dtd") in grant_repository.grants


def test_role_members_endpoint(auth_client: TestClient):
    members = auth_client.get("/api/roles/user/members").json()
    assert {m["user_id"] for m in members} == {"u-1", "u-2"}


def test_delete_missing_role_is_404(auth_client: TestClient):
    assert auth_client.delete("/api/roles/ghost").status_code == 404


# --------------------------------------------------------------------------
# 5.5 — collection access matrix
# --------------------------------------------------------------------------
def test_matrix_includes_default_and_all_collection_sources(auth_client: TestClient):
    body = auth_client.get("/api/access/matrix").json()
    assert body["collections"] == ["default", "dtd", "oup"]
    labels = [row["label"] for row in body["rows"]]
    assert "role: admin" in labels
    assert "group: DTD" in labels
    assert "user: ivanov" in labels
    assert not any("api" == row["subject"] for row in body["rows"] if row["subject_type"] == "role")


def test_matrix_reflects_existing_grants(auth_client: TestClient):
    body = auth_client.get("/api/access/matrix").json()
    admin_row = next(r for r in body["rows"] if r["subject"] == "admin" and r["subject_type"] == "role")
    assert admin_row["cells"]["default"] == {"read": True, "write": True, "level": 3}
    dtd_row = next(r for r in body["rows"] if r["subject"] == "DTD")
    assert dtd_row["cells"]["dtd"] == {"read": True, "write": True, "level": 3}


def test_matrix_marks_admin_rows_unrestricted(auth_client: TestClient):
    """The built-in admin role, and users holding it, bypass collection checks."""
    body = auth_client.get("/api/access/matrix").json()
    admin_row = next(
        r for r in body["rows"] if r["subject_type"] == "role" and r["subject"] == "admin"
    )
    assert admin_row["unrestricted"] is True

    assert auth_client.post("/api/users/u-1/role", json={"role": "admin"}).status_code == 200
    body = auth_client.get("/api/access/matrix").json()
    promoted = next(
        r for r in body["rows"] if r["subject_type"] == "user" and r["subject"] == "u-1"
    )
    assert promoted["unrestricted"] is True

    other_user = next(
        r for r in body["rows"] if r["subject_type"] == "user" and r["subject"] == "u-2"
    )
    group = next(r for r in body["rows"] if r["subject_type"] == "group" and r["subject"] == "DTD")
    assert other_user["unrestricted"] is False
    assert group["unrestricted"] is False


def test_matrix_reads_and_writes_levels(
    auth_client: TestClient, grant_repository, event_repository
):
    """Level in, level out — with the legacy flags still on every cell."""
    response = auth_client.put(
        "/api/access/grant",
        json={"subject_type": "user", "subject": "u-1", "collection": "oup", "level": 2},
    )
    assert response.status_code == 200
    stored = grant_repository.grants[("user", "u-1", "oup")]
    assert stored.level == 2
    assert (stored.read, stored.write) == (True, False)  # add, but not modify

    row = next(
        r
        for r in auth_client.get("/api/access/matrix").json()["rows"]
        if r["subject_type"] == "user" and r["subject"] == "u-1"
    )
    assert row["cells"]["oup"] == {"read": True, "write": False, "level": 2}

    audit = [e for e in event_repository.events if e["action"] == "access.grant_upserted"]
    assert audit[-1]["details"]["level"] == 2


def test_matrix_survives_mysql_outage(auth_client: TestClient, collection_source):
    collection_source.fail = True
    body = auth_client.get("/api/access/matrix").json()
    assert "default" in body["collections"]
    assert "dtd" in body["collections"]  # still known from the seeded grant


def test_toggle_grant_persists_and_bumps_version(auth_client: TestClient, grant_repository, version):
    before = version.value
    response = auth_client.put(
        "/api/access/grant",
        json={
            "subject_type": "group",
            "subject": "HR",
            "collection": "oup",
            "read": True,
            "write": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["config_version"] > before
    stored = grant_repository.grants[("group", "HR", "oup")]
    assert (stored.read, stored.write) == (True, False)

    matrix = auth_client.get("/api/access/matrix").json()
    hr_row = next(r for r in matrix["rows"] if r["subject"] == "HR")
    assert hr_row["cells"]["oup"] == {"read": True, "write": False, "level": 1}


def test_toggle_grant_off_rewrites_flags(auth_client: TestClient, grant_repository):
    auth_client.put(
        "/api/access/grant",
        json={"subject_type": "group", "subject": "DTD", "collection": "dtd", "read": False, "write": False},
    )
    stored = grant_repository.grants[("group", "DTD", "dtd")]
    assert (stored.read, stored.write) == (False, False)


def test_grant_for_unknown_role_is_rejected(auth_client: TestClient):
    response = auth_client.put(
        "/api/access/grant",
        json={"subject_type": "role", "subject": "ghost", "collection": "dtd", "read": True},
    )
    assert response.status_code == 404


def test_grant_rejects_invalid_subject_type(auth_client: TestClient):
    response = auth_client.put(
        "/api/access/grant",
        json={"subject_type": "team", "subject": "x", "collection": "dtd", "read": True},
    )
    assert response.status_code == 422


def test_delete_grant(auth_client: TestClient, grant_repository):
    response = auth_client.request(
        "DELETE",
        "/api/access/grant",
        json={"subject_type": "group", "subject": "DTD", "collection": "dtd", "read": True, "write": True},
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert ("group", "DTD", "dtd") not in grant_repository.grants


def test_default_write_requires_explicit_grant(auth_client: TestClient):
    """`default` read is implicit for everyone, so no grant row is needed."""
    body = auth_client.get("/api/access/matrix").json()
    ivanov = next(r for r in body["rows"] if r["subject"] == "u-1")
    assert ivanov["cells"]["default"] == {"read": False, "write": False, "level": 0}


# --------------------------------------------------------------------------
# 5.6 — settings
# --------------------------------------------------------------------------
def test_settings_show_config_version_and_counts(auth_client: TestClient):
    body = auth_client.get("/api/settings").json()
    assert body["admin_username"] == ADMIN_USER
    assert body["config_version"] == 0
    assert body["password_source"] == "env"
    assert body["counts"]["users"] == 3
    assert body["counts"]["manual_roles"] == 1


def test_change_password_then_old_one_fails(auth_client: TestClient):
    response = auth_client.post(
        "/api/settings/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "brand-new-pass"},
    )
    assert response.status_code == 200
    assert response.json()["changed"] is True

    fresh = TestClient(auth_client.app)
    assert fresh.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    ).status_code == 401
    assert fresh.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": "brand-new-pass"}
    ).status_code == 200


def test_settings_reports_mongo_source_after_change(auth_client: TestClient):
    auth_client.post(
        "/api/settings/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "brand-new-pass"},
    )
    assert auth_client.get("/api/settings").json()["password_source"] == "mongo"


def test_change_password_requires_correct_current(auth_client: TestClient):
    response = auth_client.post(
        "/api/settings/password",
        json={"current_password": "nope", "new_password": "brand-new-pass"},
    )
    assert response.status_code == 400


def test_change_password_rejects_short_and_identical(auth_client: TestClient):
    assert auth_client.post(
        "/api/settings/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "abc"},
    ).status_code == 400
    assert auth_client.post(
        "/api/settings/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD},
    ).status_code == 400


# --------------------------------------------------------------------------
# 5.7 — audit trail
# --------------------------------------------------------------------------
def test_every_mutation_writes_an_audit_record(auth_client: TestClient, event_repository):
    auth_client.post("/api/users/u-1/role", json={"role": "admin"})
    auth_client.post("/api/users/u-1/active", json={"is_active": False})
    auth_client.post("/api/users/u-1/permissions", json={"permissions": ["system:manage"]})
    auth_client.post("/api/users/u-1/reset-role")
    auth_client.post("/api/roles", json={"name": "Translator"})
    auth_client.patch("/api/roles/Translator", json={"description": "d"})
    auth_client.put(
        "/api/access/grant",
        json={"subject_type": "group", "subject": "HR", "collection": "oup", "read": True},
    )
    auth_client.request(
        "DELETE",
        "/api/access/grant",
        json={"subject_type": "group", "subject": "HR", "collection": "oup", "read": True},
    )
    auth_client.post(
        "/api/settings/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "brand-new-pass"},
    )
    auth_client.delete("/api/roles/Translator")

    expected = {
        "user.role_assigned",
        "user.activation_changed",
        "user.permissions_set",
        "user.role_reset_to_ldap",
        "role.created",
        "role.updated",
        "access.grant_upserted",
        "access.grant_deleted",
        "settings.password_changed",
        "role.deleted",
    }
    assert expected <= event_repository.actions()
    for event in event_repository.events:
        assert event["actor"] == ADMIN_USER
        assert event["created_at"]


def test_audit_feed_is_newest_first_and_reports_actor(auth_client: TestClient):
    auth_client.post("/api/users/u-1/role", json={"role": "admin"})
    auth_client.post("/api/users/u-2/role", json={"role": "user"})
    feed = auth_client.get("/api/audit").json()
    assert feed[0]["target"] == "u-2"
    assert feed[0]["actor"] == ADMIN_USER
    assert feed[0]["details"]["to"] == "user"


def test_dashboard_shows_counters_and_audit(auth_client: TestClient):
    auth_client.post("/api/users/u-1/role", json={"role": "admin"})
    body = auth_client.get("/api/dashboard").json()
    assert body["counts"]["users"] == 3
    assert body["counts"]["manual_roles"] == 2
    assert body["recent_events"]


def test_audit_failure_does_not_break_mutation(auth_client: TestClient, event_repository, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(event_repository, "log", boom)
    response = auth_client.post("/api/users/u-1/role", json={"role": "admin"})
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Admin guide — rendered on the server from the repository's Markdown
# --------------------------------------------------------------------------
GUIDE_SOURCE = """# Заголовок

Первый раздел со `кодом`.

## Подраздел

| колонка | значение |
| --- | --- |
| a | b |

```bash
echo hi
```

## Второй раздел

Обратно к [документации](/docs).
"""


@pytest.fixture
def guide(tmp_path: Path) -> Path:
    path = tmp_path / "admin-guide.md"
    path.write_text(GUIDE_SOURCE, encoding="utf-8")
    return path


def _login(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def guide_client(container: AdminContainer, guide: Path):
    """The admin app, signed in, pointed at a temporary guide file."""
    config = replace(container.config, guide_path=guide)
    with TestClient(create_app(container=replace(container, config=config))) as test_client:
        _login(test_client)
        yield test_client


def test_admin_guide_returns_article_and_outline(guide_client: TestClient):
    response = guide_client.get("/api/docs/admin-guide")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["title"] == "Заголовок"
    # Tables and fenced code are what the real guide is made of.
    assert "<table>" in body["html"]
    assert "<pre>" in body["html"]
    assert [(s["level"], s["text"]) for s in body["sections"]] == [
        (1, "Заголовок"),
        (2, "Подраздел"),
        (2, "Второй раздел"),
    ]

    # Every outline entry has to point at an element that is really in the
    # article, and ids must be stable fragments rather than positional counters
    # (`_1`, `_2`), which shift whenever the guide gains a section.
    ids = [section["id"] for section in body["sections"]]
    assert len(set(ids)) == len(ids)
    assert not any(section_id.startswith("_") for section_id in ids)
    for section_id in ids:
        assert f'id="{section_id}"' in body["html"]


def test_admin_guide_requires_a_session(client: TestClient):
    response = client.get("/api/docs/admin-guide")
    assert response.status_code == 401
    assert "<h1" not in response.text
    assert "html" not in response.json()


def test_admin_guide_missing_file_is_503(container: AdminContainer, tmp_path: Path):
    config = replace(container.config, guide_path=tmp_path / "absent.md")
    with TestClient(create_app(container=replace(container, config=config))) as anonymous:
        _login(anonymous)
        response = anonymous.get("/api/docs/admin-guide")
    assert response.status_code == 503
    assert "absent.md" in response.json()["detail"]


def test_admin_guide_picks_up_an_edit_without_a_rebuild(guide_client: TestClient, guide: Path):
    first = guide_client.get("/api/docs/admin-guide").json()
    assert "Третий раздел" not in first["html"]

    guide.write_text(GUIDE_SOURCE + "\n## Третий раздел\n\nДобавлено.\n", encoding="utf-8")
    # Timestamps are coarse; move the file forward explicitly so the test
    # asserts the cache key rather than the clock's resolution.
    stamp = guide.stat().st_mtime + 2
    os.utime(guide, (stamp, stamp))

    second = guide_client.get("/api/docs/admin-guide").json()
    assert "Третий раздел" in second["html"]
    assert len(second["sections"]) == len(first["sections"]) + 1


def test_documentation_page_is_served_without_a_session(client: TestClient):
    page = client.get("/docs")
    assert page.status_code == 200
    assert "docs.js" in page.text
