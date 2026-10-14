"""FastAPI dependencies shared by routers."""

from fastapi import Request

from app.core.config import Settings


def get_app_settings(request: Request) -> Settings:
    """Settings bound to this app instance by `create_app` (test-friendly)."""
    return request.app.state.settings
