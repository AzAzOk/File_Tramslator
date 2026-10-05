"""Request/response models for the admin API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class SessionInfo(BaseModel):
    authenticated: bool
    username: str = ""


class UserOut(BaseModel):
    user_id: str
    username: str
    display_name: str = ""
    role: str = ""
    manual_role: bool = False
    is_active: bool = True
    ldap_groups: list[str] = []
    permissions: list[str] = []
    role_permissions: list[str] = []
    last_login_at: str = ""


class AssignRoleRequest(BaseModel):
    role: str = Field(min_length=1, max_length=128)


class SetPermissionsRequest(BaseModel):
    permissions: list[str] = []


class SetActiveRequest(BaseModel):
    is_active: bool


class RoleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\- ]+$")
    description: str = ""
    permissions: list[str] = []
    grants: list[str] = []


class RoleUpdateRequest(BaseModel):
    description: str | None = None
    permissions: list[str] | None = None
    grants: list[str] | None = None


class RoleOut(BaseModel):
    name: str
    description: str = ""
    builtin: bool = False
    protected: bool = False
    permissions: list[str] = []
    grants: list[str] = []
    member_count: int = 0


class GrantRequest(BaseModel):
    subject_type: str = Field(pattern=r"^(group|role|user)$")
    subject: str = Field(min_length=1, max_length=256)
    collection: str = Field(min_length=1, max_length=128)
    read: bool = False
    write: bool = False


class MatrixRow(BaseModel):
    subject_type: str
    subject: str
    label: str = ""
    cells: dict[str, dict[str, bool]] = {}


class MatrixOut(BaseModel):
    collections: list[str] = []
    rows: list[MatrixRow] = []


class ChangePasswordRequest(BaseModel):
    # Length is validated by the endpoint so the caller gets an actionable
    # 400 message instead of a generic 422 validation error.
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class SettingsOut(BaseModel):
    admin_username: str
    config_version: int = 0
    password_source: str = "env"
    counts: dict[str, Any] = {}


class HealthOut(BaseModel):
    status: str
    mongo: dict[str, int] = {}
    collections: list[str] = []
    config_version: int = 0


class AuditEvent(BaseModel):
    actor: str
    action: str
    target_type: str = ""
    target: str = ""
    details: dict[str, Any] = {}
    created_at: str = ""


class DashboardOut(BaseModel):
    counts: dict[str, int] = {}
    collections: list[str] = []
    config_version: int = 0
    recent_events: list[AuditEvent] = []


class AdminGuideSection(BaseModel):
    """One heading of the rendered guide, as the navigation tree sees it."""

    level: int
    id: str
    text: str


class AdminGuideOut(BaseModel):
    """The guide rendered once, with the outline that describes that same HTML.

    Both halves travel together on purpose: the page never parses the article to
    discover its structure, so the tree cannot drift from the text.
    """

    title: str = ""
    html: str = ""
    sections: list[AdminGuideSection] = []
