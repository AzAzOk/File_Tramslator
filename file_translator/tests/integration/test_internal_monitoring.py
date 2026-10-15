"""API-level tests for the internal monitoring-log endpoint and its cleanup task.

Exercises the HTTP contract of ``GET /internal/monitoring`` (token-gated,
newest-first, bounded) and the periodic ``_cleanup_monitoring_logs`` task that
enforces durable-log retention without needing a translation job to run.

No live server: route handlers + dependencies are driven through a ``TestClient``
(no lifespan startup) and the module-level ``translation_service`` singleton is
patched with fakes.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Must be set before importing app (it fails at import without these).
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("GLOSSARY_DB_PASSWORD", "test-pass")

import file_translator.presentation.api.app as api_app
from file_translator.domain.monitoring import MonitoringEntry

from fastapi.testclient import TestClient

_TOKEN = "unit-test-internal-token"


class FakeMonitoringService:
    """Stands in for MonitoringLogService: records calls, returns fixed entries."""

    def __init__(self, entries: list[MonitoringEntry] | None = None) -> None:
        self.entries = entries or []
        self.cleanup_calls = 0
        self.cleanup_result = 0
        self.last_limit: int | None = None

    async def get_recent_entries(self, limit: int = 100) -> list[MonitoringEntry]:
        self.last_limit = limit
        return self.entries[:limit]

    async def cleanup_old_logs(self) -> int:
        self.cleanup_calls += 1
        return self.cleanup_result


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> FakeMonitoringService:
    svc = FakeMonitoringService(
        entries=[
            MonitoringEntry(job_id="b", status="completed", filename="b.docx"),
            MonitoringEntry(job_id="a", status="running", filename="a.docx"),
        ]
    )
    monkeypatch.setattr(
        api_app,
        "translation_service",
        SimpleNamespace(monitoring_log_service=svc),
    )
    return svc


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(api_app, "INTERNAL_API_TOKEN", _TOKEN)
    return TestClient(api_app.app)


def test_returns_entries_with_valid_token(client: TestClient, fake_service: FakeMonitoringService):
    resp = client.get("/internal/monitoring", headers={"X-Internal-Token": _TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert [e["job_id"] for e in body] == ["b", "a"]
    assert body[0]["status"] == "completed"
    assert body[0]["filename"] == "b.docx"


def test_rejects_missing_token(client: TestClient):
    resp = client.get("/internal/monitoring")
    assert resp.status_code == 401


def test_rejects_wrong_token(client: TestClient):
    resp = client.get("/internal/monitoring", headers={"X-Internal-Token": "nope"})
    assert resp.status_code == 401


def test_503_when_token_not_configured(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api_app, "INTERNAL_API_TOKEN", "")
    resp = client.get("/internal/monitoring", headers={"X-Internal-Token": _TOKEN})
    assert resp.status_code == 503


def test_limit_is_clamped_and_forwarded(client: TestClient, fake_service: FakeMonitoringService):
    resp = client.get(
        "/internal/monitoring?limit=99999",
        headers={"X-Internal-Token": _TOKEN},
    )
    assert resp.status_code == 200
    assert fake_service.last_limit == 500

    resp = client.get(
        "/internal/monitoring?limit=0",
        headers={"X-Internal-Token": _TOKEN},
    )
    assert resp.status_code == 200
    assert fake_service.last_limit == 1


@pytest.mark.asyncio
async def test_cleanup_task_invokes_cleanup_without_a_job(
    monkeypatch: pytest.MonkeyPatch, fake_service: FakeMonitoringService
):
    fake_service.cleanup_result = 2
    task = asyncio.create_task(api_app._cleanup_monitoring_logs(interval=0))
    await asyncio.sleep(0.05)
    task.cancel()
    # The task handles cancellation itself (graceful ``break``), so awaiting it
    # completes without raising once the current iteration unwinds.
    await task
    assert fake_service.cleanup_calls >= 1
