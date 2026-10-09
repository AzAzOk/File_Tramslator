"""Infrastructure wiring and request dependencies for the admin service.

Everything the routers need lives in a single :class:`AdminContainer`. The
production lifespan builds it from the environment; tests build it directly
with in-memory fakes, which keeps the endpoint tests fast and hermetic.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt

from admin_service.config import SESSION_COOKIE, SESSION_TTL_HOURS, AdminConfig
from file_translator.infrastructure.auth.admin_config_version import AdminConfigVersion
from file_translator.infrastructure.auth.role_config import (
    DEFAULT_COLLECTION,
    GrantDoc,
    GrantStore,
    RoleConfigStore,
)
from file_translator.infrastructure.repositories.auth_repository import MongoUserRepository
from file_translator.infrastructure.repositories.mongo_admin_event_repository import (
    AdminEventRepository,
)
from file_translator.infrastructure.repositories.mongo_config_repository import (
    MongoGrantRepository,
    MongoRoleRepository,
)

logger = logging.getLogger(__name__)

_PBKDF_ROUNDS = 12


class CollectionSource(Protocol):
    """Read-only provider of the glossary collection ids."""

    async def list_collections(self) -> list[str]:
        ...


class MySQLCollectionSource:
    """Lists collection ids from the existing ``glossary``/``glossary_*`` tables.

    Only ``information_schema`` is queried — the admin service never reads or
    writes glossary data, it just needs the list of collections to build the
    access matrix.
    """

    def __init__(self, repository: Any | None = None) -> None:
        if repository is not None:
            self._repository = repository
        else:
            from file_translator.infrastructure.repositories.mysql_glossary_repository import (
                MySQLGlossaryRepository,
            )

            self._repository = MySQLGlossaryRepository()

    async def list_collections(self) -> list[str]:
        tables = await self._repository.list_tables("glossary%")
        return [self._to_collection_id(t) for t in tables]

    @staticmethod
    def _to_collection_id(table: str) -> str:
        if table == "glossary":
            return "default"
        if table.startswith("glossary_"):
            return table[len("glossary_"):]
        return table


async def known_collections(container: "AdminContainer", grants: list[GrantDoc]) -> list[str]:
    """Collections from MySQL plus ``default`` and anything already granted.

    Lives here rather than in the access router so both routers can ask the
    same question: the access matrix and the per-collection view of one user
    must never disagree about which collections exist (otherwise a collection
    could be reported as "level 0, source none" on one screen and be absent
    from the other).
    """
    names: set[str] = {DEFAULT_COLLECTION}
    try:
        names.update(await container.collection_source.list_collections())
    except Exception:
        # Both screens must stay usable while MySQL is down — grants still list.
        logger.warning("Could not list collections from MySQL", exc_info=True)
    names.update(g.collection for g in grants if g.collection)
    return sorted(names)


class AdminPasswordStore:
    """Admin UI password stored as a bcrypt hash in Mongo.

    ``ADMIN_UI_PASSWORD`` is the bootstrap/fallback value: it is used whenever
    no hash has been stored yet. Once an administrator changes the password
    through the settings page the bcrypt hash wins, so the new password is
    required on the next login.
    """

    _DOC_ID = "admin_credentials"

    def __init__(self, db: Any, env_password: str) -> None:
        self._collection = db.admin_settings
        self._env_password = env_password

    async def stored_hash(self) -> str:
        """The bcrypt hash persisted in Mongo, or ``""`` when none exists yet."""
        doc = await self._collection.find_one({"_id": self._DOC_ID})
        if doc and doc.get("password_hash"):
            return str(doc["password_hash"])
        return ""

    def _env_hash(self) -> str:
        return bcrypt.hashpw(
            self._env_password.encode("utf-8"), bcrypt.gensalt(rounds=_PBKDF_ROUNDS)
        ).decode("utf-8")

    async def effective_hash(self) -> str:
        """The hash that currently authenticates: stored one, else the env one."""
        return await self.stored_hash() or self._env_hash()

    async def verify(self, password: str) -> bool:
        hash_value = await self.effective_hash()
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hash_value.encode("utf-8"))
        except (ValueError, TypeError):
            logger.warning("Stored admin password hash is malformed")
            return False

    async def set_password(self, password: str) -> None:
        """Persist a bcrypt hash so the new password is required next login."""
        new_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=_PBKDF_ROUNDS)
        ).decode("utf-8")
        await self._collection.update_one(
            {"_id": self._DOC_ID},
            {"$set": {"password_hash": new_hash}},
            upsert=True,
        )
        logger.info("Admin UI password changed (bcrypt hash stored in Mongo)")


class SessionTokens:
    """Stateless signed session tokens stored in an httpOnly cookie."""

    def __init__(self, secret: str, ttl_hours: int = SESSION_TTL_HOURS) -> None:
        self._secret = secret
        self._ttl_hours = ttl_hours

    @staticmethod
    def _secret_from_env() -> str:
        return (
            os.environ.get("ADMIN_SESSION_SECRET")
            or os.environ.get("JWT_SECRET")
            or secrets.token_hex(32)
        )

    def issue(self, username: str) -> str:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        payload = {
            "sub": username,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=self._ttl_hours)).timestamp()),
            "jti": secrets.token_hex(8),
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def verify(self, token: str) -> str | None:
        try:
            payload = jwt.decode(token, self._secret, algorithms=["HS256"])
        except JWTError:
            return None
        subject = payload.get("sub")
        return str(subject) if subject else None


@dataclass
class AdminContainer:
    """All collaborators used by the admin API, bundled for injection."""

    config: AdminConfig
    role_store: RoleConfigStore
    grant_store: GrantStore
    event_repo: AdminEventRepository
    user_repo: MongoUserRepository
    collection_source: CollectionSource
    config_version: AdminConfigVersion
    passwords: AdminPasswordStore
    sessions: SessionTokens
    clients: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.clients is None:
            self.clients = []

    async def close(self) -> None:
        for client in self.clients:
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # pragma: no cover - best-effort shutdown
                logger.warning("Error while closing admin client", exc_info=True)

    async def audit(
        self,
        actor: str,
        action: str,
        target_type: str = "",
        target: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record an admin mutation. Never fails the request it describes."""
        try:
            await self.event_repo.log(actor, action, target_type, target, details)
        except Exception:
            logger.warning(f"Failed to write admin event {action}", exc_info=True)


