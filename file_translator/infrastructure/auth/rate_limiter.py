"""Simple in-memory rate limiter for auth endpoints."""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Optional


class RateLimiter:
    """Token bucket rate limiter with sliding window.
    
    Tracks requests per IP address with configurable limits.
    Thread-safe via threading.Lock.
    """

    def __init__(self, max_requests: int = 5, window_seconds: int = 60):
        """
        Args:
            max_requests: Maximum requests allowed per window
            window_seconds: Time window in seconds
        """
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def _cleanup(self, key: str, now: float) -> None:
        """Remove expired entries for a key."""
        cutoff = now - self._window_seconds
        self._requests[key] = [
            ts for ts in self._requests[key] if ts > cutoff
        ]

    def is_allowed(self, key: str) -> bool:
        """Check if a request is allowed for the given key (e.g., IP address).
        
        Args:
            key: Identifier to rate limit (usually IP address)
            
        Returns:
            True if request is allowed, False if rate limited
        """
        now = time.time()
        with self._lock:
            self._cleanup(key, now)
            if len(self._requests[key]) >= self._max_requests:
                return False
            self._requests[key].append(now)
            return True

    def get_remaining(self, key: str) -> int:
        """Get remaining requests for the key."""
        now = time.time()
        with self._lock:
            self._cleanup(key, now)
            return max(0, self._max_requests - len(self._requests[key]))

    def get_retry_after(self, key: str) -> Optional[float]:
        """Get seconds until the next request is allowed.
        
        Returns:
            Seconds to wait, or None if request is allowed now
        """
        now = time.time()
        with self._lock:
            self._cleanup(key, now)
            if len(self._requests[key]) < self._max_requests:
                return None
            oldest = self._requests[key][0]
            return max(0, self._window_seconds - (now - oldest))


# Global rate limiters for auth endpoints
login_rate_limiter = RateLimiter(max_requests=5, window_seconds=60)  # 5 attempts per minute
refresh_rate_limiter = RateLimiter(max_requests=10, window_seconds=60)  # 10 attempts per minute


def get_client_ip(request) -> str:
    """Extract client IP from request, considering X-Forwarded-For header."""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
