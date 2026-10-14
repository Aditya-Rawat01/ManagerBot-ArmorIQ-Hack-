"""Application factory.

Run locally (from the repository root):

    uvicorn apps.api.main:create_app --factory --reload --port 8000

A factory (instead of a module-level `app`) keeps imports side-effect free:
tests build apps with explicit settings and never read a developer's `.env`.
"""

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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="ProxyLead API",
        version=__version__,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
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
