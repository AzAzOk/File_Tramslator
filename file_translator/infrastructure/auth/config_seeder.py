"""Idempotent startup seeding shared by the main API and the admin service.

Seeds:

1. The three built-in roles (``admin``, ``user``, ``api``) when they are
   missing from MongoDB.
2. Collection grants from ``GLOSSARY_COLLECTION_MAP`` (read+write for each
   mapped AD group) only when the grants collection is completely empty, plus
   a write grant on ``default`` for the built-in ``admin`` role.
3. Runs the legacy role migration (``operator``/``viewer`` → ``user``).

After the first successful seed the environment variable is never consulted
again (task 6.2); grants are managed exclusively through the admin service.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from file_translator.infrastructure.auth.legacy_role_migration import migrate_legacy_roles
from file_translator.infrastructure.auth.role_config import (
    BUILTIN_ROLE_NAMES,
    DEFAULT_COLLECTION,
    DEFAULT_ROLES,
    GrantDoc,
    RoleDoc,
    SUBJECT_GROUP,
    SUBJECT_ROLE,
)

logger = logging.getLogger(__name__)


def load_collection_map_from_env() -> dict[str, list[str]]:
    """Parse the GLOSSARY_COLLECTION_MAP env var (used only for the initial seed)."""
    raw = os.getenv("GLOSSARY_COLLECTION_MAP", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            logger.warning("GLOSSARY_COLLECTION_MAP is not a JSON object — ignoring")
            return {}
        return {
            str(k): list(v) if isinstance(v, list) else [str(v)]
            for k, v in parsed.items()
        }
    except json.JSONDecodeError as e:
        logger.warning(f"GLOSSARY_COLLECTION_MAP parse error: {e}")
        return {}


async def seed_roles(role_repository: Any) -> int:
    """Insert missing built-in roles. Returns the number of roles created."""
    await role_repository.ensure_indexes()
    existing_names = {r.name for r in await role_repository.find_all()}
    created = 0
    for name in BUILTIN_ROLE_NAMES:
        if name not in existing_names:
            role = RoleDoc.from_dict(DEFAULT_ROLES[name])
            if role is not None:
                await role_repository.create(role)
                created += 1
                logger.info(f"Seeded built-in role '{name}'")
    return created


async def seed_grants(grant_repository: Any, collection_map: dict[str, list[str]] | None = None) -> int:
    """Seed grants from the env map (only when the grants store is empty).

    Returns the number of grants created. Does nothing when grants exist.
    """
    await grant_repository.ensure_indexes()
    if await grant_repository.count() > 0:
        return 0
    map_data = collection_map if collection_map is not None else load_collection_map_from_env()
    grants: list[GrantDoc] = []
    for group_name, collections in map_data.items():
        for cid in collections:
            grants.append(
                GrantDoc(subject_type=SUBJECT_GROUP, subject=group_name, collection=cid, read=True, write=True)
            )
    # The built-in admin role gets explicit write on `default` (D2 seed).
    grants.append(
        GrantDoc(subject_type=SUBJECT_ROLE, subject="admin", collection=DEFAULT_COLLECTION, read=True, write=True)
    )
    for grant in grants:
        await grant_repository.upsert(grant)
    logger.info(f"Seeded {len(grants)} collection grant(s) from GLOSSARY_COLLECTION_MAP")
    return len(grants)


async def run_startup_seeding(
    role_repository: Any,
    grant_repository: Any,
    user_repository: Any,
    collection_map: dict[str, list[str]] | None = None,
) -> dict[str, int]:
    """Run the full idempotent startup seed.

    Safe to call from both the main API and the admin service on every startup.
    """
    roles_created = await seed_roles(role_repository)
    grants_created = await seed_grants(grant_repository, collection_map)
    migration = await migrate_legacy_roles(user_repository)
    summary = {
        "roles_created": roles_created,
        "grants_created": grants_created,
        "legacy_migrated": migration.get("migrated", 0),
        "legacy_stamped": migration.get("stamped", 0),
        "api_preserved": migration.get("api_preserved", 0),
    }
    if any(summary.values()):
        logger.info(f"Startup seeding summary: {summary}")
    return summary