"""Authentication middleware — validates JWT tokens on protected endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from file_translator.domain.auth import AuthCredentials, RoleType, User

logger = logging.getLogger(__name__)

PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/refresh",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path

        if path in PUBLIC_PATHS or path.startswith(("/docs/", "/redoc/", "/openapi")):
            request.state.auth = AuthCredentials(
                user=User(user_id="anonymous", username="anonymous",
                          role=RoleType.VIEWER, is_active=False),
            )
            return await call_next(request)

        if path.startswith(("/static/", "/assets/")):
            request.state.auth = AuthCredentials(
                user=User(user_id="anonymous", username="anonymous",
                          role=RoleType.VIEWER, is_active=False),
            )
            return await call_next(request)

        auth_service = getattr(request.app.state, "auth_service", None)
        if not auth_service:
            logger.critical("auth_service not initialized in app.state")
            raise HTTPException(status_code=500, detail="Auth service unavailable")

        authorization = request.headers.get("Authorization")

        credentials = await auth_service.authenticate_request(authorization)
        request.state.auth = credentials

        return await call_next(request)
