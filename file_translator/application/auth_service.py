"""Authentication service - Login, token management, permission checks."""

from __future__ import annotations

import bcrypt
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from file_translator.domain.auth import AuthCredentials, AuthToken, Permission, RoleType, User
from file_translator.domain.interfaces import AuthProvider, UserRepository

logger = logging.getLogger(__name__)


class AuthService:
    """Service for authentication and authorization.
    
    Handles:
    - User login and token issuance (LDAP first, local fallback)
    - Token validation (bearer, API key)
    - Permission checking
    - User management
    """
    
    def __init__(self, auth_provider: AuthProvider | None = None,
                 user_repository: UserRepository | None = None):
        self._auth_provider = auth_provider
        self._user_repository = user_repository
        self.session_repo = None
        self.ldap_service = None
    
    @property
    def auth_provider(self) -> AuthProvider:
        if not self._auth_provider:
            from file_translator.infrastructure.auth.stub_auth_provider import StubAuthProvider
            self._auth_provider = StubAuthProvider(user_repository=self.user_repository)
        return self._auth_provider
    
    @property
    def user_repository(self) -> UserRepository:
        if not self._user_repository:
            from file_translator.infrastructure.auth.stub_user_repository import StubUserRepository
            self._user_repository = StubUserRepository()
        return self._user_repository
    
    async def login(self, username: str, password: str) -> AuthToken | None:
        """Authenticate a user with username/password.

        Tries LDAP first (if configured), falls back to local password check.
        LDAP success auto-creates user in MongoDB on first login.

        Returns an AuthToken on success, None on failure.
        """
        # Try LDAP first
        if self.ldap_service:
            ldap_info = await self.ldap_service.authenticate(username, password)
            if ldap_info:
                role = self.ldap_service.map_to_role(ldap_info.groups)
                return await self._handle_successful_login(
                    username, ldap_info.display_name, role, ldap_info.groups,
                )
            logger.info(f"LDAP login failed for '{username}', trying local auth")

        # Fall back to local password check
        user = await self.user_repository.get_by_username(username)
        if not user or not user.is_active:
            logger.warning(f"Login failed: user '{username}' not found or inactive")
            return None
        
        if not hasattr(user, 'password_hash') or not user.password_hash:
            logger.warning(f"Login failed: no password set for '{username}'")
            return None
        
        if not self._verify_password(password, user.password_hash):
            logger.warning(f"Login failed: wrong password for '{username}'")
            return None
        
        token = await self.auth_provider.create_token(user.user_id)
        user.last_login_at = datetime.now(timezone.utc).isoformat()
        await self.user_repository.update(user)
        
        logger.info(f"User '{username}' logged in successfully (local auth)")
        return token
    
    async def _handle_successful_login(self, username: str,
                                        display_name: str,
                                        role: RoleType,
                                        ldap_groups: list[str] | None = None) -> AuthToken | None:
        """Handle post-authentication: find-or-create user, issue token."""
        user = await self.user_repository.get_by_username(username)
        if not user:
            user = User(
                user_id=str(uuid.uuid4()),
                username=username,
                display_name=display_name or username,
                role=role,
                ldap_groups=ldap_groups,
                is_active=True,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            user = await self.user_repository.create(user)
            logger.info(f"User '{username}' auto-created via LDAP with role {role.value}")
        elif not user.is_active:
            logger.warning(f"Login failed: user '{username}' inactive")
            return None
        else:
            if user.role != role:
                user.role = role
                user.ldap_groups = ldap_groups
                await self.user_repository.update(user)
                logger.info(f"User '{username}' role updated to {role.value} via LDAP")

        token = await self.auth_provider.create_token(user.user_id)
        user.last_login_at = datetime.now(timezone.utc).isoformat()
        await self.user_repository.update(user)

        logger.info(f"User '{username}' logged in via LDAP")
        return token
    
    async def authenticate_request(self, authorization: str | None,
                                    api_key: str | None = None) -> AuthCredentials:
        """Authenticate an incoming request via header or API key.
        
        Args:
            authorization: The 'Authorization' header value (Bearer <token> or Basic <creds>).
            api_key: Alternative API key authentication.
            
        Returns:
            AuthCredentials with the authenticated user (or anonymous user on failure).
        """
        # Try API key first
        if api_key:
            try:
                user = await self.auth_provider.validate_api_key(api_key)
                if user:
                    return AuthCredentials(user=user, api_key=None)
            except Exception as e:
                logger.warning(f"API key validation failed: {e}")
        
        # Try bearer token
        if authorization and authorization.lower().startswith("bearer "):
            token_str = authorization[7:]
            try:
                result = await self.auth_provider.authenticate(token_str, "bearer")
                if result:
                    return result
            except Exception as e:
                logger.warning(f"Bearer auth failed: {e}")
        
        # Return anonymous user (no permissions)
        return AuthCredentials(
            user=User(
                user_id="anonymous",
                username="anonymous",
                role=RoleType.VIEWER,
                permissions=set(),
                is_active=False,
            ),
        )
    
    def check_permission(self, credentials: AuthCredentials,
                         permission: Permission) -> bool:
        """Check if authenticated user has a specific permission."""
        if not credentials or not credentials.is_authenticated:
            return False
        return credentials.user.has_permission(permission)
    
    def require_permission(self, credentials: AuthCredentials,
                           permission: Permission) -> None:
        """Raise PermissionError if user lacks the given permission."""
        if not self.check_permission(credentials, permission):
            username = credentials.username if credentials else "anonymous"
            raise PermissionError(
                f"User '{username}' lacks required permission: {permission.value}"
            )
    
    async def create_user(self, username: str, password: str,
                           role: RoleType = RoleType.VIEWER,
                           display_name: str = "") -> User:
        """Create a new user with hashed password."""
        user = User(
            user_id=str(uuid.uuid4()),
            username=username,
            display_name=display_name or username,
            role=role,
            is_active=True,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        user.password_hash = self._hash_password(password)
        result = await self.user_repository.create(user)
        logger.info(f"User created: {username} ({role.value})")
        return result
    
    async def list_users(self) -> list[User]:
        """List all registered users."""
        return await self.user_repository.list_all()
    
    @staticmethod
    def _hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def _verify_password(password: str, hash_value: str) -> bool:
        return bcrypt.checkpw(password.encode("utf-8"), hash_value.encode("utf-8"))
