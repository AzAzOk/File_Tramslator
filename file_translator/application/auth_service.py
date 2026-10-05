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
    - Permission checking (role registry aware)
    - Collection access checking
    - User management
    """
    
    def __init__(self, auth_provider: AuthProvider | None = None,
                 user_repository: UserRepository | None = None,
                 role_store: Any | None = None,
                 collection_resolver: Any | None = None):
        self._auth_provider = auth_provider
        self._user_repository = user_repository
        self._role_store = role_store
        self._collection_resolver = collection_resolver
        self.session_repo: Any = None
        self.ldap_service: Any = None
    
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

    @property
    def role_store(self) -> Any:
        """RoleConfigStore used to resolve effective permissions.

        Falls back to a store without a repository → Python defaults.
        """
        if not self._role_store:
            from file_translator.infrastructure.auth.role_config import RoleConfigStore
            self._role_store = RoleConfigStore()
        return self._role_store

    @property
    def collection_resolver(self) -> Any:
        """GlossaryAccessResolver used for collection-level access checks."""
        if not self._collection_resolver:
            from file_translator.infrastructure.auth.glossary_access_resolver import GlossaryAccessResolver
            self._collection_resolver = GlossaryAccessResolver()
        return self._collection_resolver
    
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
        """Handle post-authentication: find-or-create user, issue token.

        Non-destructive sync (design D5):

        - Creates the user when absent (role from ``map_to_role``,
          ``manual_role=false``).
        - Otherwise refreshes ONLY the LDAP-owned facts when they changed
          (``display_name``, ``ldap_groups``) and updates the role only when
          ``manual_role=false`` and the mapped role differs. A manually
          assigned role always survives an AD login.
        """
        user = await self.user_repository.get_by_username(username)
        if not user:
            user = User(
                user_id=str(uuid.uuid4()),
                username=username,
                display_name=display_name or username,
                role=role,
                role_name=role.value,
                ldap_groups=ldap_groups,
                is_active=True,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            await self.user_repository.create(user)
            logger.info(f"User '{username}' auto-created via LDAP with role {role.value}")
        elif not user.is_active:
            logger.warning(f"Login failed: user '{username}' inactive")
            return None
        else:
            changed = False
            new_display = display_name or username
            if user.display_name != new_display:
                user.display_name = new_display
                changed = True
            if sorted(user.ldap_groups or []) != sorted(ldap_groups or []):
                user.ldap_groups = ldap_groups
                changed = True
            if not user.manual_role and user.role != role:
                user.role = role
                user.role_name = role.value
                changed = True
                logger.info(f"User '{username}' role updated to {role.value} via LDAP")
            if changed:
                await self.user_repository.update(user)

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
    
    async def effective_permissions_for(self, user: User) -> set[Permission]:
        """Resolve a user's effective permissions from the runtime role registry.

        Combines the role's permission pool (Mongo → cached → Python fallback)
        with the user's individual permission overrides. The built-in admin
        role is always granted every permission.
        """
        role_name = getattr(user, "role_name", "") or (user.role.value if user.role else "")
        if role_name == RoleType.ADMIN.value:
            return set(Permission)
        role_permission_values = await self.role_store.resolve_permissions(role_name)
        effective: set[Permission] = set()
        for value in role_permission_values:
            try:
                effective.add(Permission(value))
            except ValueError:
                logger.debug(f"Ignoring unknown permission value '{value}' for role '{role_name}'")
        return effective | set(getattr(user, "permissions", set()))

    async def check_permission(self, credentials: AuthCredentials,
                                 permission: Permission) -> bool:
        """Check if authenticated user has a specific permission."""
        if not credentials or not credentials.is_authenticated:
            return False
        return permission in await self.effective_permissions_for(credentials.user)

    async def require_permission(self, credentials: AuthCredentials,
                                   permission: Permission) -> None:
        """Raise PermissionError if user lacks the given permission."""
        if not await self.check_permission(credentials, permission):
            username = credentials.username if credentials else "anonymous"
            raise PermissionError(
                f"User '{username}' lacks required permission: {permission.value}"
            )

    async def check_collection_access(self, credentials: AuthCredentials,
                                      collection_id: str, level: str = "read") -> bool:
        """Check read/write collection access for an authenticated user."""
        if not credentials or not credentials.is_authenticated:
            return False
        return await self.collection_resolver.can_access(
            credentials.user, collection_id, level,
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
