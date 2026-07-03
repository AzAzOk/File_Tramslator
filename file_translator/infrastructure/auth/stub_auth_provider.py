"""Stub authentication provider for development/testing.

Issues unsigned tokens and validates them in-memory.
Not suitable for production — use JWT with proper signing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from file_translator.domain.auth import AuthCredentials, AuthToken, User
from file_translator.domain.interfaces import AuthProvider, UserRepository


class StubAuthProvider(AuthProvider):
    """In-memory auth provider that stores tokens in a dict.
    
    Tokens are UUIDs mapped to user IDs. No cryptographic signing.
    Falls back to UserRepository for user lookups when _user_lookup is empty,
    so that users created in the repository are immediately recognised.
    """
    
    def __init__(self, user_repository: UserRepository | None = None):
        self._tokens: dict[str, str] = {}  # token -> user_id
        self._user_lookup: dict[str, User] = {}  # user_id -> User
        self._api_keys: dict[str, str] = {}  # key_hash -> user_id
        self._user_repository = user_repository
    
    def register_user(self, user: User) -> None:
        """Register a user for token validation lookups."""
        self._user_lookup[user.user_id] = user
    
    def register_api_key(self, key_hash: str, user_id: str) -> None:
        """Register an API key for a user."""
        self._api_keys[key_hash] = user_id
    
    async def _resolve_user(self, user_id: str) -> User | None:
        """Resolve a user from lookup cache or repository fallback."""
        user = self._user_lookup.get(user_id)
        if user:
            return user
        if self._user_repository:
            return await self._user_repository.get_by_id(user_id)
        return None

    async def authenticate(self, token: str, method: str = "bearer") -> Any:
        """Validate a bearer token and return AuthCredentials."""
        user_id = self._tokens.get(token)
        if not user_id:
            return None
        
        user = await self._resolve_user(user_id)
        if not user or not user.is_active:
            return None
        
        token_obj = AuthToken(
            token=token,
            token_type=method,
            user_id=user.user_id,
            username=user.username,
            role=user.role.value,
            issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        
        return AuthCredentials(user=user, token=token_obj)
    
    async def create_token(self, user_id: str, expires_in: int = 3600) -> AuthToken:
        """Create a new token for the given user."""
        token_str = str(uuid.uuid4())
        self._tokens[token_str] = user_id
        
        user = await self._resolve_user(user_id)
        return AuthToken(
            token=token_str,
            token_type="bearer",
            user_id=user_id,
            username=user.username if user else "",
            role=user.role.value if user else "",
            issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(),
        )
    
    async def validate_api_key(self, key: str) -> Any:
        """Validate an API key and return associated user."""
        import hashlib
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        user_id = self._api_keys.get(key_hash)
        if not user_id:
            return None
        
        user = await self._resolve_user(user_id)
        if not user or not user.is_active:
            return None
        
        return AuthCredentials(user=user)
