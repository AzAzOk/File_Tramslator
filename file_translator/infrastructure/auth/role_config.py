"""Runtime role and collection-grant configuration.

Roles and grants are stored as MongoDB documents so an administrator can manage
them at runtime through the separate admin service. This module provides:

- :class:`RoleConfigStore` — cached access to the ``roles`` collection
- :class:`GrantStore` — cached access to the ``grants`` collection

Both keep an in-memory cache (default TTL 30 s) and can observe a Redis
``admin_config_version`` key so the main API picks up admin writes without a
restart. When MongoDB is unavailable — or a requested role is not found — the
stores fall back to Python defaults that mirror the legacy ``RoleType`` enum
(:data:`DEFAULT_ROLES`).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from file_translator.domain.auth import Permission, RoleType, get_permissions_for_role

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_SECONDS = 30

# The standard person role. It has no RoleType enum member — it replaces the
# legacy "operator" role and is managed as a Mongo document.
ROLE_USER = "user"

# Subject types accepted by the grants collection.
SUBJECT_GROUP = "group"
SUBJECT_ROLE = "role"
SUBJECT_USER = "user"
SUBJECT_TYPES = (SUBJECT_GROUP, SUBJECT_ROLE, SUBJECT_USER)

# The collection readable by every authenticated user with glossary view.
DEFAULT_COLLECTION = "default"


def _permission_values(*permissions: Permission) -> list[str]:
    return [p.value for p in permissions]


def _default_role(name: str) -> dict[str, Any]:
    """Build one of the three built-in role documents (mirrors RoleType)."""
    if name == RoleType.ADMIN.value:
        permissions = [p.value for p in Permission]
        description = "Full access"
    elif name == ROLE_USER:
        # The "user" role is the successor of the legacy "operator" role —
        # standard permissions for a person using the system.
        permissions = _permission_values(
            Permission.TRANSLATE,
            Permission.VIEW_GLOSSARY,
            Permission.EDIT_GLOSSARY,
            Permission.VIEW_JOBS,
            Permission.CANCEL_JOBS,
            Permission.VIEW_JOURNAL,
            Permission.SEND_FEEDBACK,
        )
        description = "Standard permissions for people"
    elif name == RoleType.API.value:
        permissions = _permission_values(Permission.TRANSLATE, Permission.VIEW_JOBS)
        description = "Machine-account role: translate only"
    else:  # legacy compatibility (operator/viewer) — resolved to user internally
        permissions = _permission_values(
            *get_permissions_for_role(RoleType(name)),
        )
        description = f"Legacy role '{name}' (migrated to 'user')"
    return {
        "name": name,
        "description": description,
        "builtin": True,
        "protected": True,
        "permissions": permissions,
        "grants": [],
    }


#: Python fallback used when MongoDB is unavailable. Mirrors the seeded
#: built-in roles exactly so the fallback never diverges from Mongo data.
DEFAULT_ROLES: dict[str, dict[str, Any]] = {
    RoleType.ADMIN.value: _default_role(RoleType.ADMIN.value),
    ROLE_USER: _default_role(ROLE_USER),
    RoleType.API.value: _default_role(RoleType.API.value),
    RoleType.OPERATOR.value: _default_role(RoleType.OPERATOR.value),
    RoleType.VIEWER.value: _default_role(RoleType.VIEWER.value),
}

BUILTIN_ROLE_NAMES = {RoleType.ADMIN.value, ROLE_USER, RoleType.API.value}


@dataclass
class RoleDoc:
    """A role document as stored in MongoDB."""

    name: str
    description: str = ""
    builtin: bool = False
    protected: bool = False
    permissions: list[str] = field(default_factory=list)
    grants: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, doc: dict[str, Any] | None) -> "RoleDoc | None":
        if not doc:
            return None
        return cls(
            name=str(doc.get("name", "")),
            description=str(doc.get("description", "") or ""),
            builtin=bool(doc.get("builtin", False)),
            protected=bool(doc.get("protected", False)),
            permissions=[str(p) for p in (doc.get("permissions") or [])],
            grants=[str(g) for g in (doc.get("grants") or [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "builtin": self.builtin,
            "protected": self.protected,
            "permissions": list(self.permissions),
            "grants": list(self.grants),
        }


@dataclass
class GrantDoc:
    """A collection-grant document as stored in MongoDB.

    ``subject_type`` is one of ``group``, ``role`` or ``user``; ``subject`` is
    the identifier (AD group CN, role name or user id).
    """

    subject_type: str
    subject: str
    collection: str
    read: bool = False
    write: bool = False

    @classmethod
    def from_dict(cls, doc: dict[str, Any] | None) -> "GrantDoc | None":
        if not doc:
            return None
        return cls(
            subject_type=str(doc.get("subject_type", "")),
            subject=str(doc.get("subject", "")),
            collection=str(doc.get("collection", "")),
            read=bool(doc.get("read", False)),
            write=bool(doc.get("write", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_type": self.subject_type,
            "subject": self.subject,
            "collection": self.collection,
            "read": self.read,
            "write": self.write,
        }


VersionProvider = Callable[[], Awaitable[int | None]]
VersionBumper = Callable[[], Awaitable[Any]]


def _null_version_provider() -> Awaitable[int | None]:
    async def _noop() -> int | None:
        return None

    return _noop()


class _CachedStore:
    """Base class: TTL cache with an optional Redis version observer."""

    _cache_lock = threading.Lock()

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        version_provider: VersionProvider | None = None,
        version_bumper: VersionBumper | None = None,
    ):
        self._ttl_seconds = max(1, ttl_seconds)
        self._version_provider = version_provider
        self._version_bumper = version_bumper
        self._loaded_at: float = 0.0
        self._observed_version: int | None = None
        self._version_error_reported = False

    def _reset_cache(self) -> None:
        self._loaded_at = 0.0
        self._observed_version = None

    async def _notify_write(self) -> None:
        """Invalidate this cache and bump the shared config version.

        The bump makes every *other* process (the main API, other admin
        instances) drop its cached roles/grants on the next read instead of
        waiting out the TTL. Failures are logged and ignored — a stale read is
        bounded by the TTL, whereas a failed write must surface to the caller.
        """
        self._reset_cache()
        if not self._version_bumper:
            return
        try:
            await self._version_bumper()
        except Exception as e:
            logger.warning(f"Config version bump failed (peers refresh within TTL): {e}")

    async def _version_changed(self) -> bool:
        """True when Redis reports a newer admin_config_version than observed.

        Returns False when Redis is unavailable (the TTL bounds staleness).
        """
        if not self._version_provider:
            return False
        try:
            version = await self._version_provider()
            if version is None:
                return False
            self._version_error_reported = False
            if self._observed_version is None:
                self._observed_version = version
                return False
            if version != self._observed_version:
                self._observed_version = version
                return True
        except Exception as e:  # Redis down — rely on TTL
            if not self._version_error_reported:
                logger.warning(f"Config version check failed (TTL fallback): {e}")
                self._version_error_reported = True
        return False

    def _is_fresh(self) -> bool:
        return self._loaded_at > 0 and (time.monotonic() - self._loaded_at) < self._ttl_seconds


class RoleConfigStore(_CachedStore):
    """Cached reader/writer for the MongoDB ``roles`` collection.

    Falls back to :data:`DEFAULT_ROLES` when Mongo is unavailable or a role is
    unknown. ``repository`` is any object exposing the Mongo CRUD contract
    (:class:`~file_translator.infrastructure.repositories.mongo_config_repository.MongoRoleRepository`).
    """

    def __init__(
        self,
        repository: Any | None = None,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        version_provider: VersionProvider | None = None,
        version_bumper: VersionBumper | None = None,
    ):
        super().__init__(
            ttl_seconds=ttl_seconds,
            version_provider=version_provider,
            version_bumper=version_bumper,
        )
        self._repository = repository
        self._roles: dict[str, RoleDoc] | None = None

    @property
    def repository(self) -> Any:
        return self._repository

    async def _load(self) -> dict[str, RoleDoc]:
        """Reload all roles from Mongo (fallback to Python defaults)."""
        try:
            if self._repository is not None:
                docs = await self._repository.find_all()
                if docs:
                    return {d.name: d for d in docs}
                logger.info("roles collection empty — falling back to Python defaults")
            else:
                logger.debug("No role repository configured — using Python defaults")
        except Exception as e:
            logger.warning(f"Role load from Mongo failed — using Python defaults: {e}")
        return {
            name: RoleDoc.from_dict(doc)  # type: ignore[arg-type]
            for name, doc in DEFAULT_ROLES.items()
        }

    async def _ensure_loaded(self) -> dict[str, RoleDoc]:
        if self._roles is None or not self._is_fresh() or await self._version_changed():
            self._roles = await self._load()
            self._loaded_at = time.monotonic()
        return self._roles

    async def get_role(self, name: str) -> RoleDoc | None:
        roles = await self._ensure_loaded()
        role = roles.get(name) or roles.get(RoleType.OPERATOR.value)
        if role is None:
            return None
        return role

    async def get_all_roles(self) -> list[RoleDoc]:
        roles = await self._ensure_loaded()
        return list(roles.values())

    async def resolve_permissions(self, role_name: str | None) -> set[str]:
        """Resolve the permission-values of a role name via the registry.

        Unknown/custom roles that are absent from Mongo fall back to the
        legacy ``operator`` permissions (standard person) so a freshly
        created but not-yet-seeded role name still behaves like ``user``.
        """
        if not role_name:
            # Legacy viewer default from the User dataclass.
            return {p.value for p in get_permissions_for_role(RoleType.VIEWER)}
        role = await self.get_role(role_name)
        if role is not None:
            return set(role.permissions)
        # Roles absent from DEFAULT_ROLES fall back to the standard user role.
        return set(DEFAULT_ROLES[ROLE_USER]["permissions"])

    # --- Admin-write helpers (used by the admin service / seeding) ---

    async def create_role(self, role: RoleDoc) -> RoleDoc:
        if self._repository is None:
            raise RuntimeError("RoleConfigStore has no repository configured")
        await self._repository.create(role)
        await self._notify_write()
        return role

    async def update_role(self, name: str, fields: dict[str, Any]) -> RoleDoc | None:
        if self._repository is None:
            raise RuntimeError("RoleConfigStore has no repository configured")
        role = await self.get_role(name)
        if role is None:
            return None
        await self._repository.update(name, fields)
        await self._notify_write()
        return role

    async def delete_role(self, name: str) -> bool:
        if self._repository is None:
            raise RuntimeError("RoleConfigStore has no repository configured")
        result = await self._repository.delete(name)
        await self._notify_write()
        return result

    async def reload(self) -> None:
        self._reset_cache()
        await self._ensure_loaded()


class GrantStore(_CachedStore):
    """Cached reader/writer for the MongoDB ``grants`` collection."""

    def __init__(
        self,
        repository: Any | None = None,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        version_provider: VersionProvider | None = None,
        version_bumper: VersionBumper | None = None,
    ):
        super().__init__(
            ttl_seconds=ttl_seconds,
            version_provider=version_provider,
            version_bumper=version_bumper,
        )
        self._repository = repository
        self._grants: list[GrantDoc] | None = None

    @property
    def repository(self) -> Any:
        return self._repository

    async def _load(self) -> list[GrantDoc]:
        try:
            if self._repository is not None:
                docs = await self._repository.find_all()
                return docs if docs else []
            logger.debug("No grant repository configured — empty grants")
        except Exception as e:
            logger.warning(f"Grant load from Mongo failed — empty grants: {e}")
        return []

    async def _ensure_loaded(self) -> list[GrantDoc]:
        if self._grants is None or not self._is_fresh() or await self._version_changed():
            self._grants = await self._load()
            self._loaded_at = time.monotonic()
        return self._grants

    async def get_all_grants(self) -> list[GrantDoc]:
        return list(await self._ensure_loaded())

    async def find_grants_for_subjects(self, subjects_by_type: dict[str, list[str]]) -> list[GrantDoc]:
        """Return grants matching any subject.

        ``subjects_by_type`` maps subject_type (group/role/user) to a list of
        subject identifiers; a grant matches when its type+subject pair appears.
        """
        all_grants = await self._ensure_loaded()
        matched: list[GrantDoc] = []
        for g in all_grants:
            candidates = subjects_by_type.get(g.subject_type) or []
            if g.subject in candidates:
                matched.append(g)
        return matched

    # --- Admin-write helpers (used by the admin service / seeding) ---

    async def upsert_grant(self, grant: GrantDoc) -> GrantDoc:
        if self._repository is None:
            raise RuntimeError("GrantStore has no repository configured")
        await self._repository.upsert(grant)
        await self._notify_write()
        return grant

    async def delete_grant(self, grant: GrantDoc) -> bool:
        if self._repository is None:
            raise RuntimeError("GrantStore has no repository configured")
        result = await self._repository.delete(grant)
        await self._notify_write()
        return result

    async def delete_many(
        self,
        subject_type: str | None = None,
        subject: str | None = None,
        collection: str | None = None,
    ) -> int:
        if self._repository is None:
            raise RuntimeError("GrantStore has no repository configured")
        result = await self._repository.delete_many(
            subject_type=subject_type, subject=subject, collection=collection,
        )
        await self._notify_write()
        return result

    async def reload(self) -> None:
        self._reset_cache()
        await self._ensure_loaded()