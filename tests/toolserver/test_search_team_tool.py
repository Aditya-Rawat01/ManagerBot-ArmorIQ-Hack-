"""`knowledge_search_team` tool tests (P2-06). Needs a LOCAL
DATABASE_URL_TOOLSERVER with seeded data (see test_tier_filters); skips
otherwise. DSNs are captured at collection time and never logged.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from services.contracts import DataTier, KnowledgeSearchResult, ToolName
from services.toolserver.main import create_app

from .conftest import make_settings
from .test_tool_dispatch import CTX_SECRET, NEEDS_DB, call, make_ctx

QUERY = "quokka Friday deploy freeze"


@NEEDS_DB
def test_team_only_results() -> None:
    from .test_tool_dispatch import live_client

    with live_client() as client:
        message = call(client, {"query": QUERY, "limit": 5, "_ctx": make_ctx()})
    assert message["result"]["isError"] is False
    result = KnowledgeSearchResult.model_validate(
        json.loads(message["result"]["content"][0]["text"])
    )
    assert result.tool == ToolName.KNOWLEDGE_SEARCH_TEAM
    assert result.tier is DataTier.TEAM
    assert result.query == QUERY
    assert result.hits and result.hits[0].source.source_id == "team-friday-deploy-freeze"
    assert all(hit.source.tier is DataTier.TEAM for hit in result.hits)


@NEEDS_DB
def test_confidential_text_unreachable() -> None:
    # Fused path: vector-similar TEAM rows may appear, but the confidential
    # record itself never does (tier purity, not emptiness, is the property).
    from .test_tool_dispatch import live_client

    with live_client() as client:
        message = call(
            client,
            {"query": "runway review", "limit": 5, "_ctx": make_ctx(jti=f"j-{uuid.uuid4().hex}")},
        )
    assert message["result"]["isError"] is False
    hits = json.loads(message["result"]["content"][0]["text"])["hits"]
    assert all(hit["source"]["tier"] == "team" for hit in hits)
    assert "conf-runway-review" not in [hit["source"]["source_id"] for hit in hits]


def test_db_error_is_a_tool_error_not_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.knowledge import retrieval

    async def broken(*args: object, **kwargs: object):
        from services.knowledge.retrieval import RetrievalError

        raise RetrievalError("database down")

    monkeypatch.setattr(retrieval, "search_with_pool", broken)
    app = create_app(make_settings(ctx_secret=CTX_SECRET))
    with TestClient(app, raise_server_exceptions=False) as client:
        message = call(
            client, {"query": QUERY, "limit": 5, "_ctx": make_ctx(jti=f"j-{uuid.uuid4().hex}")}
        )
    assert message["result"]["isError"] is True
    assert json.loads(message["result"]["content"][0]["text"])["code"] == "internal"


def test_result_contract_guards_tier() -> None:
    with pytest.raises(ValidationError):
        KnowledgeSearchResult(
            tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
            tier=DataTier.CONFIDENTIAL,
            query="q",
            hits=[],
        )
