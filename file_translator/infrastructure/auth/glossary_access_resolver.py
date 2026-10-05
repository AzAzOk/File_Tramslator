"""Resolves user access to glossary collections from runtime grants.

The resolver computes, per user, the set of collection IDs they may read and
the set they may write:

- The built-in ``admin`` role bypasses both sets (unrestricted).
- ``default`` is hardcoded into every read set (readable by everyone with
  glossary view permission) and is never implicitly writable.
- Every other collection comes from MongoDB ``grants`` documents matching the
  user's AD groups, role name(s), or user id.

While the grants collection is empty (before the first seed) the resolver
falls back to the legacy ``GLOSSARY_COLLECTION_MAP`` environment variable to
preserve pre-2.x behaviour. After seeding the env var is no longer consulted
(see task 6.2).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from file_translator.domain.auth import User
from file_translator.infrastructure.auth.role_config import (
    DEFAULT_COLLECTION,
    DEFAULT_CACHE_TTL_SECONDS,
    GrantDoc,
    GrantStore,
    SUBJECT_GROUP,
    SUBJECT_ROLE,
    SUBJECT_USER,
)

logger = logging.getLogger(__name__)

ADMIN_ROLE_NAME = "admin"


@dataclass
class AccessSets:
    """Read/write collection sets for one user.

    ``read``/``write`` are ``None`` when the user is unrestricted (admin).
    """

    read: set[str] | None = None
    write: set[str] | None = None

    @property
    def unrestricted(self) -> bool:
        return self.read is None or self.write is None


class GlossaryAccessResolver:
    """Computes read/write glossary collection access for a user.

    Args:
        role_store: :class:`RoleConfigStore` used for the admin bypass check.
        grant_store: :class:`GrantStore` providing the grant documents.
        collection_map: optional legacy env map (falls back to the
            ``GLOSSARY_COLLECTION_MAP`` environment variable). Only consulted
            while the grants collection is empty.
    """

    def __init__(
        self,
        role_store: Any | None = None,
        grant_store: Any | None = None,
        collection_map: dict[str, list[str]] | None = None,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ):
        self._role_store = role_store
        self._grant_store = grant_store or GrantStore()
        self._collection_map = collection_map if collection_map is not None else self._load_from_env()
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _load_from_env() -> dict[str, list[str]]:
        raw = os.getenv("GLOSSARY_COLLECTION_MAP", "")
        if not raw:
            logger.info("GLOSSARY_COLLECTION_MAP not set — only 'default' collection available")
            return {}
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                logger.warning("GLOSSARY_COLLECTION_MAP is not a JSON object — ignoring")
                return {}
            return {str(k): list(v) if isinstance(v, list) else [str(v)] for k, v in parsed.items()}
        except json.JSONDecodeError as e:
            logger.warning(f"GLOSSARY_COLLECTION_MAP parse error: {e}")
            return {}

    # --- subject helpers -------------------------------------------------

    @staticmethod
    def _role_names_of(user: User) -> list[str]:
        names: list[str] = []
        raw = getattr(user, "role_name", "") or ""
        if raw:
            names.append(raw)
        if getattr(user, "role", None) is not None:
            names.append(user.role.value)
        return list(dict.fromkeys(names))

    @staticmethod
    def _is_admin(user: User) -> bool:
        return any(name == ADMIN_ROLE_NAME for name in GlossaryAccessResolver._role_names_of(user))

    async def _grants_for_user(self, user: User) -> list[GrantDoc]:
        subjects_by_type = {
            SUBJECT_GROUP: sorted(set(user.ldap_groups or [])),
            SUBJECT_ROLE: self._role_names_of(user),
            SUBJECT_USER: [user.user_id] if getattr(user, "user_id", "") else [],
        }
        return await self._grant_store.find_grants_for_subjects(subjects_by_type)

    async def _grants_configured(self) -> bool:
        """True when the grants collection holds at least one document.

        The legacy env map is a *pre-seed* compatibility shim, so the decision
        must be based on whether any grant exists at all — not on whether this
        particular user matches one. Otherwise a user whose grants an admin
        removed would silently regain access from the env map for as long as
        some unrelated subject still had a grant (task 6.2).
        """
        try:
            return bool(await self._grant_store.get_all_grants())
        except Exception as e:
            logger.warning(f"Grant store unavailable — using env map: {e}")
            return False

    # --- resolution -------------------------------------------------------

    async def resolve_access(self, user: User) -> AccessSets:
        """Return the user's (read, write) collection sets.

        ``None`` means unrestricted (built-in admin role bypass).
        """
        if self._is_admin(user):
            return AccessSets(read=None, write=None)

        if not await self._grants_configured():
            # Grants not seeded yet — preserve the legacy env-map behaviour.
            return self._resolve_legacy(user)

        grants = await self._grants_for_user(user)
        read: set[str] = {DEFAULT_COLLECTION}
        write: set[str] = set()
        for g in grants:
            if g.read:
                read.add(g.collection)
            if g.write:
                write.add(g.collection)
        return AccessSets(read=read, write=write)

    def _resolve_legacy(self, user: User) -> AccessSets:
        """Env-map fallback used only while the grants collection is empty."""
        read: set[str] = {DEFAULT_COLLECTION}
        write: set[str] = set()
        for group_name in user.ldap_groups or []:
            for cid in self._collection_map.get(group_name, []):
                read.add(cid)
                write.add(cid)
        return AccessSets(read=read, write=write)

    async def can_access(self, user: User, collection_id: str, level: str = "read") -> bool:
        """Check read/write access to one collection for a user."""
        sets = await self.resolve_access(user)
        read, write = sets.read, sets.write
        if read is None or write is None:
            return True
        target = read if level == "read" else write
        return collection_id in target

    async def can_read(self, user: User, collection_id: str) -> bool:
        return await self.can_access(user, collection_id, "read")

    async def can_write(self, user: User, collection_id: str) -> bool:
        return await self.can_access(user, collection_id, "write")

    # --- legacy synchronous API (kept for tests / pre-seed callers) -------

    def resolve(self, groups: list[str] | None) -> list[str]:
        """Legacy env-map resolution (group → collection ids).

        Always includes ``default``; only consults ``GLOSSARY_COLLECTION_MAP``.
        """
        allowed: set[str] = set()
        if groups:
            for group_name in groups:
                if group_name in self._collection_map:
                    allowed.update(self._collection_map[group_name])
        if not allowed:
            allowed.add(DEFAULT_COLLECTION)
        return list(allowed)

    def is_collection_allowed(self, collection_id: str, groups: list[str] | None) -> bool:
        """Legacy synchronous check by AD groups alone."""
        return collection_id in self.resolve(groups)