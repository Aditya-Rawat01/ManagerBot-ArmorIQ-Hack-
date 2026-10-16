"""Elevated-tier tool tests (P2-08). Ceiling-matrix tests run without a
database (dev path: denial happens before any handler logic); tool-behavior
tests need a LOCAL DATABASE_URL_TOOLSERVER with seeded data and views, plus
PROXYLEAD_DB_WRITE_TESTS=1, and skip otherwise. DSNs captured at collection
time, never logged.
"""

import json
import uuid
from types import SimpleNamespace

import anyio
import pytest

from services.contracts import DataTier, ReadSourceTeamArgs, ToolExecutionStatus, ToolName
from services.toolserver import tools_memory
from services.toolserver.schemas import InvalidArguments

from .test_tool_dispatch import (
    NEEDS_DB,
    FakeLedger,
    call,
    dev_client,
    fake_ledger_client,
    make_ctx,
    result_meta,
    result_text,
)

MEMBERS = {
    "maya": {
        "app_role": "manager",
        "requester_member_id": "m-maya",
        "armoriq_member_email_ref": "PERSONA_MAYA_EMAIL",
    },
    "sam": {
        "app_role": "senior",
        "requester_member_id": "m-sam",
        "armoriq_member_email_ref": "PERSONA_SAM_EMAIL",
    },
    "priya": {
        "app_role": "junior",
        "requester_member_id": "m-priya",
        "armoriq_member_email_ref": "PERSONA_PRIYA_EMAIL",
    },
}

SEARCH_TOOLS = {
    DataTier.TEAM: ToolName.KNOWLEDGE_SEARCH_TEAM.value,
    DataTier.CONFIDENTIAL: ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL.value,
    DataTier.RESTRICTED: ToolName.KNOWLEDGE_SEARCH_RESTRICTED.value,
}
GET_SOURCE_TOOLS = {
    DataTier.TEAM: ToolName.KNOWLEDGE_GET_SOURCE_TEAM.value,
    DataTier.CONFIDENTIAL: ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL.value,
    DataTier.RESTRICTED: ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED.value,
}
READ_SOURCE_TOOLS = {
    DataTier.TEAM: ToolName.READ_SOURCE_TEAM.value,
    DataTier.CONFIDENTIAL: ToolName.READ_SOURCE_CONFIDENTIAL.value,
    DataTier.RESTRICTED: ToolName.READ_SOURCE_RESTRICTED.value,
}
READ_SOURCE_VIEWS = {
    DataTier.TEAM: "approved_company_data_team",
    DataTier.CONFIDENTIAL: "approved_company_data_confidential",
    DataTier.RESTRICTED: "approved_company_data_restricted",
}

# Member ceiling: maya → restricted, sam → confidential, priya → team.
ALLOWED = {
    "maya": {DataTier.TEAM, DataTier.CONFIDENTIAL, DataTier.RESTRICTED},
    "sam": {DataTier.TEAM, DataTier.CONFIDENTIAL},
    "priya": {DataTier.TEAM},
}


def member_ctx(persona: str, tool: str) -> str:
    return make_ctx(tool=tool, **MEMBERS[persona])


def search_args(tool: str) -> dict:
    return {"query": "q", "limit": 3}


def get_source_args(tool: str) -> dict:
    return {"source_ref": "team-friday-deploy-freeze"}


def read_source_args(tool: str) -> dict:
    tier = tool.rsplit("_", 1)[-1]
    return {"view": f"approved_company_data_{tier}", "limit": 5}


def test_ceiling_matrix_without_database() -> None:
    """3 members x 3 tiers x 3 families, dev path (no ledger: allowed calls
    refuse `internal`, denied calls refuse `out_of_scope` — both pre-handler)."""
    families = [
        (SEARCH_TOOLS, search_args),
        (GET_SOURCE_TOOLS, get_source_args),
        (READ_SOURCE_TOOLS, read_source_args),
    ]
    with dev_client() as client:
        for persona, tiers in ALLOWED.items():
            for tools, build_args in families:
                for tier, tool in tools.items():
                    arguments = build_args(tool)
                    arguments["_ctx"] = member_ctx(persona, tool)
                    code = result_text(call(client, arguments, tool=tool))["code"]
                    assert code == ("internal" if tier in tiers else "out_of_scope"), (
                        persona,
                        tool,
                        code,
                    )


@NEEDS_DB
def test_confidential_search_returns_only_confidential(real_model: object) -> None:
    from .test_tool_dispatch import live_client as live

    with live() as client:
        message = call(
            client,
            {
                "query": "runway review",
                "limit": 5,
                "_ctx": member_ctx("maya", SEARCH_TOOLS[DataTier.CONFIDENTIAL]),
            },
            tool=SEARCH_TOOLS[DataTier.CONFIDENTIAL],
        )
    assert message["result"]["isError"] is False
    ids = [
        hit["source"]["source_id"]
        for hit in json.loads(message["result"]["content"][0]["text"])["hits"]
    ]
    assert "conf-runway-review" in ids
    assert "team-friday-deploy-freeze" not in ids


@NEEDS_DB
def test_restricted_search_returns_only_restricted(real_model: object) -> None:
    from .test_tool_dispatch import live_client as live

    with live() as client:
        message = call(
            client,
            {
                "query": "workforce planning",
                "limit": 5,
                "_ctx": member_ctx("maya", SEARCH_TOOLS[DataTier.RESTRICTED]),
            },
            tool=SEARCH_TOOLS[DataTier.RESTRICTED],
        )
    assert message["result"]["isError"] is False
    body = json.loads(message["result"]["content"][0]["text"])
    assert body["hits"]
    assert {hit["source"]["tier"] for hit in body["hits"]} == {"restricted"}


