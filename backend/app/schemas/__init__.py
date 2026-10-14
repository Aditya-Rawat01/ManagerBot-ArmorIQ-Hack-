"""HTTP request/response schemas.

Shapes shared with the frontend or MCP server come from `proxylead_contracts`
(single source of truth); backend-only schemas are defined in this package.
"""

from proxylead_contracts import ErrorBody, ErrorResponse, HealthResponse

__all__ = ["ErrorBody", "ErrorResponse", "HealthResponse"]
