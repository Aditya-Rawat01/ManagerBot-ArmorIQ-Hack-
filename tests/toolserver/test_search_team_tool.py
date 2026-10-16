"""`knowledge_search_team` tool tests (P2-06). Live tests need a LOCAL
DATABASE_URL_TOOLSERVER with seeded data (see test_tier_filters) and
PROXYLEAD_DB_WRITE_TESTS=1 (each call writes permanent ledger rows); they skip
otherwise. Offline tests inject a fake ledger and pool. DSNs are captured at
collection time and never logged.
"""

import json
import uuid

import pytest
from pydantic import ValidationError

from services.contracts import DataTier, KnowledgeSearchResult, ToolExecutionStatus, ToolName

from .conftest import make_settings
from .test_tool_dispatch import (
    CTX_SECRET,
    FAKE_DSN,
    NEEDS_DB,
    FakeLedger,
    call,
    fake_ledger_client,
    make_ctx,
    result_meta,
    result_text,
)

QUERY = "quokka Friday deploy freeze"


@NEEDS_DB
def test_team_only_results(real_model: object) -> None:
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
def test_confidential_text_unreachable(real_model: object) -> None:
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
    """The real handler with a retrieval call that fails like a database
    outage: an `internal` tool error with an error ledger row, never an
    empty success."""
    from psycopg import OperationalError

    from services.knowledge import retrieval

    calls: list[dict] = []

    def broken(pool: object, **kwargs: object) -> list:  # sync, like the real one
        calls.append(dict(kwargs))
        raise OperationalError("connection to server at 10.0.0.1 failed: secret-host-detail")

    monkeypatch.setattr(retrieval, "search_with_pool", broken)
    ledger = FakeLedger()
    jti = f"j-{uuid.uuid4().hex}"
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(client, {"query": QUERY, "limit": 5, "_ctx": make_ctx(jti=jti)})
    assert calls, "the handler must reach retrieval"
    assert calls[0]["tier"] is DataTier.TEAM
    assert calls[0]["workspace_id"] == "workspace-primary"
    assert result_text(message)["code"] == "internal"
    assert "secret-host-detail" not in json.dumps(message)
    [(entry_id, entry)] = ledger.rows(jti)
    assert entry.execution_status is ToolExecutionStatus.ERROR
    assert entry.error_class == "OperationalError"
    assert result_meta(message) == {"ledger_entry_id": str(entry_id)}


class RecordingPool:
    def __init__(self) -> None:
        self.checkouts = 0

    def connection(self) -> None:  # never reached when embedding fails first
        self.checkouts += 1
        raise AssertionError("no connection may be taken without a query embedding")

    def close(self) -> None:
        pass


def test_search_fails_closed_when_the_model_did_not_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup warm failed: /health is 503 and each search is an `internal`
    error with an error ledger row. The request path never tries to load
    (download) the model and never takes a pooled connection."""
    from fastapi.testclient import TestClient

    from services.knowledge import embeddings
    from services.toolserver import main

    def broken_warm() -> None:
        raise OSError("no cached model and no network")

    loads: list[str] = []
    monkeypatch.setattr(embeddings, "warm", broken_warm)
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_load", lambda: loads.append("load"))
    pool = RecordingPool()
    ledger = FakeLedger()
    monkeypatch.setattr(main.db, "create_pool", lambda dsn, **kwargs: pool)
    monkeypatch.setattr(main, "Ledger", lambda pool: ledger)
    app = main.create_app(make_settings(ctx_secret=CTX_SECRET, database_url_toolserver=FAKE_DSN))
    jti = f"j-{uuid.uuid4().hex}"
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 503
        message = call(client, {"query": QUERY, "limit": 5, "_ctx": make_ctx(jti=jti)})
    assert result_text(message)["code"] == "internal"
    [(entry_id, entry)] = ledger.rows(jti)
    assert entry.execution_status is ToolExecutionStatus.ERROR
    assert entry.error_class == "RetrievalError"
    assert result_meta(message) == {"ledger_entry_id": str(entry_id)}
    assert loads == [] and pool.checkouts == 0


def test_result_contract_guards_tier() -> None:
    with pytest.raises(ValidationError):
        KnowledgeSearchResult(
            tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
            tier=DataTier.CONFIDENTIAL,
            query="q",
            hits=[],
        )
