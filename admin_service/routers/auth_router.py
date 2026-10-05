"""Single-account admin login with session-cookie auth (tasks 5.1, 5.6)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from admin_service.config import SESSION_COOKIE
from admin_service.deps import AdminContainer, get_container, get_current_admin
from admin_service.schemas import LoginRequest, SessionInfo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=SessionInfo)
async def login(
    payload: LoginRequest,
    response: Response,
    container: AdminContainer = Depends(get_container),
) -> SessionInfo:
    """Authenticate the single admin account and open a session cookie."""
    username_ok = payload.username == container.config.username
    password_ok = await container.passwords.verify(payload.password)
    if not (username_ok and password_ok):
        logger.warning(f"Rejected admin login attempt for '{payload.username}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    token = container.sessions.issue(payload.username)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=container.sessions._ttl_hours * 3600,
        path="/",
    )
    logger.info(f"Admin session opened for '{payload.username}'")
    await container.audit(payload.username, "auth.login", "admin", payload.username)
    return SessionInfo(authenticated=True, username=payload.username)


@router.post("/logout", response_model=SessionInfo)
async def logout(
    response: Response,
    admin: str = Depends(get_current_admin),
    container: AdminContainer = Depends(get_container),
) -> SessionInfo:
    response.delete_cookie(SESSION_COOKIE, path="/")
    await container.audit(admin, "auth.logout", "admin", admin)
    return SessionInfo(authenticated=False, username="")


@router.get("/session", response_model=SessionInfo)
async def session_info(request: Request, container: AdminContainer = Depends(get_container)) -> SessionInfo:
    """Report whether the current browser session is authenticated."""
    token = request.cookies.get(SESSION_COOKIE, "")
    username = container.sessions.verify(token) if token else None
    return SessionInfo(authenticated=bool(username), username=username or "")
