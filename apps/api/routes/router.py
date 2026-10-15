"""Versioned API router mounted at /api/v1."""

from fastapi import APIRouter

from apps.api.routes import health, session

api_v1_router = APIRouter()
api_v1_router.include_router(health.router)
api_v1_router.include_router(session.router)
