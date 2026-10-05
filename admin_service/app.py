"""Standalone admin service for runtime roles, grants and user management.

Listens on port 8011 by default; docker-compose publishes it to the host on
``127.0.0.1:8011`` so the UI opens in a browser on the Docker host while
staying unreachable from the LAN. ``create_app`` accepts a pre-built container
so the endpoint tests can inject in-memory fakes.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from admin_service.config import AdminConfig
from admin_service.deps import AdminContainer, build_container
from admin_service.routers import (
    access,
    auth_router,
    dashboard,
    docs,
    roles,
    settings,
    users,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    config: AdminConfig | None = None,
    container: AdminContainer | None = None,
) -> FastAPI:
    """Build the admin FastAPI application.

    ``container`` short-circuits the lifespan wiring — used by tests.
    """
    resolved_config = config or AdminConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active = container
        if active is None:
            active = await build_container(resolved_config)
        app.state.admin = active
        logger.info(
            f"Admin service ready (admin='{resolved_config.username}', "
            f"port={resolved_config.port})"
        )
        try:
            yield
        finally:
            if container is None and active is not None:
                await active.close()

    app = FastAPI(
        title="File Translator — Admin",
        description="Runtime roles, collection access and user management",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.include_router(auth_router.router)
    app.include_router(dashboard.router)
    app.include_router(users.router)
    app.include_router(roles.router)
    app.include_router(access.router)
    app.include_router(settings.router)
    app.include_router(docs.router)

    # The documentation page is a real URL so guide sections can be linked and
    # reopened. Declared before the static mount below, which otherwise matches
    # every path and would answer `/docs` with the file `docs/`, which is not a
    # file. The page is a shell; its content comes from the authenticated
    # endpoint behind it.
    @app.get("/docs", include_in_schema=False)
    async def admin_guide_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "docs.html")

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


app = create_app()


def main() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    config = AdminConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    uvicorn.run(
        "admin_service.app:app",
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
