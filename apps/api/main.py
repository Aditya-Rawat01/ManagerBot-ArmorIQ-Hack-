"""Application factory.

Run locally (from the repository root):

    uvicorn apps.api.main:create_app --factory --reload --port 8000

A factory (instead of a module-level `app`) keeps imports side-effect free:
tests build apps with explicit settings and never read a developer's `.env`.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api import __version__
from apps.api.core.config import Settings, get_settings
from apps.api.core.errors import register_exception_handlers
from apps.api.core.logging import configure_logging
from apps.api.core.middleware import REQUEST_ID_HEADER, RequestIdMiddleware
from apps.api.routes import health
from apps.api.routes.router import api_v1_router

API_V1_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks. Slack Socket Mode is P3-05-owned, D-27 pending.

    The adapter starts only with tokens configured AND an orchestrator handle
    (P3-04, not merged): otherwise the API serves HTTP only and `/health`
    stays green. This block must not grow P3-04's lifespan integration.
    """
    from apps.api.core.db import BackendDatabase, BackendDbNotConfiguredError
    from apps.api.identity import build_member_table
    from services.slack_socket.app import start_slack_socket, stop_slack_socket

    settings: Settings = app.state.settings
    runtime = None
    db = None
    try:
        members = build_member_table(settings)
        if settings.database_url_backend is not None:
            try:
                db = BackendDatabase(settings.database_url_backend.get_secret_value())
            except BackendDbNotConfiguredError:
                db = None
        runtime = start_slack_socket(
            bot_token=_secret(settings.slack_bot_token),
            app_token=_secret(settings.slack_app_token),
            members=members,
            expected_team_id=settings.slack_team_id,
            allowed_channels=set(settings.slack_allowed_channels),
            workspace_id=None,  # resolved by P3-04 wiring; adapter refuses without it
            db=db,
            handle=None,  # P3-04 provides the orchestrator handle when merged
            loop=asyncio.get_running_loop(),  # Bolt threads dispatch onto this loop
        )
        yield
    finally:
        stop_slack_socket(runtime)
        if db is not None:
            db.close()


def _secret(value: object) -> str | None:
    get_value = getattr(value, "get_secret_value", None)
    return get_value() if callable(get_value) else None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="ProxyLead API",
        version=__version__,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings

    register_exception_handlers(app)

    # Starlette runs the most recently added middleware first, so the request
    # ID wraps everything (including CORS preflight responses).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)  # GET /health (platform liveness probes)
    app.include_router(api_v1_router, prefix=API_V1_PREFIX)  # GET /api/v1/health

    return app
