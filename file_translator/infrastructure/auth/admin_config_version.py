"""Redis-backed admin configuration version.

Every admin-service write bumps the ``admin_config_version`` key. The main
API passes :func:`get_version` as a ``version_provider`` to the role/grant
stores: when the observed version changes, the store invalidates its TTL cache
immediately (the 30-second TTL remains as a safety net when Redis is down).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

ADMIN_CONFIG_VERSION_KEY = "admin_config_version"
_VERSION_TTL_SECONDS = 3600


class AdminConfigVersion:
    """Read/bump the single propagation version key."""

    def __init__(self, redis: Optional[Redis] = None, ttl_seconds: int = _VERSION_TTL_SECONDS):
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def _conn(self) -> Redis:
        if self._redis is None:
            self._redis = Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                db=0,
                password=os.environ.get("REDIS_PASSWORD", "") or None,
                decode_responses=True,
            )
        return self._redis

    async def get_version(self) -> int | None:
        """Return the current config version (0 when never bumped/absent).

        Returns None only when Redis is unavailable (callers then rely on TTL).
        """
        try:
            redis = await self._conn()
            value = await redis.get(ADMIN_CONFIG_VERSION_KEY)
            return int(value or 0)
        except Exception as e:
            logger.debug(f"Config version read failed: {e}")
            return None

    async def bump(self) -> int:
        """Increment the config version and return the new value."""
        redis = await self._conn()
        version = await redis.incr(ADMIN_CONFIG_VERSION_KEY)
        await redis.expire(ADMIN_CONFIG_VERSION_KEY, self._ttl_seconds)
        logger.info(f"Admin config version bumped to {version}")
        return int(version)

    def as_provider(self):
        """Return a ``version_provider`` suitable for RoleConfigStore/GrantStore."""
        return self.get_version