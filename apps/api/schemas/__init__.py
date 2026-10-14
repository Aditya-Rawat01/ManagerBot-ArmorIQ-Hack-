"""HTTP request/response schemas.

Shapes shared with the frontend or MCP server come from `services.contracts`
(single source of truth); backend-only schemas are defined in this package.
"""

from services.contracts import ErrorBody, ErrorResponse, HealthResponse
from services.contracts.api_session import SessionCreateRequest, SessionInfo

__all__ = ["ErrorBody", "ErrorResponse", "HealthResponse", "SessionCreateRequest", "SessionInfo"]
