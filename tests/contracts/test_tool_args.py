"""Strict per-tool argument/result contracts (P2-02).

Offline and deterministic: no ArmorIQ, database, or Slack calls. Secret-like
values used here are obviously-fake test constants, never real credentials.
"""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from services.contracts import (
    FORBIDDEN_ARGUMENT_KEYS,
    TOOL_ARGUMENT_MODELS,
    TOOL_CATALOG,
    TOOL_RESULT_MODELS,
    DataTier,
    KnowledgeSearchResult,
    ReviewStatus,
    SourceCitation,
    ToolName,
    validate_arguments,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
_FAKE_SECRET = "sk-fake-test-secret-0000"

VALID_ARGS: dict[ToolName, dict[str, object]] = {
    ToolName.KNOWLEDGE_SEARCH_TEAM: {"query": "atlas budget", "limit": 5},
    ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: {"query": "atlas budget", "limit": 5},
    ToolName.KNOWLEDGE_SEARCH_RESTRICTED: {"query": "atlas budget", "limit": 5},
    ToolName.KNOWLEDGE_GET_SOURCE_TEAM: {"source_ref": "src-001"},
    ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL: {"source_ref": "src-001"},
    ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED: {"source_ref": "src-001"},
    ToolName.READ_SOURCE_TEAM: {"view": "approved_company_data_team", "limit": 5},
    ToolName.READ_SOURCE_CONFIDENTIAL: {
        "view": "approved_company_data_confidential",
        "limit": 5,
    },
    ToolName.READ_SOURCE_RESTRICTED: {
        "view": "approved_company_data_restricted",
        "limit": 5,
    },
    ToolName.KNOWLEDGE_SAVE_TEAM_NOTE: {"content": "standup moved", "content_kind": "note"},
    ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION: {"record_id": "rec-1", "version": 2},
    ToolName.KNOWLEDGE_MARK_SUPERSEDED: {
        "record_id": "rec-1",
        "version": 1,
        "superseded_by_record_id": "rec-2",
        "reason": "outdated",
    },
    ToolName.CREATE_FOLLOWUP_TASK: {
        "title": "Follow up on hiring",
        "owner": "maya",
        "priority": "high",
        "idempotency_key": "task-1",
    },
    ToolName.SEND_EXTERNAL_MESSAGE: {"destination": "synthetic_sink", "message": "ping"},
}

# One invalid payload per tool (wrong type, out of bounds, or disallowed value).
INVALID_ARGS: dict[ToolName, dict[str, object]] = {
    ToolName.KNOWLEDGE_SEARCH_TEAM: {"query": "x" * 501, "limit": 5},
    ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: {"query": "q", "limit": 0},
    ToolName.KNOWLEDGE_SEARCH_RESTRICTED: {"query": "q", "limit": 51},
    ToolName.KNOWLEDGE_GET_SOURCE_TEAM: {"source_ref": ""},
    ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL: {"source_ref": "x" * 129},
    ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED: {},
    ToolName.READ_SOURCE_TEAM: {"view": "approved_company_data_team", "limit": 0},
    ToolName.READ_SOURCE_CONFIDENTIAL: {"view": "other_schema.team", "limit": 5},
    ToolName.READ_SOURCE_RESTRICTED: {"view": "approved_company_data_restricted"},
    ToolName.KNOWLEDGE_SAVE_TEAM_NOTE: {"content": "", "content_kind": "note"},
    ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION: {"record_id": "rec-1", "version": 0},
    ToolName.KNOWLEDGE_MARK_SUPERSEDED: {
        "record_id": "rec-1",
        "version": 1,
        "superseded_by_record_id": "rec-2",
        "reason": "",
    },
    ToolName.CREATE_FOLLOWUP_TASK: {
        "title": "t",
        "owner": "mallory",
        "priority": "high",
        "idempotency_key": "k",
    },
    ToolName.SEND_EXTERNAL_MESSAGE: {"destination": "ops@example.com", "message": "ping"},
}


def _citation(tier: DataTier) -> SourceCitation:
    return SourceCitation(
        source_id="src-001",
        title="Atlas budget",
        tier=tier,
        author="Maya",
        created_at=NOW,
        version=1,
        review_status=ReviewStatus.DRAFT,
    )


# --- registry ---------------------------------------------------------------


def test_every_tool_has_models() -> None:
    assert set(TOOL_ARGUMENT_MODELS) == set(ToolName)
    assert set(TOOL_RESULT_MODELS) == set(ToolName)
    assert set(TOOL_CATALOG) == set(ToolName)
    assert len(TOOL_ARGUMENT_MODELS) == len(TOOL_RESULT_MODELS) == 14
    for model in (*TOOL_ARGUMENT_MODELS.values(), *TOOL_RESULT_MODELS.values()):
        assert isinstance(model, type) and issubclass(model, BaseModel)


def test_search_results_reuse_shared_model() -> None:
    for tool in (
        ToolName.KNOWLEDGE_SEARCH_TEAM,
        ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
        ToolName.KNOWLEDGE_SEARCH_RESTRICTED,
    ):
        assert TOOL_RESULT_MODELS[tool] is KnowledgeSearchResult


def test_models_reject_unknown_fields_by_default() -> None:
    for model in (*TOOL_ARGUMENT_MODELS.values(), *TOOL_RESULT_MODELS.values()):
        assert model.model_config.get("extra") == "forbid"
        assert model.model_config.get("frozen") is True


def test_no_forbidden_or_underscore_fields() -> None:
    # Argument models: no caller-supplied control field at all. Result models:
    # `tier` is allowed because it is set by the server, never the caller
    # (KnowledgeSearchResult precedent, P2-02 §9); every other key stays banned.
    for tool, model in TOOL_ARGUMENT_MODELS.items():
        for name in model.model_fields:
            assert not name.startswith("_"), f"{tool.value}.{name}"
            assert name not in FORBIDDEN_ARGUMENT_KEYS, f"{tool.value}.{name}"
    for tool, model in TOOL_RESULT_MODELS.items():
        for name in model.model_fields:
            assert not name.startswith("_"), f"{tool.value}.{name}"
            if name != "tier":
                assert name not in FORBIDDEN_ARGUMENT_KEYS, f"{tool.value}.{name}"


# --- argument validation ----------------------------------------------------


@pytest.mark.parametrize("tool", list(ToolName))
def test_valid_arguments_accepted(tool: ToolName) -> None:
    validated = validate_arguments(tool, VALID_ARGS[tool])
    assert isinstance(validated, TOOL_ARGUMENT_MODELS[tool])


@pytest.mark.parametrize("tool", list(ToolName))
def test_unknown_keys_rejected(tool: ToolName) -> None:
    with pytest.raises(ValidationError):
        validate_arguments(tool, {**VALID_ARGS[tool], "bogus_key_xyz": 1})


@pytest.mark.parametrize("tool", list(ToolName))
def test_tier_argument_rejected(tool: ToolName) -> None:
    with pytest.raises(ValidationError):
        validate_arguments(tool, {**VALID_ARGS[tool], "tier": "team"})


@pytest.mark.parametrize("tool", list(ToolName))
def test_bounds_enforced(tool: ToolName) -> None:
    with pytest.raises(ValidationError):
        validate_arguments(tool, INVALID_ARGS[tool])


@pytest.mark.parametrize(
    "key", [*sorted(FORBIDDEN_ARGUMENT_KEYS), "_ctx", "_approvals", "_policy_context"]
)
def test_forbidden_and_underscore_keys_rejected_on_search(key: str) -> None:
    with pytest.raises(ValidationError):
        validate_arguments(ToolName.KNOWLEDGE_SEARCH_TEAM, {"query": "q", "limit": 1, key: "x"})


def test_frozen_models_reject_assignment() -> None:
    validated = validate_arguments(ToolName.KNOWLEDGE_SEARCH_TEAM, {"query": "q", "limit": 1})
    with pytest.raises(ValidationError):
        validated.query = "other"  # type: ignore[misc]


# --- family-specific rules --------------------------------------------------


def test_content_kind_enum() -> None:
    for kind in ("note", "summary"):
        validated = validate_arguments(
            ToolName.KNOWLEDGE_SAVE_TEAM_NOTE, {"content": "c", "content_kind": kind}
        )
        assert validated.content_kind == kind
    with pytest.raises(ValidationError):
        validate_arguments(
            ToolName.KNOWLEDGE_SAVE_TEAM_NOTE, {"content": "c", "content_kind": "decision"}
        )


def test_confirm_requires_version() -> None:
    validated = validate_arguments(
        ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION, {"record_id": "rec-1", "version": 3}
    )
    assert validated.version == 3
    for bad_version in (0, -1):
        with pytest.raises(ValidationError):
            validate_arguments(
                ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION,
                {"record_id": "rec-1", "version": bad_version},
            )


def test_search_query_and_limit_bounds() -> None:
    validate_arguments(ToolName.KNOWLEDGE_SEARCH_TEAM, {"query": "q", "limit": 1})
    validate_arguments(ToolName.KNOWLEDGE_SEARCH_TEAM, {"query": "q" * 500, "limit": 50})
    for bad in ({"query": "", "limit": 1}, {"query": "q", "limit": 51}):
        with pytest.raises(ValidationError):
            validate_arguments(ToolName.KNOWLEDGE_SEARCH_TEAM, bad)


def test_honeypot_accepts_only_synthetic_sink() -> None:
    validated = validate_arguments(ToolName.SEND_EXTERNAL_MESSAGE, {"message": "ping"})
    assert validated.destination == "synthetic_sink"
    for bad_destination in (
        "ops@example.com",
        "https://evil.example/hook",
        "external",
        "#general",
    ):
        with pytest.raises(ValidationError):
            validate_arguments(
                ToolName.SEND_EXTERNAL_MESSAGE,
                {"destination": bad_destination, "message": "ping"},
            )
    with pytest.raises(ValidationError):
        validate_arguments(
            ToolName.SEND_EXTERNAL_MESSAGE,
            {"destination": "synthetic_sink", "recipient": "ops@example.com"},
        )


def test_followup_owner_and_priority_allowlisted() -> None:
    validated = validate_arguments(
        ToolName.CREATE_FOLLOWUP_TASK,
        {
            "title": "t",
            "owner": "sam",
            "priority": "low",
            "idempotency_key": "k-1",
        },
    )
    assert validated.owner == "sam"
    with pytest.raises(ValidationError):
        validate_arguments(
            ToolName.CREATE_FOLLOWUP_TASK,
            {"title": "t", "owner": "sam", "priority": "urgent", "idempotency_key": "k"},
        )


def test_view_allowlist_per_tier() -> None:
    validated = validate_arguments(
        ToolName.READ_SOURCE_TEAM, {"view": "approved_company_data_team", "limit": 1}
    )
    assert validated.view == "approved_company_data_team"
    with pytest.raises(ValidationError):
        validate_arguments(
            ToolName.READ_SOURCE_TEAM,
            {"view": "approved_company_data_confidential", "limit": 1},
        )


# --- result models ----------------------------------------------------------


def test_get_source_result_envelope() -> None:
    model = TOOL_RESULT_MODELS[ToolName.KNOWLEDGE_GET_SOURCE_TEAM]
    result = model(
        tool=ToolName.KNOWLEDGE_GET_SOURCE_TEAM,
        tier=DataTier.TEAM,
        source=_citation(DataTier.TEAM),
        excerpt="snippet",
    )
    assert result.source.source_id == "src-001"
    with pytest.raises(ValidationError):
        model(
            tool=ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL,
            tier=DataTier.TEAM,
            source=_citation(DataTier.TEAM),
            excerpt="snippet",
        )
    with pytest.raises(ValidationError):
        model(
            tool=ToolName.KNOWLEDGE_GET_SOURCE_TEAM,
            tier=DataTier.CONFIDENTIAL,
            source=_citation(DataTier.TEAM),
            excerpt="snippet",
        )


def test_read_source_row_count_rule() -> None:
    model = TOOL_RESULT_MODELS[ToolName.READ_SOURCE_TEAM]
    row = {"source_id": "s-1", "title": "t", "snippet": "s"}
    ok = model(
        tool=ToolName.READ_SOURCE_TEAM,
        tier=DataTier.TEAM,
        view="approved_company_data_team",
        rows=[row],
        row_count=3,
        truncated=True,
    )
    assert ok.row_count == 3
    with pytest.raises(ValidationError, match="row_count"):
        model(
            tool=ToolName.READ_SOURCE_TEAM,
            tier=DataTier.TEAM,
            view="approved_company_data_team",
            rows=[row],
            row_count=0,
            truncated=False,
        )


def test_maintenance_results_carry_review_status() -> None:
    note = TOOL_RESULT_MODELS[ToolName.KNOWLEDGE_SAVE_TEAM_NOTE](
        tool=ToolName.KNOWLEDGE_SAVE_TEAM_NOTE,
        record_id="rec-1",
        version=1,
        review_status="draft",
    )
    assert note.review_status == "draft"
    confirm = TOOL_RESULT_MODELS[ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION](
        tool=ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION,
        record_id="rec-1",
        version=2,
        review_status="authoritative",
    )
    assert confirm.review_status == "authoritative"


def test_honeypot_result_never_delivered() -> None:
    model = TOOL_RESULT_MODELS[ToolName.SEND_EXTERNAL_MESSAGE]
    result = model(
        tool=ToolName.SEND_EXTERNAL_MESSAGE,
        delivered=False,
        sink="synthetic_sink",
        reason="policy block",
    )
    assert result.delivered is False


# --- error sanitization -----------------------------------------------------


def test_validation_errors_do_not_leak_values() -> None:
    secret_query = (_FAKE_SECRET + "-") * 30  # overlong and secret-bearing
    with pytest.raises(ValidationError) as exc_info:
        validate_arguments(ToolName.KNOWLEDGE_SEARCH_TEAM, {"query": secret_query, "limit": 5})
    assert _FAKE_SECRET not in str(exc_info.value)
    assert _FAKE_SECRET not in exc_info.value.json()
    assert "query" in str(exc_info.value)  # the field is still identified


def test_unknown_tool_is_programmer_error() -> None:
    with pytest.raises(KeyError):
        validate_arguments("not_a_tool", {"query": "q"})  # type: ignore[arg-type]
