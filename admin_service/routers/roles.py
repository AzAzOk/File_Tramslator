"""Role management API (task 5.4).

Built-in roles are protected: they can neither be modified nor deleted. A role
that still has members cannot be deleted either — the UI asks the
administrator to reassign the users first.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from admin_service.deps import AdminContainer, get_container, get_current_admin
from admin_service.routers.users import HIDDEN_ROLE, find_role, known_permissions
from admin_service.schemas import (
    RoleCreateRequest,
    RoleOut,
    RoleUpdateRequest,
    UserOut,
)
from file_translator.infrastructure.auth.role_config import RoleDoc, SUBJECT_ROLE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/roles", tags=["roles"])


def _validate_permissions(permissions: list[str]) -> list[str]:
    valid = known_permissions()
    unknown = sorted(set(permissions) - valid)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permissions: {', '.join(unknown)}",
        )
    # Preserve pool order, drop duplicates.
    return sorted(set(permissions))


async def _member_counts(container: AdminContainer) -> dict[str, int]:
    counts: dict[str, int] = {}
    for user in await container.user_repo.list_all():
        role = user.role_name or (user.role.value if user.role else "")
        if role:
            counts[role] = counts.get(role, 0) + 1
    return counts


def _to_out(role: RoleDoc, member_count: int = 0) -> RoleOut:
    return RoleOut(
        name=role.name,
        description=role.description,
        builtin=role.builtin,
        protected=role.protected,
        permissions=list(role.permissions),
        grants=list(role.grants),
        member_count=member_count,
    )


@router.get("", response_model=list[RoleOut])
async def list_roles(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> list[RoleOut]:
    """All roles except the machine-account ``api`` role, which the UI hides."""
    roles = await container.role_store.get_all_roles()
    counts = await _member_counts(container)
    visible = [r for r in roles if r.name != HIDDEN_ROLE]
    return [_to_out(r, counts.get(r.name, 0)) for r in sorted(visible, key=lambda r: r.name)]


@router.post("", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreateRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> RoleOut:
    """Create a custom role with its own permission pool."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Role name is required")
    existing = await find_role(container, name)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Role '{name}' already exists"
        )
    role = RoleDoc(
        name=name,
        description=payload.description.strip(),
        builtin=False,
        protected=False,
        permissions=_validate_permissions(payload.permissions),
        grants=sorted(set(payload.grants)),
    )
    await container.role_store.create_role(role)
    await container.audit(
        admin, "role.created", "role", name, {"permissions": role.permissions, "grants": role.grants}
    )
    return _to_out(role, 0)


@router.patch("/{name}", response_model=RoleOut)
async def update_role(
    name: str,
    payload: RoleUpdateRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> RoleOut:
    """Edit a custom role's description, permission pool or grants."""
    role = await find_role(container, name)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Role '{name}' not found")
    if role.protected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Built-in role '{name}' cannot be modified",
        )

    fields: dict[str, object] = {}
    if payload.description is not None:
        fields["description"] = payload.description.strip()
    if payload.permissions is not None:
        fields["permissions"] = _validate_permissions(payload.permissions)
    if payload.grants is not None:
        fields["grants"] = sorted(set(payload.grants))
    if not fields:
        return _to_out(role, (await _member_counts(container)).get(name, 0))

    updated = await container.role_store.update_role(name, fields)
    if updated is None:  # pragma: no cover - concurrent delete
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Role '{name}' not found")
    await container.audit(admin, "role.updated", "role", name, {"fields": sorted(fields)})
    counts = await _member_counts(container)
    return _to_out(updated, counts.get(name, 0))


@router.delete("/{name}")
async def delete_role(
    name: str,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> dict[str, object]:
    """Delete a custom role. Blocked for built-ins and for roles with members."""
    role = await find_role(container, name)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Role '{name}' not found")
    if role.protected or role.builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Built-in role '{name}' cannot be deleted",
        )

    members = [
        u.user_id
        for u in await container.user_repo.list_all()
        if (u.role_name or (u.role.value if u.role else "")) == name
    ]
    if members:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Role '{name}' still has {len(members)} assigned user(s): "
                f"{', '.join(members[:5])}. Reassign them first."
            ),
        )

    await container.role_store.delete_role(name)
    removed = await container.grant_store.delete_many(subject_type=SUBJECT_ROLE, subject=name)
    await container.audit(admin, "role.deleted", "role", name, {"grants_removed": removed})
    return {"deleted": name, "grants_removed": removed}


@router.get("/permissions-pool", response_model=list[str])
async def permissions_pool(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> list[str]:
    """The permission pool a role or user override can pick from."""
    del container
    return sorted(known_permissions())


@router.get("/{name}/members", response_model=list[UserOut])
async def role_members(
    name: str,
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> list[UserOut]:
    """Users currently assigned to a role."""
    from admin_service.routers.users import to_out

    role = await find_role(container, name)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Role '{name}' not found")
    members = [
        u
        for u in await container.user_repo.list_all()
        if (u.role_name or (u.role.value if u.role else "")) == name
    ]
    return [to_out(u, role.permissions) for u in members]
