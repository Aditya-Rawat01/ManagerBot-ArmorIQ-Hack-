"""Knowledge retrieval (Phase 2): hybrid pgvector + full-text search with
hard-coded tier filters. Callable only from the MCP tool server (`services/toolserver`); the backend
never reads protected knowledge directly (ADR-002, ADR-003)."""
