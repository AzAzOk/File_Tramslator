"""Settings API (task 5.6): change the admin password, show the config version.

The password is stored as a bcrypt hash in Mongo (``admin_settings``); the
``ADMIN_UI_PASSWORD`` environment value is only the bootstrap/fallback used
until an administrator changes it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from admin_service.deps import AdminContainer, get_container, get_current_admin
from admin_service.schemas import ChangePasswordRequest, SettingsOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

_MIN_PASSWORD_LENGTH = 6


@router.get("", response_model=SettingsOut)
async def get_settings(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> SettingsOut:
    users = await container.user_repo.list_all()
    roles = await container.role_store.get_all_roles()
    grants = await container.grant_store.get_all_grants()
    stored = await container.passwords.stored_hash()
    return SettingsOut(
        admin_username=container.config.username,
        config_version=await container.config_version.get_version() or 0,
        password_source="mongo" if stored else "env",
        counts={
            "users": len(users),
            "roles": len(roles),
            "grants": len(grants),
            "manual_roles": sum(1 for u in users if u.manual_role),
            "active_users": sum(1 for u in users if u.is_active),
        },
    )


@router.post("/password")
async def change_password(
    payload: ChangePasswordRequest,
    container: AdminContainer = Depends(get_container),
    admin: str = Depends(get_current_admin),
) -> dict[str, object]:
    """Persist a new admin password as a bcrypt hash in Mongo."""
    if len(payload.new_password) < _MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"New password must be at least {_MIN_PASSWORD_LENGTH} characters",
        )
    if not await container.passwords.verify(payload.current_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect"
        )
    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current one",
        )

    await container.passwords.set_password(payload.new_password)
    await container.audit(admin, "settings.password_changed", "admin", admin)
    return {"changed": True, "password_source": "mongo"}
