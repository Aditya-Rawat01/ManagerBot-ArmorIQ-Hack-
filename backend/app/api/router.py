"""Versioned API router mounted at /api/v1."""

from fastapi import APIRouter

from app.api.routes import health

api_v1_router = APIRouter()
api_v1_router.include_router(health.router)
