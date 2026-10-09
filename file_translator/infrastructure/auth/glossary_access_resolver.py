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
    ACCESS_LEVEL_ADD,
    ACCESS_LEVEL_MODIFY,
    ACCESS_LEVEL_NONE,
    ACCESS_LEVEL_VIEW,
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

    #: Which level a check demands. ``write`` is kept as the old name for
    #: ``modify`` so existing call sites keep working until they are split.
    LEVEL_REQUIRED: dict[str, int] = {
        "read": ACCESS_LEVEL_VIEW,
        "create": ACCESS_LEVEL_ADD,
        "write": ACCESS_LEVEL_MODIFY,
        "modify": ACCESS_LEVEL_MODIFY,
    }

    async def resolve_levels(
        self, user: User, *, include_personal: bool = True
    ) -> dict[str, int] | None:
        """Per-collection access level for a user; ``None`` means unrestricted.

        The rule, in order:

        - the built-in ``admin`` role bypasses everything;
        - the levels of the role and of the AD groups combine to their maximum;
        - a personal grant for this user replaces that inherited value outright,
          so it may raise *or* lower it (design D5) — unless ``include_personal``
          is False, which answers "what would this person have from the role
          and their groups alone?";
        - ``default`` is floored at level 1, because its readability is not
          revocable (spec: `glossary-access`).
        """
        if self._is_admin(user):
            return None

        if not await self._grants_configured():
            # Grants not seeded yet — preserve the legacy env-map behaviour.
            return self._levels_from_legacy(self._resolve_legacy(user))

        grants = await self._grants_for_user(user)
        levels: dict[str, int] = {}
        personal: dict[str, int] = {}
        for g in grants:
            grant_level = int(g.level or 0)
            if g.subject_type == SUBJECT_USER:
                personal[g.collection] = grant_level
                continue
            levels[g.collection] = max(levels.get(g.collection, ACCESS_LEVEL_NONE), grant_level)

        if include_personal:
            # Existence is the switch: a personal document holding level 0 is a
            # real denial, not an unspecified value to fall back from.
            for collection, grant_level in personal.items():
                levels[collection] = grant_level

        return self._floor_default(levels)

    async def resolve_levels_with_sources(
        self, user: User, *, include_personal: bool = True
    ) -> dict[str, tuple[int, str]] | None:
        """Per-collection ``(level, source)`` for a user; ``None`` = unrestricted.

        The level is exactly what :meth:`resolve_levels` computes — the admin
        dialog must never explain a number the enforcement would disagree
        with. The source only attributes it:

        - ``personal``     — a grant for this user id (overrides everything);
        - ``role:<name>``  — the strongest role grant;
        - ``group:<name>`` — the strongest AD-group grant;
        - ``default``      — the unrevocable floor on ``default``, not a grant;
        - ``none``         — no matching document, level 0.

        Ties inside one subject kind (two roles granting the same level) name
        the first document found; either name is truthful, and the level is
        the same either way.
        """
        if self._is_admin(user):
            return None

        if not await self._grants_configured():
            levels = self._levels_from_legacy(self._resolve_legacy(user))
            sources: dict[str, str] = {}
            for collection in levels:
                sources[collection] = (
                    "default" if collection == DEFAULT_COLLECTION else "none"
                )
            # The legacy map is keyed by AD group, so that is the only source
            # it can name; `default` keeps its floor label unless a group
            # actually granted it.
            for group_name in user.ldap_groups or []:
                for collection in self._collection_map.get(group_name, []):
                    if collection in levels:
                        sources[collection] = f"group:{group_name}"
            return {c: (lvl, sources[c]) for c, lvl in levels.items()}

        role_levels: dict[str, int] = {}
        role_sources: dict[str, str] = {}
        group_levels: dict[str, int] = {}
        group_sources: dict[str, str] = {}
        personal: dict[str, tuple[int, str]] = {}
        for grant in await self._grants_for_user(user):
            grant_level = int(grant.level or 0)
            if grant.subject_type == SUBJECT_USER:
                personal[grant.collection] = (grant_level, "personal")
            elif grant.subject_type == SUBJECT_GROUP:
                if grant_level > group_levels.get(grant.collection, ACCESS_LEVEL_NONE - 1):
                    group_levels[grant.collection] = grant_level
                    group_sources[grant.collection] = f"group:{grant.subject}"
            else:
                if grant_level > role_levels.get(grant.collection, ACCESS_LEVEL_NONE - 1):
                    role_levels[grant.collection] = grant_level
                    role_sources[grant.collection] = f"role:{grant.subject}"

        levels: dict[str, int] = {}
        sources = {}
        for collection in set(role_levels) | set(group_levels):
            role_level = role_levels.get(collection, ACCESS_LEVEL_NONE)
            group_level = group_levels.get(collection, ACCESS_LEVEL_NONE)
            # A group-only document — including one stored at level 0 by
            # toggling a grant off — has no role label to fall back to, so
            # only take the role branch when the role actually granted it.
            if collection in role_sources and role_level >= group_level:
                levels[collection] = role_level
                sources[collection] = role_sources[collection]
            else:
                levels[collection] = group_level
                sources[collection] = group_sources[collection]

        if include_personal:
            for collection, (grant_level, _) in personal.items():
                levels[collection] = grant_level
                sources[collection] = "personal"

        self._floor_default(levels)
        for collection in levels:
            # The floor is not a document, so a `default` nobody granted says
            # `default` rather than pretending somebody granted it.
            sources.setdefault(
                collection,
                "default" if collection == DEFAULT_COLLECTION else "none",
            )
        return {c: (lvl, sources[c]) for c, lvl in levels.items()}

    @classmethod
    def _levels_from_legacy(cls, sets: AccessSets) -> dict[str, int]:
        levels: dict[str, int] = {
            collection: ACCESS_LEVEL_VIEW for collection in sets.read or set()
        }
        for collection in sets.write or set():
            levels[collection] = ACCESS_LEVEL_MODIFY
        return cls._floor_default(levels)

    @staticmethod
    def _floor_default(levels: dict[str, int]) -> dict[str, int]:
        levels[DEFAULT_COLLECTION] = max(
            levels.get(DEFAULT_COLLECTION, ACCESS_LEVEL_NONE), ACCESS_LEVEL_VIEW
        )
        return levels

    async def resolve_access(self, user: User) -> AccessSets:
        """Return the user's (read, write) collection sets.

        ``None`` means unrestricted (built-in admin role bypass).
        """
        levels = await self.resolve_levels(user)
        if levels is None:
            return AccessSets(read=None, write=None)
        return AccessSets(
            read={c for c, lvl in levels.items() if lvl >= ACCESS_LEVEL_VIEW},
            write={c for c, lvl in levels.items() if lvl >= ACCESS_LEVEL_MODIFY},
        )

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
        """Check one collection for a user at ``read``/``create``/``modify``."""
        levels = await self.resolve_levels(user)
        if levels is None:
            return True
        required = self.LEVEL_REQUIRED.get(level, ACCESS_LEVEL_VIEW)
        return levels.get(collection_id, ACCESS_LEVEL_NONE) >= required

    async def can_read(self, user: User, collection_id: str) -> bool:
        return await self.can_access(user, collection_id, "read")

    async def can_create(self, user: User, collection_id: str) -> bool:
        return await self.can_access(user, collection_id, "create")

    async def can_write(self, user: User, collection_id: str) -> bool:
        return await self.can_access(user, collection_id, "write")

    async def can_modify(self, user: User, collection_id: str) -> bool:
        return await self.can_access(user, collection_id, "modify")

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