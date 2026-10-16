"""Application factory.

Run locally (from the repository root):

    uvicorn apps.api.main:create_app --factory --reload --port 8000

A factory (instead of a module-level `app`) keeps imports side-effect free:
tests build apps with explicit settings and never read a developer's `.env`.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

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


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks. Slack Socket Mode is P3-05-owned, D-27 pending.

    The adapter starts only with tokens configured AND an orchestrator handle
    (P3-04, not merged; it sets ``app.state.orchestrator_handle``): otherwise
    the API serves HTTP only and `/health` stays green. Starting Slack can
    never fail API startup: any exception (an invalid or revoked token makes
    Bolt's constructor ``auth.test`` raise; Slack may be unreachable) is
    logged by exception class and the API continues without Slack.
    """
    from services.slack_socket.app import stop_slack_socket

    runtime, db = await _start_slack(app)
    try:
        yield
    finally:
        stop_slack_socket(runtime)
        if db is not None:
            db.close()


async def _start_slack(app: FastAPI) -> tuple[Any, Any]:
    """Start the Socket Mode adapter if configured. Never raises.

    Returns ``(runtime, db)``; both are None when Slack is disabled or failed
    to start. The backend database is opened, and the single workspace ID
    resolved (``resolve_workspace_id``), only when Slack will actually start
    (tokens AND handle). The blocking parts (the workspace query, Bolt's
    ``auth.test``) run in a worker thread so the event loop is never stalled.
    """
    settings: Settings = app.state.settings
    db = None
    try:
        from apps.api.core.db import BackendDatabase, BackendDbNotConfiguredError
        from apps.api.identity import build_member_table
        from services.slack_socket.app import start_slack_socket
        from services.slack_socket.events import resolve_workspace_id

        bot_token = _secret(settings.slack_bot_token)
        app_token = _secret(settings.slack_app_token)
        handle = getattr(app.state, "orchestrator_handle", None)  # P3-04 sets it
        workspace_id = None
        if bot_token and app_token and handle is not None:
            if settings.database_url_backend is not None:
                try:
                    db = BackendDatabase(settings.database_url_backend.get_secret_value())
                except BackendDbNotConfiguredError:
                    db = None
            if db is not None:
                workspace_id = await asyncio.to_thread(resolve_workspace_id, db)
            if workspace_id is None:
                logger.warning(
                    "slack socket mode: no single workspace resolved; "
                    "events will be refused (db_unavailable)"
                )
        runtime = await asyncio.to_thread(
            start_slack_socket,
            bot_token=bot_token,
            app_token=app_token,
            members=build_member_table(settings),
            expected_team_id=settings.slack_team_id,
            allowed_channels=set(settings.slack_allowed_channels),
            workspace_id=workspace_id,
            db=db,
            handle=handle,
            loop=asyncio.get_running_loop(),  # Bolt threads dispatch onto this loop
        )
    except Exception as exc:
        logger.error(
            "slack socket mode failed to start; API continues without Slack error=%s",
            type(exc).__name__,
        )
        runtime = None
    if runtime is None and db is not None:
        db.close()
        db = None
    return runtime, db


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
