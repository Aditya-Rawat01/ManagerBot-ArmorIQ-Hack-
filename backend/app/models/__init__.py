"""Persistence models for backend-owned app metadata (requests, plans, audit,
delivery, unanswered questions). Added with the Neon migrations phase.

The backend database role must never read protected knowledge tables; those
belong to the MCP tool server (see docs/IMPLEMENTATION_CONTRACT.md).
"""
