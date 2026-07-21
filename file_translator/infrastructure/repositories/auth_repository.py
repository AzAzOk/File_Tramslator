"""MongoDB-based user and session repository."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from file_translator.domain.auth import ApiKey, RoleType, User
from file_translator.domain.interfaces import UserRepository

logger = logging.getLogger(__name__)


class MongoUserRepository(UserRepository):
    def __init__(self, db: AsyncIOMotorDatabase):
        self._users = db.users

    async def get_by_id(self, user_id: str) -> User | None:
        doc = await self._users.find_one({"user_id": user_id})
        return self._doc_to_user(doc) if doc else None

    async def get_by_username(self, username: str) -> User | None:
        doc = await self._users.find_one({"username": username})
        return self._doc_to_user(doc) if doc else None

    async def create(self, user: User) -> User:
        doc = self._user_to_doc(user)
        await self._users.insert_one(doc)
        return user

    async def update(self, user: User) -> User | None:
        result = await self._users.update_one(
            {"user_id": user.user_id},
            {"$set": self._user_to_doc(user)},
        )
        return user if result.modified_count > 0 else None

    async def delete(self, user_id: str) -> bool:
        result = await self._users.delete_one({"user_id": user_id})
        return result.deleted_count > 0

    async def list_all(self) -> list[User]:
        docs = await self._users.find().to_list(length=None)
        return [self._doc_to_user(d) for d in docs]

    @staticmethod
    def _doc_to_user(doc: dict[str, Any]) -> User:
        user = User(
            user_id=doc.get("user_id", ""),
            username=doc.get("username", ""),
            display_name=doc.get("display_name", ""),
            role=RoleType(doc.get("role", "viewer")),
            is_active=doc.get("is_active", True),
            created_at=doc.get("created_at", ""),
            last_login_at=doc.get("last_login_at", ""),
            ldap_groups=doc.get("ldap_groups"),
        )
        password_hash = doc.get("password_hash", "")
        if password_hash:
            user.password_hash = password_hash
        return user

    @staticmethod
    def _user_to_doc(user: User) -> dict[str, Any]:
        return {
            "user_id": user.user_id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role.value if user.role else "viewer",
            "password_hash": getattr(user, "password_hash", ""),
            "is_active": user.is_active,
            "created_at": user.created_at,
            "last_login_at": user.last_login_at,
            "ldap_groups": getattr(user, "ldap_groups", None),
        }


class MongoSessionRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self._sessions = db.sessions
        self._refresh_tokens = db.refresh_tokens

    async def create_session(self, token: str, user_id: str,
                             expires_at: datetime) -> str:
        await self._sessions.insert_one({
            "token": token,
            "user_id": user_id,
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
        })
        return token

    async def get_session(self, token: str) -> dict[str, Any] | None:
        return await self._sessions.find_one({"token": token})

    async def delete_session(self, token: str) -> bool:
        result = await self._sessions.delete_one({"token": token})
        return result.deleted_count > 0

    async def delete_user_sessions(self, user_id: str) -> int:
        result = await self._sessions.delete_many({"user_id": user_id})
        return result.deleted_count

    async def store_refresh_token(self, token: str, user_id: str,
                                  expires_at: datetime) -> str:
        await self._refresh_tokens.insert_one({
            "token": token,
            "user_id": user_id,
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
        })
        return token

    async def consume_refresh_token(self, token: str) -> dict[str, Any] | None:
        doc = await self._refresh_tokens.find_one({"token": token})
        return doc

    async def cleanup_expired_refresh_tokens(self) -> int:
        """Delete all expired refresh tokens. Returns count of deleted documents."""
        now = datetime.now(timezone.utc)
        result = await self._refresh_tokens.delete_many({
            "expires_at": {"$lt": now}
        })
        if result.deleted_count > 0:
            logger.info(f"Cleaned up {result.deleted_count} expired refresh tokens")
        return result.deleted_count
