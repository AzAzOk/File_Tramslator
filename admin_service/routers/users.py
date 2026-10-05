"""User management API (task 5.3).

Every mutation here writes an ``admin_events`` record (task 5.7). Assigning a
role sets ``manual_role=True`` so the next AD login does not overwrite it;
"reset to LDAP" clears the flag again.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status

from admin_service.deps import AdminContainer, get_container, get_current_admin
from admin_service.schemas import (
    AssignRoleRequest,
    SetActiveRequest,
    SetPermissionsRequest,
    UserOut,
)
from file_translator.domain.auth import Permission, RoleType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])

#: The `api` role is a machine-account role and is not assignable to people.
HIDDEN_ROLE = RoleType.API.value


def known_permissions() -> set[str]:
    return {p.value for p in Permission}


def apply_role(user, role_name: str) -> None:
    """Set the canonical role name and keep the legacy enum in sync.

    Custom roles are not ``RoleType`` members, so the enum field falls back to
    a safe value while ``role_name`` remains the source of truth (it is what
    ``MongoUserRepository`` persists).
    """
    user.role_name = role_name
    try:
        user.role = RoleType(role_name)
    except ValueError:
        user.role = RoleType.VIEWER


def ldap_derived_role(user) -> str:
    """Role an AD login would assign: admin group -> admin, otherwise user."""
    admin_group = os.environ.get("LDAP_GROUP_ADMIN", "")
    groups = {str(g).strip().lower() for g in (user.ldap_groups or [])}
    if admin_group and admin_group.strip().lower() in groups:
        return RoleType.ADMIN.value
    return "user"


async def find_role(container: AdminContainer, name: str):
    """Exact role lookup by name.

    ``RoleConfigStore.get_role`` deliberately falls back to the legacy
    ``operator`` role for unknown names so the main API keeps resolving
    permissions for custom roles. The admin UI needs strict semantics instead:
    "does this role exist?" must answer no for a typo.
    """
    for role in await container.role_store.get_all_roles():
        if role.name == name:
            return role
    return None


async def _load(container: AdminContainer, user_id: str):
    user = await container.user_repo.get_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"User '{user_id}' not found"
        )
    return user


def to_out(user, role_permissions: list[str]) -> UserOut:
    return UserOut(
        user_id=user.user_id,
        username=user.username,
        display_name=user.display_name,
        role=user.role_name or (user.role.value if user.role else ""),
        manual_role=bool(user.manual_role),
        is_active=bool(user.is_active),
        ldap_groups=list(user.ldap_groups or []),
        permissions=sorted(p.value for p in user.permissions),
        role_permissions=role_permissions,
        last_login_at=user.last_login_at or "",
    )


@router.get("", response_model=list[UserOut])
async def list_users(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> list[UserOut]:
    users = await container.user_repo.list_all()
    out: list[UserOut] = []
    for user in sorted(users, key=lambda u: (u.username or "").lower()):
        role = user.role_name or (user.role.value if user.role else "")
        role_permissions = sorted(await container.role_store.resolve_permissions(role)) if role else []
        out.append(to_out(user, role_permissions))
    return out


@router.post("/{user_id}/role", response_model=UserOut)
async def assign_role(
    user_id: str,
    payload: AssignRoleRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> UserOut:
    """Assign a role manually — sets ``manual_role=True``."""
    role_name = payload.role.strip()
    if role_name == HIDDEN_ROLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Role '{HIDDEN_ROLE}' cannot be assigned to users",
        )
    role = await find_role(container, role_name)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Role '{role_name}' not found"
        )

    user = await _load(container, user_id)
    previous = user.role_name
    apply_role(user, role_name)
    user.manual_role = True
    await container.user_repo.update(user)
    await container.audit(
        admin,
        "user.role_assigned",
        "user",
        user_id,
        {"from": previous, "to": role_name, "manual_role": True},
    )
    return to_out(user, role.permissions)


@router.post("/{user_id}/permissions", response_model=UserOut)
async def set_permissions(
    user_id: str,
    payload: SetPermissionsRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> UserOut:
    """Replace the per-user permission overrides."""
    valid = known_permissions()
    unknown = sorted(set(payload.permissions) - valid)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permissions: {', '.join(unknown)}",
        )

    user = await _load(container, user_id)
    user.permissions = {Permission(p) for p in payload.permissions}
    await container.user_repo.update(user)
    await container.audit(
        admin,
        "user.permissions_set",
        "user",
        user_id,
        {"permissions": sorted(payload.permissions)},
    )
    role_permissions = await container.role_store.resolve_permissions(
        user.role_name or (user.role.value if user.role else "")
    )
    return to_out(user, sorted(role_permissions))


@router.post("/{user_id}/active", response_model=UserOut)
async def set_active(
    user_id: str,
    payload: SetActiveRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> UserOut:
    """Activate or deactivate an account."""
    user = await _load(container, user_id)
    user.is_active = payload.is_active
    await container.user_repo.update(user)
    await container.audit(
        admin, "user.activation_changed", "user", user_id, {"is_active": payload.is_active}
    )
    role_permissions = await container.role_store.resolve_permissions(
        user.role_name or (user.role.value if user.role else "")
    )
    return to_out(user, sorted(role_permissions))


@router.post("/{user_id}/reset-role", response_model=UserOut)
async def reset_to_ldap_role(
    user_id: str,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> UserOut:
    """Clear the manual flag so the role follows AD group mapping again."""
    user = await _load(container, user_id)
    previous = user.role_name
    derived = ldap_derived_role(user)
    apply_role(user, derived)
    user.manual_role = False
    await container.user_repo.update(user)
    await container.audit(
        admin,
        "user.role_reset_to_ldap",
        "user",
        user_id,
        {"from": previous, "to": derived, "manual_role": False},
    )
    role = await find_role(container, derived)
    return to_out(user, role.permissions if role else [])
