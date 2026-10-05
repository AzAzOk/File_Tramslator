"""MongoDB repository for the admin-service audit trail.

Every admin mutation writes one ``admin_events`` document: who performed it,
what was changed, and when. Documents are append-only; the repository exposes
read helpers for the dashboard feed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class AdminEventRepository:
    """Append-only audit log in the ``admin_events`` collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self._collection = db.admin_events

    async def ensure_indexes(self) -> None:
        await self._collection.create_index([("created_at", -1)])
        await self._collection.create_index("actor")
        await self._collection.create_index("action")

    async def log(
        self,
        actor: str,
        action: str,
        target_type: str = "",
        target: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one audit record and return it."""
        doc: dict[str, Any] = {
            "actor": actor or "unknown",
            "action": action,
            "target_type": target_type,
            "target": target,
            "details": details or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._collection.insert_one(dict(doc))
        doc.pop("_id", None)
        logger.info(f"Admin event: {doc['actor']} {action} {target_type}={target}")
        return doc

    async def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent events first."""
        cursor = self._collection.find().sort("created_at", -1).limit(max(1, min(limit, 500)))
        return [{k: v for k, v in doc.items() if k != "_id"} async for doc in cursor]

    async def count(self) -> int:
        return await self._collection.count_documents({})
