"""Collection access matrix API (task 5.5).

Serves the subject x collection matrix (including ``default``) and upserts or
removes individual grants. Every toggle bumps ``admin_config_version`` through
the store, so the main API picks the change up without a restart.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from admin_service.deps import (
    AdminContainer,
    get_container,
    get_current_admin,
    known_collections,
)
from admin_service.routers.users import HIDDEN_ROLE, find_role
from admin_service.schemas import GrantRequest, MatrixOut, MatrixRow
from file_translator.domain.auth import RoleType
from file_translator.infrastructure.auth.role_config import (
    SUBJECT_GROUP,
    SUBJECT_ROLE,
    SUBJECT_TYPES,
    SUBJECT_USER,
    GrantDoc,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/access", tags=["access"])


@router.get("/collections", response_model=list[str])
async def list_collections(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> list[str]:
    grants = await container.grant_store.get_all_grants()
    return await known_collections(container, grants)


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
            gkey = (SUBJECT_GROUP, group)
            if gkey not in seen:
                subjects.append((SUBJECT_GROUP, group, f"group: {group}", False))
                seen.add(gkey)

    for grant in grants:
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

    no_cell: dict[str, object] = {"read": False, "write": False, "level": 0}
    flags: dict[tuple[str, str, str], dict[str, object]] = {}
    for grant in grants:
        flags[(grant.subject_type, grant.subject, grant.collection)] = {
            "read": grant.read,
            "write": grant.write,
            "level": grant.level,
        }

    rows: list[MatrixRow] = []
    for subject_type, subject, label, unrestricted in subjects:
        cells = {
            collection: dict(flags.get((subject_type, subject, collection), no_cell))
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
