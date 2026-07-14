"""JWT-based authentication provider with Redis-backed token blacklist."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from jose import JWTError, jwt
import bcrypt as _bcrypt

from file_translator.domain.auth import ApiKey, AuthCredentials, AuthToken, RoleType, User
from file_translator.domain.interfaces import AuthProvider, UserRepository

logger = logging.getLogger(__name__)


class JwtAuthProvider(AuthProvider):
    ACCESS_EXPIRE_MINUTES = 30
    REFRESH_EXPIRE_DAYS = 7
    ALGORITHM = "HS256"

    def __init__(self, secret_key: str, user_repository: UserRepository, 
                 token_blacklist: Optional[Any] = None):
        self._secret_key = secret_key
        self._user_repo = user_repository
        self._token_blacklist = token_blacklist  # Redis-backed blacklist (optional)
        self._blacklisted_jtis: dict[str, float] = {}  # Fallback in-memory blacklist

    def create_access_token(self, user_id: str, username: str,
                            role: str) -> str:
        expires = datetime.now(timezone.utc) + timedelta(minutes=self.ACCESS_EXPIRE_MINUTES)
        payload = {
            "sub": user_id,
            "username": username,
            "role": role,
            "type": "access",
            "jti": str(uuid.uuid4()),
            "exp": expires,
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, self._secret_key, algorithm=self.ALGORITHM)

    def create_refresh_token(self, user_id: str) -> str:
        expires = datetime.now(timezone.utc) + timedelta(days=self.REFRESH_EXPIRE_DAYS)
        payload = {
            "sub": user_id,
            "type": "refresh",
            "exp": expires,
            "iat": datetime.now(timezone.utc),
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(payload, self._secret_key, algorithm=self.ALGORITHM)

    def decode_token(self, token: str) -> dict[str, Any] | None:
        try:
            return jwt.decode(token, self._secret_key, algorithms=[self.ALGORITHM])
        except JWTError as e:
            logger.warning(f"Token decode failed: {e}")
            return None

    async def blacklist_token(self, jti: str, expires_at: float) -> None:
        """Add a token's JTI to the blacklist until its expiry."""
        if self._token_blacklist:
            # Use Redis-backed blacklist
            await self._token_blacklist.blacklist(jti, expires_at)
        else:
            # Fallback to in-memory (for backwards compatibility)
            self._blacklisted_jtis[jti] = expires_at
            logger.debug(f"Token {jti[:8]}... blacklisted in-memory until {datetime.fromtimestamp(expires_at)}")

    async def _is_blacklisted(self, jti: str) -> bool:
        """Check if a JTI is blacklisted."""
        if self._token_blacklist:
            # Use Redis-backed blacklist
            return await self._token_blacklist.is_blacklisted(jti)
        else:
            # Fallback to in-memory
            return jti in self._blacklisted_jtis

    async def authenticate(self, token: str, method: str = "bearer") -> AuthCredentials | None:
        payload = self.decode_token(token)
        if not payload or payload.get("type") != "access":
            return None

        # Check blacklist
        jti = payload.get("jti")
        if jti and await self._is_blacklisted(jti):
            logger.warning(f"Token {jti[:8]}... is blacklisted")
            return None

        user_id = payload.get("sub")
        if not user_id:
            return None

        user = await self._user_repo.get_by_id(user_id)
        if not user or not user.is_active:
            return None

        iat = payload.get("iat")
        exp = payload.get("exp")
        return AuthCredentials(
            user=user,
            token=AuthToken(
                token=token,
                token_type="bearer",
                user_id=user_id,
                username=payload.get("username", ""),
                role=payload.get("role", ""),
                issued_at=datetime.fromtimestamp(iat).isoformat() if iat else "",
                expires_at=datetime.fromtimestamp(exp).isoformat() if exp else "",
            ),
        )

    async def create_token(self, user_id: str, expires_in: int = 3600) -> AuthToken:
        user = await self._user_repo.get_by_id(user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")

        token_str = self.create_access_token(
            user_id=user_id,
            username=user.username,
            role=user.role.value if user.role else "viewer",
        )
        expires = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        return AuthToken(
            token=token_str,
            token_type="bearer",
            user_id=user_id,
            username=user.username,
            role=user.role.value if user.role else "viewer",
            issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=expires.isoformat(),
        )

    async def validate_api_key(self, key: str) -> User | None:
        return None

    @staticmethod
    def hash_password(password: str) -> str:
        return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def verify_password(password: str, hash_value: str) -> bool:
        return _bcrypt.checkpw(password.encode("utf-8"), hash_value.encode("utf-8"))
