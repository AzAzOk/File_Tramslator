"""Dashboard, audit feed and health check (tasks 5.2, 5.7).

``/api/health`` is intentionally unauthenticated so container orchestration can
probe it; it only exposes counts and the collection list.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Query

from admin_service.deps import AdminContainer, get_container, get_current_admin
from admin_service.schemas import (
    AuditEvent,
    DashboardOut,
    HealthOut,
    JobMonitoringOut,
    MonitoredJob,
    MonitoredLogEntry,
    MonitoringHistoryOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])


async def _counts(container: AdminContainer) -> dict[str, int]:
    users = await container.user_repo.list_all()
    roles = await container.role_store.get_all_roles()
    grants = await container.grant_store.get_all_grants()
    return {
        "users": len(users),
        "active_users": sum(1 for u in users if u.is_active),
        "manual_roles": sum(1 for u in users if u.manual_role),
        "roles": len(roles),
        "custom_roles": sum(1 for r in roles if not r.builtin),
        "grants": len(grants),
    }


@router.get("/health", response_model=HealthOut)
async def health(container: AdminContainer = Depends(get_container)) -> HealthOut:
    """Counts from Mongo and the collection list from MySQL."""
    try:
        counts = await _counts(container)
    except Exception:
        logger.warning("Health check: Mongo unavailable", exc_info=True)
        counts = {}
    try:
        collections = await container.collection_source.list_collections()
    except Exception:
        logger.warning("Health check: MySQL unavailable", exc_info=True)
        collections = []
    return HealthOut(
        status="ok" if counts else "degraded",
        mongo=counts,
        collections=collections,
        config_version=await container.config_version.get_version() or 0,
    )


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> DashboardOut:
    """Counters plus the most recent audit records."""
    events = await container.event_repo.list_recent(limit=20)
    return DashboardOut(
        counts=await _counts(container),
        collections=await container.collection_source.list_collections(),
        config_version=await container.config_version.get_version() or 0,
        recent_events=[AuditEvent(**e) for e in events],
    )


@router.get("/audit", response_model=list[AuditEvent])
async def audit_feed(
    limit: int = Query(default=50, ge=1, le=500),
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> list[AuditEvent]:
    """Audit records, newest first."""
    return [AuditEvent(**e) for e in await container.event_repo.list_recent(limit=limit)]


@router.get("/jobs", response_model=JobMonitoringOut)
async def jobs_feed(
    limit: int = Query(default=50, ge=1, le=200),
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> JobMonitoringOut:
    """Read-only monitoring feed of recent jobs across all users.

    Proxies the main API's ``/internal/jobs`` and resolves each job's owner id
    to a user name. When the main API is unreachable the block still renders —
    ``available`` is False and the list is empty rather than a 500.
    """
    try:
        response = await container.http_client.get(
            "/internal/jobs", params={"limit": limit}
        )
    except httpx.HTTPError as exc:
        logger.warning(f"Job monitoring feed unavailable: {exc}")
        return JobMonitoringOut(available=False, jobs=[])
    if response.status_code >= 400:
        logger.warning(
            f"Job monitoring feed returned {response.status_code}: {response.text}"
        )
        return JobMonitoringOut(available=False, jobs=[])

    raw = response.json() if response.content else []
    owners = await _owner_names(container, [j.get("user_id", "") for j in raw])
    return JobMonitoringOut(
        available=True,
        jobs=[
            MonitoredJob(
                job_id=j.get("job_id", ""),
                owner=owners.get(j.get("user_id", ""), j.get("user_id", "")),
                created_at=j.get("created_at", ""),
                filename=j.get("filename", ""),
                status=j.get("status", ""),
                format=j.get("format", ""),
            )
            for j in raw
        ],
    )


@router.get("/monitoring/history", response_model=MonitoringHistoryOut)
async def monitoring_history(
    limit: int = Query(default=100, ge=1, le=500),
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> MonitoringHistoryOut:
    """Read-only durable monitoring history, newest first.

    Proxies the main API's ``/internal/monitoring`` and resolves each entry's
    owner id to a user name. Unlike ``/jobs`` this survives the live job store's
    short retention, so a completed-then-deleted job is still listed. When the
    main API is unreachable the block still renders — ``available`` is False and
    the list is empty rather than a 500.
    """
    try:
        response = await container.http_client.get(
            "/internal/monitoring", params={"limit": limit}
        )
    except httpx.HTTPError as exc:
        logger.warning(f"Monitoring history unavailable: {exc}")
        return MonitoringHistoryOut(available=False, entries=[])
    if response.status_code >= 400:
        logger.warning(
            f"Monitoring history returned {response.status_code}: {response.text}"
        )
        return MonitoringHistoryOut(available=False, entries=[])

    raw = response.json() if response.content else []
    owners = await _owner_names(container, [e.get("owner_id", "") for e in raw])
    return MonitoringHistoryOut(
        available=True,
        entries=[
            MonitoredLogEntry(
                job_id=e.get("job_id", ""),
                owner=owners.get(e.get("owner_id", ""), e.get("owner_id", "")),
                timestamp=e.get("timestamp", ""),
                filename=e.get("filename", ""),
                status=e.get("status", ""),
                format=e.get("format", ""),
            )
            for e in raw
        ],
    )


async def _owner_names(
    container: AdminContainer, user_ids: list[str]
) -> dict[str, str]:
    """Resolve user ids to display names, falling back to the raw id.

    Iterates the users known to the admin store rather than hitting the main API
    per row; an id missing from the store simply maps to itself.
    """
    wanted = {uid for uid in user_ids if uid}
    if not wanted:
        return {}
    resolved: dict[str, str] = {}
    for user in await container.user_repo.list_all():
        if user.user_id in wanted:
            resolved[user.user_id] = user.username or user.user_id
    return resolved
