"""Unit tests for JWT authentication provider."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from file_translator.domain.auth import RoleType, User
from file_translator.infrastructure.auth.jwt_auth_provider import JwtAuthProvider


@pytest.fixture
def mock_user_repo():
    """Create a mock user repository."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def jwt_provider(mock_user_repo):
    """Create a JWT provider with test secret."""
    return JwtAuthProvider(
        secret_key="test-secret-key-for-testing-only",
        user_repository=mock_user_repo,
    )


@pytest.fixture
def jwt_provider_with_blacklist(mock_user_repo):
    """Create a JWT provider with Redis-backed blacklist."""
    mock_blacklist = AsyncMock()
    return JwtAuthProvider(
        secret_key="test-secret-key-for-testing-only",
        user_repository=mock_user_repo,
        token_blacklist=mock_blacklist,
    ), mock_blacklist


@pytest.fixture
def test_user():
    """Create a test user."""
    return User(
        user_id="test-user-id-123",
        username="testuser",
        display_name="Test User",
        role=RoleType.OPERATOR,
        is_active=True,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


class TestTokenCreation:
    """Tests for access and refresh token creation."""

    def test_create_access_token_returns_string(self, jwt_provider):
        token = jwt_provider.create_access_token(
            user_id="user123",
            username="testuser",
            role="operator",
        )
        assert isinstance(token, str)
        assert len(token) > 0

    def test_create_access_token_contains_correct_payload(self, jwt_provider):
        token = jwt_provider.create_access_token(
            user_id="user123",
            username="testuser",
            role="operator",
        )
        payload = jwt_provider.decode_token(token)
        assert payload is not None
        assert payload["sub"] == "user123"
        assert payload["username"] == "testuser"
        assert payload["role"] == "operator"
        assert payload["type"] == "access"
        assert "jti" in payload
        assert "exp" in payload
        assert "iat" in payload

    def test_create_refresh_token_returns_string(self, jwt_provider):
        token = jwt_provider.create_refresh_token(user_id="user123")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_create_refresh_token_contains_correct_payload(self, jwt_provider):
        token = jwt_provider.create_refresh_token(user_id="user123")
        payload = jwt_provider.decode_token(token)
        assert payload is not None
        assert payload["sub"] == "user123"
        assert payload["type"] == "refresh"
        assert "jti" in payload
        assert "exp" in payload

    def test_tokens_have_unique_jti(self, jwt_provider):
        token1 = jwt_provider.create_access_token("user1", "user1", "admin")
        token2 = jwt_provider.create_access_token("user1", "user1", "admin")
        payload1 = jwt_provider.decode_token(token1)
        payload2 = jwt_provider.decode_token(token2)
        assert payload1["jti"] != payload2["jti"]

    def test_access_token_expiry(self, jwt_provider):
        token = jwt_provider.create_access_token("user1", "user1", "admin")
        payload = jwt_provider.decode_token(token)
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = exp - now
        # Should be approximately 30 minutes
        assert 29 * 60 <= diff.total_seconds() <= 31 * 60

    def test_refresh_token_expiry(self, jwt_provider):
        token = jwt_provider.create_refresh_token("user1")
        payload = jwt_provider.decode_token(token)
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = exp - now
        # Should be approximately 7 days
        assert 6 * 24 * 60 * 60 <= diff.total_seconds() <= 8 * 24 * 60 * 60


class TestTokenDecoding:
    """Tests for token decoding."""

    def test_decode_valid_token(self, jwt_provider):
        token = jwt_provider.create_access_token("user1", "user1", "admin")
        payload = jwt_provider.decode_token(token)
        assert payload is not None
        assert payload["sub"] == "user1"

    def test_decode_invalid_token(self, jwt_provider):
        payload = jwt_provider.decode_token("invalid.token.here")
        assert payload is None

    def test_decode_token_with_wrong_secret(self, mock_user_repo):
        provider1 = JwtAuthProvider("secret1", mock_user_repo)
        provider2 = JwtAuthProvider("secret2", mock_user_repo)
        token = provider1.create_access_token("user1", "user1", "admin")
        payload = provider2.decode_token(token)
        assert payload is None

    def test_decode_expired_token(self, jwt_provider):
        # Create token with negative expiry (already expired)
        from jose import jwt
        from datetime import datetime, timedelta, timezone
        
        payload = {
            "sub": "user1",
            "username": "user1",
            "role": "admin",
            "type": "access",
            "jti": "test-jti",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            "iat": datetime.now(timezone.utc) - timedelta(hours=2),
        }
        token = jwt.encode(payload, jwt_provider._secret_key, algorithm=JwtAuthProvider.ALGORITHM)
        result = jwt_provider.decode_token(token)
        assert result is None


class TestTokenBlacklist:
    """Tests for token blacklisting (in-memory fallback)."""

    @pytest.mark.asyncio
    async def test_blacklist_token_in_memory(self, jwt_provider):
        jti = "test-jti-123"
        expires_at = time.time() + 3600  # 1 hour from now
        await jwt_provider.blacklist_token(jti, expires_at)
        assert await jwt_provider._is_blacklisted(jti)

    @pytest.mark.asyncio
    async def test_is_not_blacklisted_by_default(self, jwt_provider):
        assert not await jwt_provider._is_blacklisted("nonexistent-jti")

    def test_blacklist_with_redis(self, jwt_provider_with_blacklist):
        provider, mock_blacklist = jwt_provider_with_blacklist
        jti = "test-jti-redis"
        expires_at = time.time() + 3600
        
        import asyncio
        asyncio.run(provider.blacklist_token(jti, expires_at))
        
        mock_blacklist.blacklist.assert_called_once_with(jti, expires_at)

    def test_is_blacklisted_with_redis(self, jwt_provider_with_blacklist):
        provider, mock_blacklist = jwt_provider_with_blacklist
        mock_blacklist.is_blacklisted.return_value = True
        
        import asyncio
        result = asyncio.run(provider._is_blacklisted("test-jti"))
        
        assert result is True
        mock_blacklist.is_blacklisted.assert_called_once_with("test-jti")


class TestPasswordHashing:
    """Tests for password hashing utilities."""

    def test_hash_password_returns_string(self, jwt_provider):
        hashed = jwt_provider.hash_password("mypassword")
        assert isinstance(hashed, str)
        assert len(hashed) > 0

    def test_verify_password_correct(self, jwt_provider):
        password = "securepassword123"
        hashed = jwt_provider.hash_password(password)
        assert jwt_provider.verify_password(password, hashed)

    def test_verify_password_incorrect(self, jwt_provider):
        password = "securepassword123"
        hashed = jwt_provider.hash_password(password)
        assert not jwt_provider.verify_password("wrongpassword", hashed)

    def test_different_hashes_for_same_password(self, jwt_provider):
        password = "samepassword"
        hash1 = jwt_provider.hash_password(password)
        hash2 = jwt_provider.hash_password(password)
        assert hash1 != hash2  # bcrypt uses random salt


class TestAuthentication:
    """Tests for the authenticate method."""

    @pytest.mark.asyncio
    async def test_authenticate_valid_token(self, jwt_provider, mock_user_repo, test_user):
        mock_user_repo.get_by_id.return_value = test_user
        token = jwt_provider.create_access_token(
            user_id=test_user.user_id,
            username=test_user.username,
            role=test_user.role.value,
        )
        
        result = await jwt_provider.authenticate(token)
        
        assert result is not None
        assert result.user.user_id == test_user.user_id
        assert result.token.token == token

    @pytest.mark.asyncio
    async def test_authenticate_refresh_token_rejected(self, jwt_provider, mock_user_repo):
        """Refresh tokens should not be accepted for authentication."""
        token = jwt_provider.create_refresh_token(user_id="user1")
        
        result = await jwt_provider.authenticate(token)
        
        assert result is None

    @pytest.mark.asyncio
    async def test_authenticate_invalid_token(self, jwt_provider):
        result = await jwt_provider.authenticate("invalid.token.here")
        assert result is None

    @pytest.mark.asyncio
    async def test_authenticate_blacklisted_token(self, jwt_provider, mock_user_repo, test_user):
        mock_user_repo.get_by_id.return_value = test_user
        token = jwt_provider.create_access_token(
            user_id=test_user.user_id,
            username=test_user.username,
            role=test_user.role.value,
        )
        
        # Blacklist the token
        payload = jwt_provider.decode_token(token)
        await jwt_provider.blacklist_token(payload["jti"], payload["exp"])
        
        result = await jwt_provider.authenticate(token)
        assert result is None

    @pytest.mark.asyncio
    async def test_authenticate_inactive_user(self, jwt_provider, mock_user_repo):
        inactive_user = User(
            user_id="inactive-user",
            username="inactive",
            display_name="Inactive User",
            role=RoleType.VIEWER,
            is_active=False,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        mock_user_repo.get_by_id.return_value = inactive_user
        
        token = jwt_provider.create_access_token(
            user_id=inactive_user.user_id,
            username=inactive_user.username,
            role=inactive_user.role.value,
        )
        
        result = await jwt_provider.authenticate(token)
        assert result is None

    @pytest.mark.asyncio
    async def test_authenticate_nonexistent_user(self, jwt_provider, mock_user_repo):
        mock_user_repo.get_by_id.return_value = None
        
        token = jwt_provider.create_access_token(
            user_id="nonexistent",
            username="ghost",
            role="admin",
        )
        
        result = await jwt_provider.authenticate(token)
        assert result is None


class TestCreateToken:
    """Tests for the create_token method."""

    @pytest.mark.asyncio
    async def test_create_token_returns_auth_token(self, jwt_provider, mock_user_repo, test_user):
        mock_user_repo.get_by_id.return_value = test_user
        
        result = await jwt_provider.create_token(user_id=test_user.user_id)
        
        assert result is not None
        assert result.user_id == test_user.user_id
        assert result.username == test_user.username
        assert result.token_type == "bearer"

    @pytest.mark.asyncio
    async def test_create_token_nonexistent_user(self, jwt_provider, mock_user_repo):
        mock_user_repo.get_by_id.return_value = None
        
        with pytest.raises(ValueError, match="User not found"):
            await jwt_provider.create_token(user_id="nonexistent")
