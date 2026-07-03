"""FastAPI dependency injectors for auth and service access."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status

from file_translator.application.auth_service import AuthService
from file_translator.application.service import TranslationService
from file_translator.domain.auth import AuthCredentials, Permission


def get_translation_service() -> TranslationService:
    """Get the singleton TranslationService instance."""
    from file_translator.presentation.api.app import translation_service
    return translation_service


def get_auth_service() -> AuthService:
    """Get the AuthService instance from app state (or fallback to stub)."""
    from file_translator.presentation.api.app import app as _app
    if hasattr(_app.state, "auth_service"):
        return _app.state.auth_service
    if not hasattr(get_auth_service, "_instance"):
        get_auth_service._instance = AuthService()
    return get_auth_service._instance


async def get_current_user(request: Request) -> AuthCredentials:
    """Extract authenticated user from request state.
    
    Set by AuthMiddleware. Raises 401 if not authenticated.
    """
    credentials: AuthCredentials | None = getattr(request.state, "auth", None)
    if not credentials or not credentials.is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials


def require_permission(permission: Permission) -> Any:
    """Dependency factory that requires a specific permission.
    
    Usage:
        @app.get("/glossary")
        async def list_glossary(user: AuthCredentials = Depends(require_permission(Permission.VIEW_GLOSSARY))):
            ...
    """
    async def _check_permission(
        credentials: AuthCredentials = Depends(get_current_user),
        auth_service: AuthService = Depends(get_auth_service),
    ) -> AuthCredentials:
        if not auth_service.check_permission(credentials, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission.value}",
            )
        return credentials
    
    return _check_permission
