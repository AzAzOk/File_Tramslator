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
    #: Rights the role grants but this user was specifically denied. Together
    #: with ``permissions`` these are the *deviations*; ``permissions`` alone
    #: must never contain a right the role already provides.
    denied: list[str] = []
    #: The state the dialog renders: (role ∖ denied) ∪ permissions. The client
    #: displays this and submits it back; the server re-derives the delta.
    effective: list[str] = []
    last_login_at: str = ""
    # The role this user would get from their AD groups. Lets the UI state the
    # consequence of a reset without a second request.
    ldap_role: str = ""


class AssignRoleRequest(BaseModel):
    role: str = Field(min_length=1, max_length=128)
    #: Accepting a role offer: take the role and drop every personal deviation
    #: in the same request, so access cannot change in between the two calls.
    clear_deviations: bool = False


class SetPermissionsRequest(BaseModel):
    #: The *desired effective* rights, not the personal set. The server derives
    #: the minimal deviation from the role, so a stale client cannot write a
    #: right the role already provides.
    permissions: list[str] = []


class RightsSaveResponse(BaseModel):
    user: UserOut
    #: How many rights were raised above the role and lowered below it — what
    #: the administrator is told changed, in place of echoing the payload back.
    raised: int = 0
    lowered: int = 0
    #: Every role that would reproduce the saved rights *and* the saved
    #: collection levels; empty when the state already is the role's. Never a
    #: bare permissions match (see design D3).
    suggestions: list[str] = []


class SetActiveRequest(BaseModel):
    is_active: bool


class CollectionAccessOut(BaseModel):
    collection: str
    #: 0 … 3 — the level the running API will actually enforce for this user.
    level: int = 0
    #: Where that level comes from: ``default`` (the unrevocable floor),
    #: ``role:<name>``, ``group:<name>``, ``personal``, or ``none`` when no
    #: matching document grants the collection at all.
    source: str = "none"


class UserAccessOut(BaseModel):
    user_id: str
    #: The built-in admin role bypasses every check, so the response says so
    #: instead of showing an empty list that would read as "no access".
    unrestricted: bool = False
    collections: list[CollectionAccessOut] = []


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
    #: Ordered access level (0 none … 3 edit). When omitted the level is derived
    #: from ``read``/``write``, so a legacy two-flag client keeps working; when
    #: present the level wins and the flags are recalculated from it.
    level: int | None = Field(default=None, ge=0, le=3)


class MatrixRow(BaseModel):
    subject_type: str
    subject: str
    label: str = ""
    #: The built-in ``admin`` role (and users holding it) bypasses every check,
    #: so the row says so instead of listing every collection as unavailable.
    unrestricted: bool = False
    #: Each cell carries the legacy read/write flags *and* the level they
    #: express, so a page written against the flags still renders unchanged.
    cells: dict[str, dict[str, bool | int]] = {}


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
