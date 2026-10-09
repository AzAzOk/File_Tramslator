"""Authentication and authorization domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Permission(Enum):
    """Granular permissions for the translation system.

    Deliberately small catalog: the whole per-user translation workflow is one
    right (``translate``), glossary access is governed by per-collection grants
    (no global right), and the journal is readable by every authenticated user.
    """

    TRANSLATE = "translate"                   # Submit translation jobs & manage own jobs
    MANAGE_SYSTEM = "system:manage"           # System-level operations (incl. all users' jobs)
    SEND_FEEDBACK = "feedback:send"           # Submit support feedback
    VIEW_FEEDBACK = "feedback:view"           # View all feedback


#: Human-readable wording for each right, kept beside the values it names so a
#: right cannot be added without one. The admin API serves it from here instead
#: of the browser holding a second copy that would drift.
PERMISSION_LABELS: dict[str, str] = {
    Permission.TRANSLATE.value: "Отправлять документы на перевод",
    Permission.MANAGE_SYSTEM.value: "Системные операции",
    Permission.SEND_FEEDBACK.value: "Отправлять отзыв",
    Permission.VIEW_FEEDBACK.value: "Просмотр всех отзывов",
}


class RoleType(Enum):
    """Predefined roles with bundled permissions.

    ``USER`` is the standard person role managed as a Mongo document by the
    admin service; it is the successor of the legacy ``OPERATOR`` role. The
    remaining members form the legacy compatibility layer for pre-2.x user
    documents (``operator``/``viewer``/``api`` literal values).
    """
    
    ADMIN = "admin"           # Full access
    USER = "user"             # Standard permissions for people (runtime role)
    OPERATOR = "operator"     # Legacy: can translate and send feedback
    VIEWER = "viewer"         # Legacy: can send feedback only
    API = "api"               # Machine account: translate only


_ROLE_PERMISSIONS: dict[RoleType, set[Permission]] = {
    RoleType.ADMIN: set(Permission),
    RoleType.USER: {
        Permission.TRANSLATE,
        Permission.SEND_FEEDBACK,
    },
    RoleType.OPERATOR: {
        Permission.TRANSLATE,
        Permission.SEND_FEEDBACK,
    },
    RoleType.VIEWER: {
        Permission.SEND_FEEDBACK,
    },
    RoleType.API: {
        Permission.TRANSLATE,
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
    #: Personal *deviations* from the role, downward: rights the role grants but
    #: this user specifically does not have. Kept separate from ``permissions``
    #: (which holds only raises) so that a right the role provides is never
    #: duplicated on the user document, and so that lowering a right for one
    #: person cannot touch anyone else in the role.
    denied: set[Permission] = field(default_factory=set)
    password_hash: str = ""
    ldap_groups: list[str] | None = None
    is_active: bool = True
    created_at: str = ""
    last_login_at: str = ""
    # Raw persisted role name (may be a custom role managed through the admin
    # service and therefore not a member of the legacy RoleType enum).
    role_name: str = ""
    # True when an administrator manually assigned the role (AD logins must
    # NOT overwrite a manual role).
    manual_role: bool = False
    
    @property
    def effective_permissions(self) -> set[Permission]:
        """Effective rights: the role's pool, minus personal denials, plus
        personal raises.

        A right both denied and raised resolves as granted (the raise wins),
        which is the resolution ``POST /users/{id}/permissions`` guarantees by
        deriving the two sets from one target set.
        """
        return (get_permissions_for_role(self.role) - self.denied) | self.permissions

    @property
    def has_deviations(self) -> bool:
        """True when the user's effective rights differ from their role's."""
        return bool(self.denied or self.permissions)
    
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
