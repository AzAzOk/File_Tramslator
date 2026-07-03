"""Stub user repository - In-memory implementation for development."""

from __future__ import annotations

from typing import Any

from file_translator.domain.auth import RoleType, User
from file_translator.domain.interfaces import UserRepository


class StubUserRepository(UserRepository):
    """In-memory user storage with a default admin user.
    
    Default credentials:
        username: admin
        password: admin
        role: admin
    """
    
    def __init__(self):
        self._users: dict[str, User] = {}
        self._by_username: dict[str, User] = {}
        self._init_default_users()
    
    def _init_default_users(self) -> None:
        """Create default users for development."""
        import hashlib
        
        admin = User(
            user_id="admin-001",
            username="admin",
            display_name="Administrator",
            role=RoleType.ADMIN,
            is_active=True,
            created_at="2026-01-01T00:00:00+00:00",
        )
        admin.password_hash = hashlib.sha256(b"admin").hexdigest()
        self._users[admin.user_id] = admin
        self._by_username[admin.username] = admin
        
        operator = User(
            user_id="operator-001",
            username="operator",
            display_name="Operator",
            role=RoleType.OPERATOR,
            is_active=True,
            created_at="2026-01-01T00:00:00+00:00",
        )
        operator.password_hash = hashlib.sha256(b"operator").hexdigest()
        self._users[operator.user_id] = operator
        self._by_username[operator.username] = operator
    
    async def get_by_id(self, user_id: str) -> Any | None:
        return self._users.get(user_id)
    
    async def get_by_username(self, username: str) -> Any | None:
        return self._by_username.get(username)
    
    async def create(self, user: Any) -> Any:
        self._users[user.user_id] = user
        self._by_username[user.username] = user
        return user
    
    async def update(self, user: Any) -> Any | None:
        if user.user_id in self._users:
            old = self._users[user.user_id]
            if old.username != user.username and old.username in self._by_username:
                del self._by_username[old.username]
            self._users[user.user_id] = user
            self._by_username[user.username] = user
            return user
        return None
    
    async def delete(self, user_id: str) -> bool:
        user = self._users.get(user_id)
        if user:
            del self._users[user_id]
            if user.username in self._by_username:
                del self._by_username[user.username]
            return True
        return False
    
    async def list_all(self) -> list[Any]:
        return list(self._users.values())
