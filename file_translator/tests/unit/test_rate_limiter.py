"""Unit tests for rate limiter."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from file_translator.infrastructure.auth.rate_limiter import (
    RateLimiter,
    get_client_ip,
    login_rate_limiter,
    refresh_rate_limiter,
)


class TestRateLimiter:
    """Tests for the RateLimiter class."""

    def test_allows_requests_within_limit(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        
        assert limiter.is_allowed("ip1") is True
        assert limiter.is_allowed("ip1") is True
        assert limiter.is_allowed("ip1") is True

    def test_blocks_requests_over_limit(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        
        assert limiter.is_allowed("ip1") is True
        assert limiter.is_allowed("ip1") is True
        assert limiter.is_allowed("ip1") is False

    def test_different_keys_independent(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        
        assert limiter.is_allowed("ip1") is True
        assert limiter.is_allowed("ip2") is True
        assert limiter.is_allowed("ip1") is False  # ip1 exhausted
        assert limiter.is_allowed("ip2") is False  # ip2 exhausted

    def test_window_expiry_allows_new_requests(self):
        limiter = RateLimiter(max_requests=1, window_seconds=1)
        
        assert limiter.is_allowed("ip1") is True
        assert limiter.is_allowed("ip1") is False
        
        # Wait for window to expire
        time.sleep(1.1)
        
        assert limiter.is_allowed("ip1") is True

    def test_get_remaining(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        
        assert limiter.get_remaining("ip1") == 3
        limiter.is_allowed("ip1")
        assert limiter.get_remaining("ip1") == 2
        limiter.is_allowed("ip1")
        limiter.is_allowed("ip1")
        assert limiter.get_remaining("ip1") == 0

    def test_get_retry_after_when_not_limited(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        
        assert limiter.get_retry_after("ip1") is None

    def test_get_retry_after_when_limited(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        
        limiter.is_allowed("ip1")
        retry_after = limiter.get_retry_after("ip1")
        
        assert retry_after is not None
        assert retry_after > 0
        assert retry_after <= 60


class TestGetClientIp:
    """Tests for get_client_ip function."""

    def test_extracts_ip_from_client(self):
        request = MagicMock()
        request.client.host = "192.168.1.100"
        request.headers = {}
        
        assert get_client_ip(request) == "192.168.1.100"

    def test_extracts_ip_from_x_forwarded_for(self):
        request = MagicMock()
        request.client.host = "127.0.0.1"
        request.headers = {"x-forwarded-for": "10.0.0.1, 10.0.0.2"}
        
        assert get_client_ip(request) == "10.0.0.1"

    def test_handles_no_client(self):
        request = MagicMock()
        request.client = None
        request.headers = {}
        
        assert get_client_ip(request) == "unknown"


class TestGlobalRateLimiters:
    """Tests for the global rate limiter instances."""

    def test_login_rate_limiter_exists(self):
        assert login_rate_limiter is not None
        assert login_rate_limiter._max_requests == 5

    def test_refresh_rate_limiter_exists(self):
        assert refresh_rate_limiter is not None
        assert refresh_rate_limiter._max_requests == 10