@NEEDS_DB
def test_get_source_per_tier() -> None:
    from .test_tool_dispatch import live_client as live

    refs = {
        DataTier.TEAM: "team-friday-deploy-freeze",
        DataTier.CONFIDENTIAL: "conf-runway-review",
        DataTier.RESTRICTED: "restr-workforce-planning",
    }
    with live() as client:
        for tier, ref in refs.items():
            tool = GET_SOURCE_TOOLS[tier]
            message = call(client, {"source_ref": ref, "_ctx": member_ctx("maya", tool)}, tool=tool)
            assert message["result"]["isError"] is False, (tier, message)
            body = json.loads(message["result"]["content"][0]["text"])
            assert body["source"]["source_id"] == ref
            assert body["source"]["tier"] == tier.value
            assert body["excerpt"]


@NEEDS_DB
def test_read_source_per_tier_with_limits() -> None:
    from .test_tool_dispatch import live_client as live

    with live() as client:
        for tier, tool in READ_SOURCE_TOOLS.items():
            message = call(
                client,
                {"view": READ_SOURCE_VIEWS[tier], "limit": 50, "_ctx": member_ctx("maya", tool)},
                tool=tool,
            )
            assert message["result"]["isError"] is False, (tier, message)
            body = json.loads(message["result"]["content"][0]["text"])
            assert body["row_count"] >= len(body["rows"]) > 0
            assert all(len(row["snippet"]) <= 2000 for row in body["rows"])
        over = call(
            client,
            {
                "view": READ_SOURCE_VIEWS[DataTier.TEAM],
                "limit": 51,
                "_ctx": member_ctx(
                    "maya",
                    READ_SOURCE_TOOLS[DataTier.TEAM],
                ),
            },
            tool=READ_SOURCE_TOOLS[DataTier.TEAM],
        )
        assert result_text(over)["code"] == "invalid_arguments"


@NEEDS_DB
def test_sam_confidential_allowed_restricted_denied(real_model: object) -> None:
    from .test_tool_dispatch import live_client as live

    with live() as client:
        ok_tool = SEARCH_TOOLS[DataTier.CONFIDENTIAL]
        ok = call(
            client,
            {"query": "runway", "limit": 3, "_ctx": member_ctx("sam", ok_tool)},
            tool=ok_tool,
        )
        assert ok["result"]["isError"] is False
        denied_tool = SEARCH_TOOLS[DataTier.RESTRICTED]
        denied = call(
            client,
            {"query": "workforce", "limit": 3, "_ctx": member_ctx("sam", denied_tool)},
            tool=denied_tool,
        )
        assert result_text(denied)["code"] == "out_of_scope"


# --- include_superseded fails closed (D-09 rule 3; SourceRow has no status) ----


def test_include_superseded_is_rejected_before_any_handler() -> None:
    """`include_superseded=True` is `invalid_arguments` for every read tool
    (dispatch step 1.2); the same call with False reaches the no-ledger
    refusal, so only the argument policy can produce the rejection."""
    with dev_client() as client:
        for tier, tool in READ_SOURCE_TOOLS.items():
            base = {"view": READ_SOURCE_VIEWS[tier], "limit": 5}
            rejected = call(
                client,
                {**base, "include_superseded": True, "_ctx": member_ctx("maya", tool)},
                tool=tool,
            )
            assert result_text(rejected)["code"] == "invalid_arguments", tool
            allowed = call(
                client,
                {**base, "include_superseded": False, "_ctx": member_ctx("maya", tool)},
                tool=tool,
            )
            assert result_text(allowed)["code"] == "internal", tool


def test_include_superseded_rejection_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorded as an `invalid_arguments` rejection row; the real handler never
    runs (the fake pool would fail it)."""
    tool = READ_SOURCE_TOOLS[DataTier.TEAM]
    ledger = FakeLedger()
    jti = f"jti-sup-{uuid.uuid4().hex}"
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(
            client,
            {
                "view": READ_SOURCE_VIEWS[DataTier.TEAM],
                "limit": 5,
                "include_superseded": True,
                "_ctx": make_ctx(jti=jti, tool=tool, **MEMBERS["maya"]),
            },
            tool=tool,
        )
    assert result_text(message)["code"] == "invalid_arguments"
    [(entry_id, entry)] = ledger.rows(jti)
    assert result_meta(message) == {"ledger_entry_id": str(entry_id)}
    assert entry.validation_outcome == "invalid_arguments"
    assert entry.execution_status is ToolExecutionStatus.REJECTED_BY_TOOL_SERVER


def test_read_source_handler_refuses_include_superseded_directly() -> None:
    """Defense in depth: a direct handler call (no dispatcher) fails closed."""
    args = ReadSourceTeamArgs(view="approved_company_data_team", limit=5, include_superseded=True)
    runtime = SimpleNamespace(pool=object())
    with pytest.raises(InvalidArguments):
        anyio.run(tools_memory.handle_read_source_team, None, args, runtime)


@NEEDS_DB
def test_read_source_never_returns_superseded_rows() -> None:
    """The seeded superseded team project (proj-compass) never comes back."""
    from .test_tool_dispatch import live_client as live

    tool = READ_SOURCE_TOOLS[DataTier.TEAM]
    with live() as client:
        message = call(
            client,
            {
                "view": READ_SOURCE_VIEWS[DataTier.TEAM],
                "limit": 50,
                "_ctx": member_ctx("maya", tool),
            },
            tool=tool,
        )
    assert message["result"]["isError"] is False, message
    body = json.loads(message["result"]["content"][0]["text"])
    ids = {row["source_id"] for row in body["rows"]}
    assert "proj-atlas" in ids
    assert "proj-compass" not in ids
    assert body["row_count"] == len(body["rows"])
    assert not any("SUPERSEDED" in row["snippet"] for row in body["rows"])
