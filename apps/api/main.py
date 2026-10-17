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
    """Startup/shutdown hooks: the orchestrator (P3-04), then Slack (P3-05).

    The orchestrator singletons (real ``ArmorIQGateway``, Gemini planner
    and responder clients, backend audit database, D-14 confirmation store)
    are built only when every required setting exists (names in
    ``apps.api.orchestrator.REQUIRED_SETTINGS``); they then set
    ``app.state.orchestrator`` (``confirm`` and ``handle`` for P4-01) and
    ``app.state.orchestrator_handle`` (the Slack ``OrchestratorHandle``),
    plus ``app.state.audit_db`` (the owner-only status reads of P4-01) and the
    web confirmation presenter (the owner-scoped web inbox, P4-01).
    The Slack adapter is stored as ``app.state.slack_runtime`` (None when
    disabled): P4-04's manager health check reads its thread liveness.
    Missing settings are logged by NAME only and the API serves HTTP only.
    Construction makes no network call (clients and the pool are lazy).

    D-27 (RESOLVED by VTCodeCraft, 17 Oct 2026, issue #26): the Slack client
    runs inside this API process, one uvicorn worker (never ``--workers N``:
    one websocket per worker, and the D-14 confirmation store is in-process).
    The adapter starts only with tokens configured AND an orchestrator
    handle: otherwise the API serves HTTP only and `/health` stays green.
    Its sender is handed to the orchestrator's delivery through a
    :class:`SlackSenderSlot`. Neither can fail API startup: any exception
    (an invalid or revoked token makes Bolt's constructor ``auth.test``
    raise; Slack may be unreachable) is logged by exception class and the
    API continues.
    """
    from services.slack_socket.app import stop_slack_socket

    orchestrator, orchestrator_db, sender_slot = _start_orchestrator(app)
    runtime, slack_db = await _start_slack(app, shared_db=orchestrator_db)
    # P4-04: the manager health check reports the socket thread's liveness.
    app.state.slack_runtime = runtime
    if sender_slot is not None and runtime is not None:
        sender_slot.set(getattr(runtime, "sender", None))
    try:
        yield
    finally:
        stop_slack_socket(runtime)
        app.state.slack_runtime = None
        if orchestrator is not None:
            try:
                await orchestrator.aclose()
            except Exception as exc:
                logger.warning("orchestrator shutdown error=%s", type(exc).__name__)
        if sender_slot is not None:
            sender_slot.clear()
        for db in (slack_db, orchestrator_db):
            if db is not None:
                db.close()


def _start_orchestrator(app: FastAPI) -> tuple[Any, Any, Any]:
    """Build the orchestrator if configured. Never raises; no network call.

    Returns ``(orchestrator, db, sender_slot)``, all None when disabled. A
    handle already present on ``app.state`` (tests) is left alone when the
    orchestrator is not configured.
    """
    settings: Settings = app.state.settings
    try:
        from apps.api.orchestrator import (
            SlackSenderSlot,
            build_orchestrator,
            missing_orchestrator_settings,
        )

        missing = missing_orchestrator_settings(settings)
        if missing:
            logger.info(
                "orchestrator disabled (API serves HTTP only): missing settings %s",
                ", ".join(missing),
            )
            return None, None, None
        slot = SlackSenderSlot()
        orchestrator, db = build_orchestrator(settings, sender_provider=slot)
    except Exception as exc:
        logger.error(
            "orchestrator failed to start; API serves HTTP only error=%s", type(exc).__name__
        )
        return None, None, None
    if settings.min_score is None:
        logger.info("MIN_SCORE unset: answers abstain (threshold_unset) until E17 calibrates it")
    from services.contracts import EntryPoint

    # P4-01: D-14 offers for web requests go to the requester's web inbox.
    orchestrator.set_confirmation_presenter(EntryPoint.WEB, orchestrator.web_inbox.put_offer)
    app.state.orchestrator = orchestrator
    app.state.orchestrator_handle = orchestrator.handle
    app.state.audit_db = db
    logger.info("orchestrator ready")
    return orchestrator, db, slot


async def _start_slack(app: FastAPI, shared_db: Any = None) -> tuple[Any, Any]:
    """Start the Socket Mode adapter if configured. Never raises.

    Returns ``(runtime, owned_db)``; both are None when Slack is disabled or
    failed to start. ``shared_db`` (the orchestrator's backend database) is
    reused when given and never closed here; otherwise a backend database is
    opened, and the single workspace ID resolved (``resolve_workspace_id``),
    only when Slack will actually start (tokens AND handle). The blocking
    parts (the workspace query, Bolt's ``auth.test``) run in a worker thread
    so the event loop is never stalled.
    """
    settings: Settings = app.state.settings
    db = None
    owned = None
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
            if shared_db is not None:
                db = shared_db
            elif settings.database_url_backend is not None:
                try:
                    db = owned = BackendDatabase(settings.database_url_backend.get_secret_value())
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
    if runtime is None and owned is not None:
        owned.close()
        owned = None
    return runtime, owned


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
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)  # GET /health (platform liveness probes)
    app.include_router(api_v1_router, prefix=API_V1_PREFIX)  # GET /api/v1/health

    return app
