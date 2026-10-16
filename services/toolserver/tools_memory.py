"""`memory-mcp` product tools (P2-06; module name per ADR-027).

Each entry in PRODUCT_TOOLS pairs a tool name with its handler. Handlers run
inside the guarded dispatcher (`servers.py`), never directly: they receive the
verified call and the validated P2-02 arguments, and return the P2-02 result
model. P2-10 reuses this pattern for `workspace-mcp` (it owns
`tools_workspace.py`, not this file).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

import anyio.to_thread
from pydantic import BaseModel

from services.contracts import (
    DataTier,
    KnowledgeSearchResult,
    KnowledgeSearchTeamArgs,
    ToolName,
)
from services.knowledge import retrieval
from services.toolserver.security import VerifiedCall


class _NoDatabase(Exception):
    """No database pool is configured; product tools fail closed."""


async def handle_knowledge_search_team(
    verified: VerifiedCall,
    args: KnowledgeSearchTeamArgs,
    runtime: Any,
) -> KnowledgeSearchResult:
    """Run team-tier search. `DataTier.TEAM` is a literal constant (R-22)."""
    if runtime.pool is None:
        raise _NoDatabase("knowledge search needs a database pool")
    hits = await anyio.to_thread.run_sync(
        partial(
            retrieval.search_with_pool,
            runtime.pool,
            tier=DataTier.TEAM,
            workspace_id=verified.ctx.workspace_id,
            query=args.query,
            limit=args.limit,
        )
    )
    return KnowledgeSearchResult(
        tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
        tier=DataTier.TEAM,
        query=args.query,
        hits=hits,
        min_score=None,
    )


@dataclass(frozen=True)
class ProductTool:
    name: ToolName
    description: str
    handler: Callable[..., Awaitable[BaseModel]]


PRODUCT_TOOLS: dict[ToolName, ProductTool] = {
    ToolName.KNOWLEDGE_SEARCH_TEAM: ProductTool(
        name=ToolName.KNOWLEDGE_SEARCH_TEAM,
        description=(
            "Search team-tier knowledge the requester is allowed to see. "
            "Returns matching sources with citations."
        ),
        handler=handle_knowledge_search_team,
    ),
}
