"""Authentication API endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from file_translator.application.auth_service import AuthService
from file_translator.domain.auth import AuthCredentials, RoleType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    ldap_groups: list[str] | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class MeResponse(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    ldap_groups: list[str] | None = None


class LogoutResponse(BaseModel):
    detail: str


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest,
                auth_service: AuthService = Depends(get_auth_service)):
    result = await auth_service.login(body.username, body.password)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    refresh_token = auth_service.auth_provider.create_refresh_token(
        result.user_id
    )
    await auth_service.session_repo.store_refresh_token(
        token=refresh_token,
        user_id=result.user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    # Fetch user from database to get ldap_groups (AuthToken only has user_id)
    ldap_service = auth_service.ldap_service if hasattr(auth_service, "ldap_service") else None
    groups = None
    if ldap_service:
        user = await auth_service.user_repository.get_by_username(body.username)
        if user:
            groups = getattr(user, "ldap_groups", None)
    return LoginResponse(
        access_token=result.token,
        refresh_token=refresh_token,
        expires_in=1800,
        ldap_groups=groups,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(body: RefreshRequest,
                  auth_service: AuthService = Depends(get_auth_service)):
    payload = auth_service.auth_provider.decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    consumed = await auth_service.session_repo.consume_refresh_token(
        body.refresh_token
    )
    if not consumed:
        raise HTTPException(status_code=401, detail="Refresh token already used")

    user = await auth_service.user_repository.get_by_id(user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User inactive")

    new_access = auth_service.auth_provider.create_access_token(
        user_id=user_id,
        username=user.username,
        role=user.role.value if user.role else "viewer",
    )
    new_refresh = auth_service.auth_provider.create_refresh_token(user_id)

    await auth_service.session_repo.store_refresh_token(
        token=new_refresh,
        user_id=user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    return RefreshResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=1800,
    )


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request):
    auth: AuthCredentials = request.state.auth
    if auth and auth.token:
        # Extract JTI from the access token and blacklist it
        jwt_provider = getattr(request.app.state, "jwt_provider", None)
        if jwt_provider and auth.token.token:
            payload = jwt_provider.decode_token(auth.token.token)
            if payload and payload.get("jti"):
                exp = payload.get("exp", 0)
                if exp:
                    jwt_provider.blacklist_token(payload["jti"], float(exp))
        # Also delete any stored session data
        await request.app.state.auth_service.session_repo.delete_session(
            auth.token.token
        )
    return LogoutResponse(detail="Logged out")


@router.get("/me", response_model=MeResponse)
async def me(request: Request):
    auth: AuthCredentials = request.state.auth
    if not auth or not auth.is_authenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return MeResponse(
        user_id=auth.user.user_id,
        username=auth.user.username,
        display_name=auth.user.display_name,
        role=auth.user.role.value if auth.user.role else "viewer",
        is_active=auth.user.is_active,
        ldap_groups=getattr(auth.user, "ldap_groups", None),
    )
