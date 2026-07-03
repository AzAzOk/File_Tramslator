"""Authentication and authorization domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Permission(Enum):
    """Granular permissions for the translation system."""
    
    TRANSLATE = "translate"                   # Submit translation jobs
    VIEW_GLOSSARY = "glossary:view"           # Read glossary entries
    EDIT_GLOSSARY = "glossary:edit"           # Create/update/delete glossary
    VIEW_JOBS = "jobs:view"                   # View job status/history
    CANCEL_JOBS = "jobs:cancel"               # Cancel any job
    VIEW_JOURNAL = "journal:view"             # Read processing journal
    VIEW_USERS = "users:view"                 # List users
    MANAGE_USERS = "users:manage"             # Create/update/delete users
    MANAGE_SYSTEM = "system:manage"           # System-level operations
    SEND_FEEDBACK = "feedback:send"           # Submit support feedback
    VIEW_FEEDBACK = "feedback:view"           # View all feedback


class RoleType(Enum):
    """Predefined roles with bundled permissions."""
    
    ADMIN = "admin"           # Full access
    OPERATOR = "operator"     # Can translate, view/manage own jobs, view glossary
    VIEWER = "viewer"         # Read-only access to jobs and glossary
    API = "api"               # Machine account: translate only


_ROLE_PERMISSIONS: dict[RoleType, set[Permission]] = {
    RoleType.ADMIN: set(Permission),
    RoleType.OPERATOR: {
        Permission.TRANSLATE,
        Permission.VIEW_GLOSSARY,
        Permission.EDIT_GLOSSARY,
        Permission.VIEW_JOBS,
        Permission.CANCEL_JOBS,
        Permission.VIEW_JOURNAL,
        Permission.SEND_FEEDBACK,
    },
    RoleType.VIEWER: {
        Permission.VIEW_GLOSSARY,
        Permission.VIEW_JOBS,
        Permission.VIEW_JOURNAL,
        Permission.SEND_FEEDBACK,
    },
    RoleType.API: {
        Permission.TRANSLATE,
        Permission.VIEW_JOBS,
    },
}


def get_permissions_for_role(role: RoleType) -> set[Permission]:
    """Get the set of permissions granted to a given role."""
    return _ROLE_PERMISSIONS.get(role, set()).copy()


@dataclass
class User:
    """A user of the translation system."""
    
    user_id: str = ""
    username: str = ""
    display_name: str = ""
    role: RoleType = RoleType.VIEWER
    permissions: set[Permission] = field(default_factory=set)
    password_hash: str = ""
    ldap_groups: list[str] | None = None
    is_active: bool = True
    created_at: str = ""
    last_login_at: str = ""
    
    @property
    def effective_permissions(self) -> set[Permission]:
        """Get all permissions (role-based + individual grants)."""
        return get_permissions_for_role(self.role) | self.permissions
    
    def has_permission(self, permission: Permission) -> bool:
        """Check if user has a specific permission."""
        return permission in self.effective_permissions
    
    def has_any_permission(self, *permissions: Permission) -> bool:
        """Check if user has at least one of the given permissions."""
        return any(self.has_permission(p) for p in permissions)
    
    def has_all_permissions(self, *permissions: Permission) -> bool:
        """Check if user has all of the given permissions."""
        return all(self.has_permission(p) for p in permissions)


@dataclass
class ApiKey:
    """API key for machine-to-machine authentication."""
    
    key_id: str = ""
    key_hash: str = ""  # Hashed API key value
    name: str = ""
    user_id: str = ""
    is_active: bool = True
    created_at: str = ""
    expires_at: str = ""
    
    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at)
            return expiry < datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return False


@dataclass
class AuthToken:
    """A JWT or session token issued after authentication."""
    
    token: str = ""
    token_type: str = "bearer"
    user_id: str = ""
    username: str = ""
    role: str = ""
    issued_at: str = ""
    expires_at: str = ""
    
    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return True
        try:
            expiry = datetime.fromisoformat(self.expires_at)
            return expiry < datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return True


@dataclass
class AuthCredentials:
    """Parsed and validated authentication credentials."""
    
    user: User
    token: AuthToken | None = None
    api_key: ApiKey | None = None
    
    @property
    def is_authenticated(self) -> bool:
        return self.user is not None and self.user.is_active
    
    @property
    def username(self) -> str:
        return self.user.username
    
    @property
    def role(self) -> str:
        return self.user.role.value if self.user.role else ""
