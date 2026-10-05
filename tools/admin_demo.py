"""Dev-only admin service runner backed by in-memory stores.

Used for the manual browser flow of OpenSpec task 5.8 — no Mongo, Redis or
MySQL required:

    python tools/admin_demo.py            # http://127.0.0.1:8011, admin/admin123
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin_service.app import create_app  # noqa: E402
from admin_service.config import AdminConfig, default_guide_path  # noqa: E402
from admin_service.deps import AdminContainer, AdminPasswordStore, SessionTokens  # noqa: E402
from file_translator.domain.auth import RoleType, User  # noqa: E402
from file_translator.infrastructure.auth.role_config import (  # noqa: E402
    DEFAULT_ROLES,
    GrantDoc,
    GrantStore,
    RoleConfigStore,
    RoleDoc,
)


class MemRoleRepo:
    def __init__(self) -> None:
        self.roles = {n: RoleDoc.from_dict(dict(d)) for n, d in DEFAULT_ROLES.items()}

    async def ensure_indexes(self) -> None: ...
    async def find_all(self) -> list[RoleDoc]: return list(self.roles.values())
    async def find_by_name(self, name): return self.roles.get(name)
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

    async def delete(self, name: str) -> bool: return self.roles.pop(name, None) is not None
    async def count(self) -> int: return len(self.roles)


class MemGrantRepo:
    def __init__(self) -> None:
        self.grants: dict[tuple[str, str, str], GrantDoc] = {
            ("role", "admin", "default"): GrantDoc("role", "admin", "default", True, True),
            ("group", "DTD", "dtd"): GrantDoc("group", "DTD", "dtd", True, True),
        }

    async def ensure_indexes(self) -> None: ...
    async def find_all(self) -> list[GrantDoc]: return list(self.grants.values())
    async def find_by_subject(self, t: str, s: str) -> list[GrantDoc]:
        return [g for g in self.grants.values() if g.subject_type == t and g.subject == s]

    async def find_by_subject_types(self, mapping: dict[str, list[str]]) -> list[GrantDoc]:
        out: list[GrantDoc] = []
        for t, subjects in mapping.items():
            for s in subjects:
                out.extend(await self.find_by_subject(t, s))
        return out

    async def upsert(self, grant: GrantDoc) -> GrantDoc:
        self.grants[(grant.subject_type, grant.subject, grant.collection)] = grant
        return grant

    async def delete(self, grant: GrantDoc) -> bool:
        return self.grants.pop((grant.subject_type, grant.subject, grant.collection), None) is not None

    async def delete_many(self, subject_type=None, subject=None, collection=None) -> int:
        victims = [
            k for k in self.grants
            if (subject_type is None or k[0] == subject_type)
            and (subject is None or k[1] == subject)
            and (collection is None or k[2] == collection)
        ]
        for k in victims:
            del self.grants[k]
        return len(victims)

    async def count(self) -> int: return len(self.grants)


class MemUserRepo:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}

    def add(self, user_id: str, username: str, role: str, groups: list[str]) -> None:
        user = User(user_id=user_id, username=username, display_name=username.title())
        user.role_name = role
        user.ldap_groups = groups
        user.manual_role = False
        try:
            user.role = RoleType(role)
        except ValueError:
            user.role = RoleType.VIEWER
        self.users[user_id] = user

    async def get_by_id(self, user_id: str): return self.users.get(user_id)
    async def get_by_username(self, username: str):
        return next((u for u in self.users.values() if u.username == username), None)

    async def create(self, user: User) -> User:
        self.users[user.user_id] = user
        return user

    async def update(self, user: User) -> User:
        self.users[user.user_id] = user
        return user

    async def delete(self, user_id: str) -> bool: return self.users.pop(user_id, None) is not None
    async def list_all(self) -> list[User]: return list(self.users.values())


class MemEventRepo:
    def __init__(self) -> None:
        from datetime import datetime, timezone

        self.events: list[dict[str, Any]] = []
        self._now = lambda: datetime.now(timezone.utc).isoformat()

    async def ensure_indexes(self) -> None: ...
    async def log(self, actor, action, target_type="", target="", details=None):
        event = {
            "actor": actor, "action": action, "target_type": target_type,
            "target": target, "details": details or {}, "created_at": self._now(),
        }
        self.events.append(event)
        return event

    async def list_recent(self, limit: int = 50): return list(reversed(self.events))[:limit]
    async def count(self) -> int: return len(self.events)


class MemSettingsCollection:
    def __init__(self) -> None:
        self.docs: dict[Any, dict[str, Any]] = {}

    async def find_one(self, query):
        return self.docs.get(query.get("_id"))

    async def update_one(self, query, update, upsert=False):
        key = query.get("_id")
        self.docs.setdefault(key, {"_id": key}).update(update.get("$set", {}))


class MemVersion:
    def __init__(self) -> None:
        self.value = 0

    async def get_version(self) -> int: return self.value

    async def bump(self) -> int:
        self.value += 1
        return self.value


class MemCollections:
    async def list_collections(self) -> list[str]: return ["default", "dtd", "oup"]


def build_container(username: str, password: str) -> AdminContainer:
    version = MemVersion()
    role_repo, grant_repo = MemRoleRepo(), MemGrantRepo()
    users = MemUserRepo()
    users.add("u-1", "ivanov", "user", ["DTD"])
    users.add("u-2", "petrova", "user", ["HR"])
    users.add("u-3", "sidorov", "api", [])

    class MemDb:
        admin_settings = MemSettingsCollection()

    config = AdminConfig(
        username=username, password=password, mongo_uri="", mongo_db="", redis_host="",
        redis_port=6379, redis_password="", mysql_host="", mysql_port=3306, mysql_user="",
        mysql_password="", mysql_db="glossary", host="127.0.0.1", port=8011, log_level="INFO",
        # Honoured like in production, so a broken guide path can be reproduced
        # locally: ADMIN_GUIDE_PATH=/nonexistent.md python tools/admin_demo.py
        guide_path=Path(os.environ.get("ADMIN_GUIDE_PATH") or default_guide_path()),
    )
    return AdminContainer(
        config=config,
        role_store=RoleConfigStore(role_repo, version_provider=version.get_version, version_bumper=version.bump),
        grant_store=GrantStore(grant_repo, version_provider=version.get_version, version_bumper=version.bump),
        event_repo=MemEventRepo(),
        user_repo=users,
        collection_source=MemCollections(),
        config_version=version,
        passwords=AdminPasswordStore(MemDb(), password),
        sessions=SessionTokens("demo-secret"),
    )


def main() -> None:
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    username = os.environ.get("ADMIN_UI_USERNAME", "admin")
    password = os.environ.get("ADMIN_UI_PASSWORD", "admin123")
    app = create_app(container=build_container(username, password))
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ADMIN_PORT", "8011")), log_level="warning")


if __name__ == "__main__":
    main()

