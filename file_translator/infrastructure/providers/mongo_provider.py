"""MongoDB connection manager for auth storage."""

from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class MongoProvider:
    _client: AsyncIOMotorClient | None = None
    _db: AsyncIOMotorDatabase | None = None

    async def connect(self, uri: str, db_name: str = "file_translator_auth") -> None:
        self._client = AsyncIOMotorClient(uri)
        self._db = self._client[db_name]

        await self._db.users.create_index("username", unique=True)
        await self._db.sessions.create_index("token", unique=True)
        await self._db.sessions.create_index("expires_at", expireAfterSeconds=0)
        await self._db.refresh_tokens.create_index("token", unique=True)
        await self._db.refresh_tokens.create_index("expires_at", expireAfterSeconds=0)
        await self._db.refresh_tokens.create_index("user_id")

        logger.info(f"Connected to MongoDB: {db_name}")

    async def close(self) -> None:
        if self._client:
            self._client.close()
            logger.info("MongoDB connection closed")

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            raise RuntimeError("MongoDB not connected. Call connect() first.")
        return self._db
