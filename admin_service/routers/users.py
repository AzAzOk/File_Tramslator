"""User management API (task 5.3).

Every mutation here writes an ``admin_events`` record (task 5.7). Assigning a
role sets ``manual_role=True`` so the next AD login does not overwrite it;
"reset to LDAP" clears the flag again.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace as copy_user
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from admin_service.deps import (
    AdminContainer,
    get_container,
    get_current_admin,
    known_collections,
)
from admin_service.schemas import (
    AssignRoleRequest,
    CollectionAccessOut,
    RightsSaveResponse,
    SetActiveRequest,
    SetPermissionsRequest,
    UserAccessOut,
    UserOut,
)
from file_translator.domain.auth import Permission, RoleType
from file_translator.infrastructure.auth.glossary_access_resolver import (
    GlossaryAccessResolver,
)
from file_translator.infrastructure.auth.role_config import (
    ACCESS_LEVEL_NONE,
    SUBJECT_USER,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])

#: The `api` role is a machine-account role and is not assignable to people.
HIDDEN_ROLE = RoleType.API.value


def known_permissions() -> set[str]:
    return {p.value for p in Permission}


def normalize_stored_permissions(values: Any, *, warn: bool = True) -> list[str]:
    """Drop persisted permission keys that are no longer in the catalog.

    Stale values can live in role documents written by an older build. They are
    ignored when permissions are resolved and removed the next time the record
    is saved through the admin service; every dropped key is logged so the
    disappearance is auditable (authorization spec). Display paths pass
    ``warn=False`` so the same stale key is not logged on every read.
    """
    valid = known_permissions()
    raw = {p.value if isinstance(p, Permission) else str(p) for p in (values or [])}
    dropped = sorted(raw - valid)
    if dropped and warn:
        logger.warning("Dropping stale permission(s) on save: %s", ", ".join(dropped))
    return sorted(raw & valid)


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


async def _role_permissions(container: AdminContainer, name: str) -> list[str]:
    """Permission pool of a role, or an empty list when the name is unknown."""
    role = await find_role(container, name)
    return list(role.permissions) if role else []


async def _load(container: AdminContainer, user_id: str):
    user = await container.user_repo.get_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"User '{user_id}' not found"
        )
    return user


async def _read_back(container: AdminContainer, user_id: str):
    """Re-read the user and answer from storage, never from the written object.

    The write is confirmed by reading back: a silent no-op has to surface as a
    failure here, otherwise the client is told a role changed when nothing did.
    """
    stored = await container.user_repo.get_by_id(user_id)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' disappeared while saving",
        )
    return stored


def _resolver(container: AdminContainer) -> GlossaryAccessResolver:
    """The admin service resolves levels with the same object the API enforces.

    Sharing the stores (and therefore the caches) is what makes an offer made
    here a claim the running service will honour.
    """
    return GlossaryAccessResolver(
        role_store=container.role_store,
        grant_store=container.grant_store,
    )


def _as_role(user, role_name: str):
    """The same user under a different role, exactly as ``assign_role`` would
    leave them — including the enum fallback for a custom role name."""
    clone = copy_user(user, role_name=role_name)
    try:
        clone.role = RoleType(role_name)
    except ValueError:
        clone.role = RoleType.VIEWER
    return clone


async def matching_roles(container: AdminContainer, user, desired: set[str]) -> list[str]:
    """Roles that would reproduce the saved rights *and* the saved levels.

    Both conditions are required (design D3): rights alone would promise
    "nothing changes" while the person's per-collection access moved. The
    comparison uses the state *after* the save, and the candidate state is the
    role plus AD groups with no personal deviations — which is exactly what
    accepting the offer produces.
    """
    current_role = user.role_name or (user.role.value if user.role else "")
    valid = known_permissions()
    role_pool = set(await container.role_store.resolve_permissions(current_role)) & valid
    if desired == role_pool and not await _personal_grants(container, user):
        # Nothing to trade away: the effective state already is the role's.
        return []

    resolver = _resolver(container)
    current_levels = await resolver.resolve_levels(user)
    if current_levels is None:
        # Already unrestricted — no other role could match it.
        return []

    matches: list[str] = []
    for role in await container.role_store.get_all_roles():
        if role.name in (current_role, HIDDEN_ROLE):
            continue
        pool = {p for p in role.permissions if p in valid}
        if pool != desired:
            continue
        candidate = await resolver.resolve_levels(
            _as_role(user, role.name), include_personal=False
        )
        if candidate == current_levels:
            matches.append(role.name)
    return sorted(matches)


async def _personal_grants(container: AdminContainer, user) -> list:
    return await container.grant_store.find_grants_for_subjects(
        {SUBJECT_USER: [user.user_id] if user.user_id else []}
    )


def to_out(user, role_permissions: list[str]) -> UserOut:
    raised = {p.value for p in getattr(user, "permissions", set())}
    lowered = {p.value for p in getattr(user, "denied", set())}
    valid = known_permissions()
    # Stale role keys must not surface in the dialog (authorization spec).
    role_permissions = sorted({p for p in role_permissions if p in valid})
    role_pool = set(role_permissions)
    return UserOut(
        user_id=user.user_id,
        username=user.username,
        display_name=user.display_name,
        role=user.role_name or (user.role.value if user.role else ""),
        manual_role=bool(user.manual_role),
        is_active=bool(user.is_active),
        ldap_groups=list(user.ldap_groups or []),
        permissions=sorted(raised),
        role_permissions=role_permissions,
        # Same formula as the runtime's effective permissions, so what the
        # dialog shows is what the API will enforce.
        denied=sorted(lowered),
        effective=sorted((role_pool - lowered) | raised),
        last_login_at=user.last_login_at or "",
        # What the role would become if it were handed back to AD — the UI
        # names it in the reset confirmation instead of asking for it again.
        ldap_role=ldap_derived_role(user),
    )


async def _stored_out(container: AdminContainer, user_id: str, role_permissions: list[str]):
    return to_out(await _read_back(container, user_id), role_permissions)


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
    had_raised = len(user.permissions)
    had_lowered = len(getattr(user, "denied", set()))
    apply_role(user, role_name)
    user.manual_role = True
    if payload.clear_deviations:
        user.permissions = set()
        user.denied = set()
    await container.user_repo.update(user)

    cleared_grants = 0
    if payload.clear_deviations:
        # The offer is "nothing about access changes", so it must remove the
        # personal collection levels too — rights alone are only half of it.
        cleared_grants = await container.grant_store.delete_many(
            subject_type=SUBJECT_USER, subject=user_id
        )

    stored = await _stored_out(container, user_id, role.permissions)
    if stored.role != role_name:
        # The write was accepted but the stored role is not the requested one.
        # Reporting success here is exactly what made this invisible before.
        logger.warning(
            f"Role assignment did not persist for {user_id}: requested '{role_name}', "
            f"stored '{stored.role}'"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Роль «{role_name}» не сохранилась",
        )

    details: dict[str, object] = {
        "from": previous,
        "to": role_name,
        "manual_role": True,
    }
    if payload.clear_deviations:
        # Record what was traded away, so "accept the offer" is auditable as
        # the two halves it really is.
        details.update(
            clear_deviations=True,
            cleared_raised=had_raised,
            cleared_lowered=had_lowered,
            cleared_grants=cleared_grants,
        )
    await container.audit(admin, "user.role_assigned", "user", user_id, details)
    return stored


@router.post("/{user_id}/permissions", response_model=RightsSaveResponse)
async def set_permissions(
    user_id: str,
    payload: SetPermissionsRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> RightsSaveResponse:
    """Set the desired effective rights; store only what differs from the role.

    The payload is the state the administrator wants the user to end up with,
    not the personal grants. Deriving both sets here is what keeps a redundant
    copy of a role right out of the user document: a client that resubmits what
    it displayed writes nothing at all.
    """
    valid = known_permissions()
    unknown = sorted(set(payload.permissions) - valid)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permissions: {', '.join(unknown)}",
        )

    user = await _load(container, user_id)
    role_name = user.role_name or (user.role.value if user.role else "")
    # The registry hands out strings; the payload is validated against the enum
    # above, so both sides of the difference are the same type.
    role_values = set(await container.role_store.resolve_permissions(role_name)) & valid
    role_pool = {Permission(p) for p in role_values}
    desired = {Permission(p) for p in payload.permissions}

    raised = desired - role_pool
    lowered = role_pool - desired

    user.permissions = raised
    user.denied = lowered
    await container.user_repo.update(user)

    await container.audit(
        admin,
        "user.permissions_set",
        "user",
        user_id,
        {
            "raised": sorted(p.value for p in raised),
            "lowered": sorted(p.value for p in lowered),
            "raised_count": len(raised),
            "lowered_count": len(lowered),
        },
    )
    stored = await _read_back(container, user_id)
    suggestions = await matching_roles(container, stored, {p.value for p in desired})
    return RightsSaveResponse(
        user=to_out(stored, sorted(p.value for p in role_pool)),
        raised=len(raised),
        lowered=len(lowered),
        suggestions=suggestions,
    )


@router.post("/{user_id}/reset-rights", response_model=RightsSaveResponse)
async def reset_to_role(
    user_id: str,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> RightsSaveResponse:
    """«Вернуть к роли» — drop every personal deviation in one action."""
    user = await _load(container, user_id)
    had_raised = len(user.permissions)
    had_lowered = len(getattr(user, "denied", set()))

    user.permissions = set()
    user.denied = set()
    await container.user_repo.update(user)
    # Personal collection levels are deviations too — "Вернуть к роли" has to
    # return the person to the role's access wholesale, rights and levels.
    cleared_grants = await container.grant_store.delete_many(
        subject_type=SUBJECT_USER, subject=user_id
    )

    await container.audit(
        admin,
        "user.rights_reset_to_role",
        "user",
        user_id,
        {
            "cleared_raised": had_raised,
            "cleared_lowered": had_lowered,
            "cleared_grants": cleared_grants,
        },
    )
    role_name = user.role_name or (user.role.value if user.role else "")
    role_pool = sorted(await container.role_store.resolve_permissions(role_name))
    stored = await _stored_out(container, user_id, role_pool)
    return RightsSaveResponse(user=stored, raised=0, lowered=0)


@router.get("/{user_id}/access", response_model=UserAccessOut)
async def user_access(
    user_id: str,
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> UserAccessOut:
    """The per-collection view: every collection's level and where it comes from.

    The levels are resolved by the same object the running API enforces with,
    so the dialog cannot display a claim the enforcement would reject. An
    unrestricted (built-in admin) user is reported as such rather than as an
    empty list, which would read as "no access at all".
    """
    user = await _load(container, user_id)
    detailed = await _resolver(container).resolve_levels_with_sources(user)
    if detailed is None:
        return UserAccessOut(user_id=user_id, unrestricted=True)

    names = await known_collections(container, await container.grant_store.get_all_grants())
    collections: list[CollectionAccessOut] = []
    for collection in names:
        level, source = detailed.get(collection, (ACCESS_LEVEL_NONE, "none"))
        collections.append(
            CollectionAccessOut(collection=collection, level=level, source=source)
        )
    return UserAccessOut(user_id=user_id, collections=collections)


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

    stored = await _stored_out(container, user_id, await _role_permissions(container, derived))
    if stored.role != derived or stored.manual_role:
        logger.warning(
            f"Role reset did not persist for {user_id}: expected '{derived}', "
            f"stored '{stored.role}' (manual={stored.manual_role})"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Сброс роли к AD не сохранился",
        )

    await container.audit(
        admin,
        "user.role_reset_to_ldap",
        "user",
        user_id,
        {"from": previous, "to": derived, "manual_role": False},
    )
    return stored