async def build_container(config: AdminConfig) -> AdminContainer:
    """Create the production container from environment configuration."""
    from motor.motor_asyncio import AsyncIOMotorClient
    from redis.asyncio import Redis

    from file_translator.infrastructure.auth.config_seeder import run_startup_seeding

    mongo_client = AsyncIOMotorClient(config.mongo_uri)
    db = mongo_client[config.mongo_db]
    redis_client = Redis(
        host=config.redis_host,
        port=config.redis_port,
        password=config.redis_password or None,
        decode_responses=True,
    )

    config_version = AdminConfigVersion(redis_client)
    role_repository = MongoRoleRepository(db)
    grant_repository = MongoGrantRepository(db)
    role_store = RoleConfigStore(
        role_repository,
        version_provider=config_version.get_version,
        version_bumper=config_version.bump,
    )
    grant_store = GrantStore(
        grant_repository,
        version_provider=config_version.get_version,
        version_bumper=config_version.bump,
    )
    user_repo = MongoUserRepository(db)
    event_repo = AdminEventRepository(db)
    await event_repo.ensure_indexes()

    # The admin service seeds on start too, so a fresh deployment behaves the
    # same whether the main API or the admin UI boots first.
    await run_startup_seeding(role_repository, grant_repository, user_repo)

    return AdminContainer(
        config=config,
        role_store=role_store,
        grant_store=grant_store,
        event_repo=event_repo,
        user_repo=user_repo,
        collection_source=MySQLCollectionSource(),
        config_version=config_version,
        passwords=AdminPasswordStore(db, config.password),
        sessions=SessionTokens(SessionTokens._secret_from_env()),
        clients=[mongo_client, redis_client],
    )


def get_container(request: Request) -> AdminContainer:
    container = getattr(request.app.state, "admin", None)
    if container is None:  # pragma: no cover - lifespan always sets it
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin service is not initialised",
        )
    return container


def get_current_admin(
    request: Request,
    container: AdminContainer = Depends(get_container),
) -> str:
    """Validate the session cookie and return the logged-in admin username."""
    token = request.cookies.get(SESSION_COOKIE, "")
    username = container.sessions.verify(token) if token else None
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return username
