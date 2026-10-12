"""AD-group → glossary-collection grants.

A glossary collection whose id matches an AD group name (``DTD`` ↔ ``dtd``) is
meant to be readable by everyone in that group. That is expressed as a real
``group`` grant document at level 1, so the runtime resolver enforces it.

The rule is applied eagerly from three triggers:

- at startup, over the AD groups of every stored user (so users that already
  existed before the feature are backfilled);
- on every successful AD login, for the login's AD groups;
- on a periodic sweep, so a collection created after boot is picked up.

A ``(group, collection)`` pair that already has a grant document is never
touched — including a document an administrator set to level 0 (a real denial)
or raised above view — so administrator decisions survive later logins and
sweeps. Only missing documents are created, always at level 1.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable

from file_translator.infrastructure.auth.role_config import (
    ACCESS_LEVEL_VIEW,
    DEFAULT_COLLECTION,
    GrantDoc,
    GrantStore,
    SUBJECT_GROUP,
)

logger = logging.getLogger(__name__)

CollectionLister = Callable[[], Awaitable[list[str]]]


def to_collection_id(table: str) -> str:
    """Map a MySQL glossary table name to its collection id.

    ``glossary`` is the ``default`` collection; ``glossary_<id>`` is ``<id>``.
    Same mapping as ``admin_service/deps.py``.
    """

    if table == "glossary":
        return DEFAULT_COLLECTION
    if table.startswith("glossary_"):
        return table[len("glossary_"):]
    return table


def build_mysql_collection_lister(repository: Any | None = None) -> CollectionLister:
    """Build the async collection-id lister from the glossary tables.

    Args:
        repository: optional object exposing ``list_tables(pattern)``; a
            ``MySQLGlossaryRepository`` is built when omitted.
    """

    if repository is None:
        from file_translator.infrastructure.repositories.mysql_glossary_repository import (
            MySQLGlossaryRepository,
        )

        repository = MySQLGlossaryRepository()

    async def _list() -> list[str]:
        tables = await repository.list_tables("glossary%")
        return [to_collection_id(t) for t in tables]

    return _list


class GroupGrantProvisioner:
    """Grants view access for collections named after an AD group.

    Args:
        list_collection_ids: async callable returning the existing collection ids.
        grant_store: the :class:`GrantStore` used to read and write grants.
    """

    def __init__(self, list_collection_ids: CollectionLister, grant_store: GrantStore) -> None:
        self._list_collection_ids = list_collection_ids
        self._grant_store = grant_store

    async def provision_groups(self, groups: "Iterable[str]") -> int:
        """Create level-1 grants for collections matching ``groups``.

        Returns the number of grants created. Any ``(group, collection)`` pair
        that already has a grant is skipped, so an existing grant — even one an
        administrator set to level 0 — is never created, lowered or raised.
        """
        unique = [g for g in dict.fromkeys(groups) if g]
        if not unique:
            return 0

        collections = await self._list_collection_ids()
        if not collections:
            return 0

        by_folded = {c.casefold(): c for c in collections if c}
        existing = {
            (g.subject_type, g.subject, g.collection)
            for g in await self._grant_store.get_all_grants()
        }

        created = 0
        for group in unique:
            collection = by_folded.get(group.casefold())
            if collection is None:
                continue
            key = (SUBJECT_GROUP, group, collection)
            if key in existing:
                continue
            await self._grant_store.upsert_grant(
                GrantDoc(
                    subject_type=SUBJECT_GROUP,
                    subject=group,
                    collection=collection,
                    level=ACCESS_LEVEL_VIEW,
                )
            )
            existing.add(key)
            created += 1

        if created:
            logger.info(f"Provisioned {created} AD-group collection grant(s)")
        return created

    async def provision(self, user: Any) -> int:
        """Provision from one user's AD groups (used on every login)."""
        return await self.provision_groups(getattr(user, "ldap_groups", None) or [])

    async def provision_all_users(self, user_repository: Any) -> int:
        """Provision from every stored user's AD groups (startup / periodic)."""
        users = await user_repository.list_all()
        groups = [g for user in users for g in (getattr(user, "ldap_groups", None) or [])]
        return await self.provision_groups(groups)
