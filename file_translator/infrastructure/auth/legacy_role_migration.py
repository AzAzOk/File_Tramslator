"""Legacy role migration.

Pre-2.x user documents carried only the legacy ``RoleType`` strings
(``operator``, ``viewer``, ``api``). With runtime-managed roles the standard
person role is ``user``; this migration reassigns ``operator``/``viewer`` to
``user`` and leaves ``api`` untouched. It also stamps ``manual_role=false`` on
every migrated/existing user so AD logins can keep deriving the role until an
administrator explicitly assigns one.
"""

from __future__ import annotations

import logging
from typing import Any

from file_translator.domain.auth import RoleType
from file_translator.infrastructure.auth.role_config import ROLE_USER

logger = logging.getLogger(__name__)

# Legacy role names that are collapsed into the standard "user" role.
_LEGACY_TO_USER = (RoleType.OPERATOR.value, RoleType.VIEWER.value)


def canonical_role_name(user: Any) -> str:
    """The role name as persisted in Mongo (``users.role``).

    ``role_name`` is the canonical field (``_user_to_doc`` writes it), the enum
    attribute is only a compatibility view.
    """
    name = getattr(user, "role_name", "") or ""
    if name:
        return str(name)
    role = getattr(user, "role", None)
    return role.value if role else ""


async def migrate_legacy_roles(user_repository: Any) -> dict[str, int]:
    """Reassign legacy roles and stamp ``manual_role=false``.

    Idempotent: users already on ``user``/``admin``/custom roles are only
    updated when their ``manual_role`` flag needs stamping. ``api`` users are
    never reassigned.

    Args:
        user_repository: any UserRepository implementation.

    Returns:
        A summary dict: ``{"migrated": N, "stamped": M, "api_preserved": K}``
    """
    users = await user_repository.list_all()
    migrated = 0
    stamped = 0
    api_preserved = 0

    for user in users:
        role_name = canonical_role_name(user)
        needs_reassign = role_name in _LEGACY_TO_USER
        needs_stamp = not bool(getattr(user, "manual_role", False))

        if role_name == RoleType.API.value:
            api_preserved += 1

        if not needs_reassign and not needs_stamp:
            continue

        # `role_name` is the field Mongo persists, so the reassignment must be
        # written there as well as on the enum attribute.
        if needs_reassign:
            user.role_name = ROLE_USER
            user.role = RoleType.USER
            migrated += 1
        if needs_stamp:
            user.manual_role = False
            stamped += 1
        await user_repository.update(user)

    if migrated or stamped:
        logger.info(
            f"Legacy role migration: {migrated} user(s) reassigned to '{ROLE_USER}', "
            f"{stamped} user(s) stamped manual_role=false, "
            f"{api_preserved} api user(s) preserved"
        )
    return {"migrated": migrated, "stamped": stamped, "api_preserved": api_preserved}