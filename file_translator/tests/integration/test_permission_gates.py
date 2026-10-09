"""Endpoint gate tests for the slimmed permission model.

Covers the spec scenarios from change `permission-model-slimming`:
- removed admin-user routes are gone (`/auth/users` → 404);
- the journal needs authentication only, not a dedicated right;
- sending feedback needs `feedback:send` (403 otherwise);
- `GET /jobs` is scoped server-side (own jobs only, unless `system:manage`).

The FastAPI app is driven through a `TestClient` WITHOUT the lifespan context
manager (startup would try to reach Mongo/Redis), so the auth middleware is fed
a fake service placed on `app.state`.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

# Must be set before importing the app (it fails at import without these).
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("GLOSSARY_DB_PASSWORD", "test-pass")

from fastapi.testclient import TestClient  # noqa: E402

import file_translator.presentation.api.app as api_app  # noqa: E402
from file_translator.domain.auth import (  # noqa: E402
    AuthCredentials,
    Permission,
    RoleType,
    User,
)
from file_translator.domain.job import Job  # noqa: E402
from file_translator.presentation.api.dependencies import (  # noqa: E402
    get_auth_service,
    get_current_user,
)


class FakeAuthService:
    """Minimal stand-in for the middleware + permission checks.

    ``check_permission`` answers from the user's own effective rights, so the
    same credentials object drives both the middleware and the route gate.
    """

    def __init__(self, credentials: AuthCredentials) -> None:
        self.credentials = credentials

    async def authenticate_request(self, authorization: str | None) -> AuthCredentials:
        return self.credentials

    async def check_permission(self, credentials: AuthCredentials, permission: Permission) -> bool:
        return credentials.user.has_permission(permission)


def _credentials(
    role: RoleType,
    raised: set[Permission] | None = None,
    denied: set[Permission] | None = None,
) -> AuthCredentials:
    return AuthCredentials(
        user=User(
            user_id="u-1",
            username="ivanov",
            role=role,
            permissions=set(raised or ()),
            denied=set(denied or ()),
        )
    )


@pytest.fixture
def make_client():
    """Factory that builds a TestClient authenticated as a given role.

    Overrides the JWT-parsing dependencies so no token is needed, and stashes a
    matching fake service on app.state for the middleware.
    """
    app = api_app.app
    created: list[TestClient] = []

    def _factory(credentials: AuthCredentials) -> TestClient:
        fake = FakeAuthService(credentials)
        app.state.auth_service = fake
        app.dependency_overrides[get_current_user] = lambda: credentials
        app.dependency_overrides[get_auth_service] = lambda: fake
        client = TestClient(app)
        created.append(client)
        return client

    yield _factory

    app.dependency_overrides.clear()
    if hasattr(app.state, "auth_service"):
        delattr(app.state, "auth_service")


class FakeJobManager:
    def __init__(self, jobs: list[Job]) -> None:
        self.jobs = jobs

    async def get_recent_jobs(self, limit: int = 50) -> list[Job]:
        return self.jobs


def test_auth_users_routes_are_gone(make_client):
    client = make_client(_credentials(RoleType.ADMIN))
    assert client.get("/auth/users").status_code == 404
    assert client.post("/auth/users").status_code in (404, 405)


def test_journal_needs_authentication_only(make_client):
    # API role has no journal-related right — it must still reach the handler.
    client = make_client(_credentials(RoleType.API))
    response = client.get("/journal")
    assert response.status_code not in (401, 403)


def test_feedback_submit_requires_send_permission(make_client):
    client = make_client(_credentials(RoleType.API))  # no feedback:send
    response = client.post("/support/feedback", data={"message": "hello"})
    assert response.status_code == 403
    assert "feedback:send" in response.json()["detail"]


def test_list_jobs_scoped_to_caller(make_client, monkeypatch):
    jobs = [Job(job_id="j-own", user_id="u-1"), Job(job_id="j-other", user_id="u-2")]
    monkeypatch.setattr(
        api_app, "translation_service", SimpleNamespace(job_manager=FakeJobManager(jobs))
    )

    own = make_client(_credentials(RoleType.USER))
    own_ids = {j["job_id"] for j in own.get("/jobs").json()}
    assert own_ids == {"j-own"}

    admin = make_client(_credentials(RoleType.ADMIN))
    admin_ids = {j["job_id"] for j in admin.get("/jobs").json()}
    assert admin_ids == {"j-own", "j-other"}