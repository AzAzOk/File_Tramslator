"""Redis-backed token blacklist for JWT authentication."""

from __future__ import annotations

import logging
import os
from typing import Optional

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RedisTokenBlacklist:
    """Redis-backed token blacklist with automatic TTL.
    
    Stores blacklisted JTIs (JSON Web Token IDs) with TTL matching
    the token's original expiry, so entries auto-expire when the
    token would have naturally expired.
    """

    _KEY_PREFIX = "blacklist:jti:"

    def __init__(self, redis: Optional[Redis] = None):
        self._redis = redis

    async def _conn(self) -> Redis:
        if self._redis is None:
            password = os.environ.get("REDIS_PASSWORD", "")
            self._redis = Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                db=0,
                password=password or None,
                decode_responses=True,
            )
        return self._redis

    async def blacklist(self, jti: str, expires_at: float) -> None:
        """Add a token's JTI to the blacklist until its expiry.
        
        Args:
            jti: The token's unique identifier
            expires_at: Unix timestamp when the token expires
        """
        redis = await self._conn()
        key = f"{self._KEY_PREFIX}{jti}"
        
        # Calculate TTL: max(1 second, expiry - now)
        import time
        ttl = max(1, int(expires_at - time.time()))
        
        await redis.set(key, "1", ex=ttl)
        logger.debug(f"Token {jti[:8]}... blacklisted for {ttl}s")

    async def is_blacklisted(self, jti: str) -> bool:
        """Check if a JTI is blacklisted."""
        redis = await self._conn()
        key = f"{self._KEY_PREFIX}{jti}"
        return await redis.exists(key) > 0

    async def remove(self, jti: str) -> None:
        """Remove a JTI from the blacklist (if present)."""
        redis = await self._conn()
        key = f"{self._KEY_PREFIX}{jti}"
        await redis.delete(key)

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()
            self._redis = None
