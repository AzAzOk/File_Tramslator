"""Collection access matrix API (task 5.5).

Serves the subject x collection matrix (including ``default``) and upserts or
removes individual grants. Every toggle bumps ``admin_config_version`` through
the store, so the main API picks the change up without a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, status

from admin_service.deps import (
    AdminContainer,
    get_container,
    get_current_admin,
    known_collections,
)
from admin_service.routers.users import HIDDEN_ROLE, find_role
from admin_service.schemas import (
    GrantRequest,
    GroupDeleteRequest,
    MatrixOut,
    MatrixRow,
)
from file_translator.domain.auth import RoleType
from file_translator.infrastructure.auth.role_config import (
    ACCESS_LEVEL_VIEW,
    SUBJECT_GROUP,
    SUBJECT_ROLE,
    SUBJECT_TYPES,
    SUBJECT_USER,
    GrantDoc,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/access", tags=["access"])


def _group_subject_visible(
    group: str,
    collection_ids: list[str],
    grants: list[GrantDoc],
) -> bool:
    """Whether a group subject may appear in the matrix.

    The group is shown when its name matches a glossary collection id
    (case-insensitively) or when a group grant exists for it, compared
    case-insensitively on the subject name. A level-0 grant still counts, so an
    explicit denial stays visible (design D1).
    """
    folded = group.casefold()
    if any(collection.casefold() == folded for collection in collection_ids):
        return True
    return any(
        g.subject_type == SUBJECT_GROUP and g.subject.casefold() == folded
        for g in grants
    )


def _iter_known_group_names(
    users: Sequence[object],
    grants: list[GrantDoc],
) -> dict[str, str]:
    """Casefold-deduped union of every user's LDAP groups and group grant
    subjects, mapping fold -> canonical first-seen name."""
    canonical: dict[str, str] = {}
    for user in users:
        for group in getattr(user, "ldap_groups", None) or []:
            canonical.setdefault(group.casefold(), group)
    for grant in grants:
        if grant.subject_type == SUBJECT_GROUP:
            canonical.setdefault(grant.subject.casefold(), grant.subject)
    return canonical


def _group_aware_cell_flags(
    grants: list[GrantDoc],
) -> dict[tuple[str, str, str], dict[str, bool | int]]:
    """Per (subject_type, folded-subject, collection) cell, MAX across grants.

    Group subjects are folded so a differently-cased grant still fills the cell,
    and the level shown is the maximum, which is exactly what access enforcement
    would apply to the group's members (design D3).
    """
    flags: dict[tuple[str, str, str], dict[str, bool | int]] = {}
    for grant in grants:
        subj = grant.subject.casefold() if grant.subject_type == SUBJECT_GROUP else grant.subject
        key = (grant.subject_type, subj, grant.collection)
        entry: dict[str, bool | int] = {
            "read": grant.read,
            "write": grant.write,
            "level": grant.level or 0,
        }
        previous = flags.get(key)
        if previous is None or int(entry["level"]) > int(previous["level"]):
            flags[key] = entry
    return flags


def _group_matching_grants(grants: list[GrantDoc], group: str) -> list[GrantDoc]:
    """Every group grant whose subject matches ``group`` case-insensitively.

    Both spellings of a deduplicated row are returned, so deleting a group
    clears the full set (design D6).
    """
    folded = group.casefold()
    return [
        g
        for g in grants
        if g.subject_type == SUBJECT_GROUP and g.subject.casefold() == folded
    ]


def _group_has_effective_rights(grants: list[GrantDoc], group: str) -> bool:
    """Whether deleting ``group`` would silently revoke rights in use.

    True when any matching grant holds level >= ``ACCESS_LEVEL_VIEW`` on any
    collection; level-0 documents (explicit denials) grant nothing and do not
    block deletion (design D6).
    """
    return any(
        int(g.level or 0) >= ACCESS_LEVEL_VIEW
        for g in _group_matching_grants(grants, group)
    )


@router.get("/collections", response_model=list[str])
async def list_collections(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> list[str]:
    grants = await container.grant_store.get_all_grants()
    return await known_collections(container, grants)


@router.get("/groups", response_model=list[str])
async def list_groups(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> list[str]:
    """Knowable group names for the add-control autocomplete.

    AD group names are case-insensitive-unique, so two spellings that differ
    only in case collapse to one entry (design D3).
    """
    users = await container.user_repo.list_all()
    grants = await container.grant_store.get_all_grants()
    return sorted(_iter_known_group_names(users, grants).values())


@router.delete("/groups")
async def delete_group(
    payload: GroupDeleteRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> dict[str, object]:
    """Retire a group: delete every grant document it holds.

    Refuses the whole operation (HTTP 409, nothing deleted) while any matching
    grant still carries an effective level, so a deletion can never silently
    revoke access somebody is using; level-0 denials are removed with the group.
    """
    grants = await container.grant_store.get_all_grants()
    targets = _group_matching_grants(grants, payload.subject)
    if _group_has_effective_rights(grants, payload.subject):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Group '{payload.subject}' still grants access to at least one "
                "collection; revoke or set those cells to level «—» before deleting"
            ),
        )
    removed = 0
    for grant in targets:
        removed += int(await container.grant_store.delete_grant(grant))
    await container.audit(
        admin,
        "access.group_deleted",
        "group",
        payload.subject,
        {"grants_removed": removed},
    )
    return {
        "grants_removed": removed,
        "config_version": await container.config_version.get_version(),
    }


@router.get("/matrix", response_model=MatrixOut)
async def matrix(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> MatrixOut:
    """Subject x collection matrix with read/write flags per cell."""
    grants = await container.grant_store.get_all_grants()
    collections = await known_collections(container, grants)

    admin_name = RoleType.ADMIN.value
    subjects: list[tuple[str, str, str, bool]] = []
    seen: set[tuple[str, str]] = set()
    group_seen: dict[str, str] = {}

    for role in await container.role_store.get_all_roles():
        if role.name == HIDDEN_ROLE:
            continue
        subjects.append(
            (SUBJECT_ROLE, role.name, f"role: {role.name}", role.name == admin_name)
        )
        seen.add((SUBJECT_ROLE, role.name))

    users = await container.user_repo.list_all()
    for user in users:
        key = (SUBJECT_USER, user.user_id)
        unrestricted = getattr(user, "role_name", "") == admin_name
        if key not in seen:
            subjects.append(
                (SUBJECT_USER, user.user_id, f"user: {user.username or user.user_id}", unrestricted)
            )
            seen.add(key)
        for group in user.ldap_groups or []:
            if group.casefold() in group_seen:
                continue
            if not _group_subject_visible(group, collections, grants):
                continue
            group_seen[group.casefold()] = group
            subjects.append((SUBJECT_GROUP, group, f"group: {group}", False))

    for grant in grants:
        if grant.subject_type == SUBJECT_GROUP:
            folded = grant.subject.casefold()
            if folded not in group_seen:
                group_seen[folded] = grant.subject
                subjects.append(
                    (SUBJECT_GROUP, grant.subject, f"group: {grant.subject}", False)
                )
            continue
        key = (grant.subject_type, grant.subject)
        if key not in seen:
            subjects.append(
                (
                    grant.subject_type,
                    grant.subject,
                    f"{grant.subject_type}: {grant.subject}",
                    grant.subject_type == SUBJECT_ROLE and grant.subject == admin_name,
                )
            )
            seen.add(key)

    no_cell: dict[str, bool | int] = {"read": False, "write": False, "level": 0}
    flags = _group_aware_cell_flags(grants)

    rows: list[MatrixRow] = []
    for subject_type, subject, label, unrestricted in subjects:
        subj = subject.casefold() if subject_type == SUBJECT_GROUP else subject
        cells = {
            collection: dict(flags.get((subject_type, subj, collection), no_cell))
            for collection in collections
        }
        rows.append(
            MatrixRow(
                subject_type=subject_type,
                subject=subject,
                label=label,
                unrestricted=unrestricted,
                cells=cells,
            )
        )

    return MatrixOut(collections=collections, rows=rows)


@router.put("/grant")
async def upsert_grant(
    payload: GrantRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> dict[str, object]:
    """Create or update one grant. Bumps the config version via the store."""
    if payload.subject_type not in SUBJECT_TYPES:  # pragma: no cover - pydantic guards
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"subject_type must be one of {', '.join(SUBJECT_TYPES)}",
        )
    if payload.subject_type == SUBJECT_ROLE:
        role = await find_role(container, payload.subject)
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Role '{payload.subject}' not found",
            )

    grant = GrantDoc(
        subject_type=payload.subject_type,
        subject=payload.subject,
        collection=payload.collection,
        read=payload.read,
        write=payload.write,
        # ``level`` wins over the flags when both are sent (GrantDoc derives the
        # flags from it); ``None`` means a legacy flag-only request.
        level=payload.level,
    )
    await container.grant_store.upsert_grant(grant)
    await container.audit(
        admin,
        "access.grant_upserted",
        "grant",
        f"{payload.subject_type}:{payload.subject}@{payload.collection}",
        {"read": grant.read, "write": grant.write, "level": grant.level},
    )
    return {"grant": grant.to_dict(), "config_version": await container.config_version.get_version()}


@router.delete("/grant")
async def delete_grant(
    payload: GrantRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> dict[str, object]:
    """Remove one grant entirely."""
    removed = await container.grant_store.delete_grant(
        GrantDoc(
            subject_type=payload.subject_type,
            subject=payload.subject,
            collection=payload.collection,
        )
    )
    await container.audit(
        admin,
        "access.grant_deleted",
        "grant",
        f"{payload.subject_type}:{payload.subject}@{payload.collection}",
    )
    return {
        "deleted": removed,
        "config_version": await container.config_version.get_version(),
    }
