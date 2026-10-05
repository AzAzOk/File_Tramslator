"""MongoDB repositories for runtime role and grant configuration.

These provide low-level CRUD over the ``roles`` and ``grants`` collections.
The cached stores in
:mod:`file_translator.infrastructure.auth.role_config` sit on top of them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from file_translator.infrastructure.auth.role_config import GrantDoc, RoleDoc

logger = logging.getLogger(__name__)


class MongoRoleRepository:
    """CRUD over the MongoDB ``roles`` collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self._roles = db.roles

    async def ensure_indexes(self) -> None:
        await self._roles.create_index("name", unique=True)

    async def find_all(self) -> list[RoleDoc]:
        docs = await self._roles.find().to_list(length=None)
        roles = [RoleDoc.from_dict(d) for d in docs]
        return [r for r in roles if r is not None]

    async def find_by_name(self, name: str) -> RoleDoc | None:
        doc = await self._roles.find_one({"name": name})
        return RoleDoc.from_dict(doc)

    async def create(self, role: RoleDoc) -> RoleDoc:
        doc = dict(role.to_dict())
        doc["created_at"] = datetime.now(timezone.utc).isoformat()
        await self._roles.insert_one(doc)
        return role

    async def update(self, name: str, fields: dict[str, Any]) -> bool:
        result = await self._roles.update_one(
            {"name": name},
            {"$set": {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        return result.modified_count > 0

    async def delete(self, name: str) -> bool:
        result = await self._roles.delete_one({"name": name})
        return result.deleted_count > 0

    async def count(self) -> int:
        return await self._roles.count_documents({})


class MongoGrantRepository:
    """CRUD over the MongoDB ``grants`` collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self._grants = db.grants

    async def ensure_indexes(self) -> None:
        await self._grants.create_index(
            [("subject_type", 1), ("subject", 1), ("collection", 1)],
            unique=True,
        )

    async def find_all(self) -> list[GrantDoc]:
        docs = await self._grants.find().to_list(length=None)
        grants = [GrantDoc.from_dict(d) for d in docs]
        return [g for g in grants if g is not None]

    async def find_by_subject(self, subject_type: str, subject: str) -> list[GrantDoc]:
        docs = await self._grants.find(
            {"subject_type": subject_type, "subject": subject}
        ).to_list(length=None)
        grants = [GrantDoc.from_dict(d) for d in docs]
        return [g for g in grants if g is not None]

    async def find_by_subject_types(self, subjects_by_type: dict[str, list[str]]) -> list[GrantDoc]:
        """Find grants matching any of the given subject_type→subjects pairs.

        Built as an OR query so a single Mongo query resolves a user's
        group + role + individual grants at once.
        """
        or_clauses = [
            {"subject_type": st, "subject": {"$in": list(subjects)}}
            for st, subjects in subjects_by_type.items()
            if subjects
        ]
        if not or_clauses:
            return []
        docs = await self._grants.find({"$or": or_clauses}).to_list(length=None)
        grants = [GrantDoc.from_dict(d) for d in docs]
        return [g for g in grants if g is not None]

    async def upsert(self, grant: GrantDoc) -> GrantDoc:
        doc = grant.to_dict()
        await self._grants.update_one(
            {
                "subject_type": grant.subject_type,
                "subject": grant.subject,
                "collection": grant.collection,
            },
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        return grant

    async def delete(self, grant: GrantDoc) -> bool:
        result = await self._grants.delete_one(
            {
                "subject_type": grant.subject_type,
                "subject": grant.subject,
                "collection": grant.collection,
            }
        )
        return result.deleted_count > 0

    async def delete_many(
        self,
        subject_type: str | None = None,
        subject: str | None = None,
        collection: str | None = None,
    ) -> int:
        query: dict[str, Any] = {}
        if subject_type is not None:
            query["subject_type"] = subject_type
        if subject is not None:
            query["subject"] = subject
        if collection is not None:
            query["collection"] = collection
        result = await self._grants.delete_many(query)
        return result.deleted_count

    async def count(self) -> int:
        return await self._grants.count_documents({})