"""Dashboard, audit feed and health check (tasks 5.2, 5.7).

``/api/health`` is intentionally unauthenticated so container orchestration can
probe it; it only exposes counts and the collection list.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from admin_service.deps import AdminContainer, get_container, get_current_admin
from admin_service.schemas import AuditEvent, DashboardOut, HealthOut

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
