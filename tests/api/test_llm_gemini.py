"""Gemini LLM wiring for the planner and responder (ADR-032).

Offline only, with fake LLMs (``tests/fakes/gemini.py``): a fake
``client.aio.models`` returning real SDK response objects, and a real
``google.genai.Client`` whose HTTP goes to an ``httpx.MockTransport``. No
network, no real key, no live model. Nothing here is live evidence of Gemini
behaviour (schema acceptance, latency, or the 2.5 access limits).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

import httpx
import pytest

from apps.api import llm
from apps.api.planner import (
    PLAN_OUTPUT_SCHEMA,
    Failed,
    GeminiPlanClient,
    PlanModelError,
    Planned,
    plan,
    plan_client_from_settings,
)
from apps.api.responder import (
    _DRAFT_SCHEMA,
    GeminiGenerator,
    ModelDraft,
    ToolRequest,
    parse_draft,
    respond,
)
from tests.api.conftest import make_settings
from tests.fakes.gemini import (
    FAKE_GEMINI_KEY,
    FakeGeminiClient,
    gemini_response,
    json_response,
    mock_transport_client,
    response_json,
)
from tests.fakes.planner import (
    FakeModelClient,
    FakeTopicDb,
    conf_rule_db,
    conf_search,
    maya_ctx,
    team_search,
)
from tests.fakes.planner import wire as plan_wire
from tests.fakes.responder import make_ctx as responder_ctx
from tests.fakes.responder import make_item, scripted

GOOD_DRAFT = {
    "answer_text": "The freeze starts Friday [s1].",
    "citations": ["s1"],
    "requested_tools": [{"tool": "send_external_message", "arguments_json": '{"message": "hi"}'}],
}
# Every finish reason except STOP fails closed (a missing reason too).
UNUSABLE_FINISH = [
    "MAX_TOKENS",
    "SAFETY",
    "RECITATION",
    "LANGUAGE",
    "OTHER",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "MALFORMED_FUNCTION_CALL",
    "FINISH_REASON_UNSPECIFIED",
    None,
]


def _api_error(kind: str, code: int) -> Exception:
    from google.genai import errors

    cls = errors.ClientError if kind == "client" else errors.ServerError
    # The details deliberately echo the key: callers must log the class only.
    details = {"error": {"code": code, "message": f"bad key {FAKE_GEMINI_KEY}", "status": "X"}}
    return cls(code, details)


def _failures() -> list[Any]:
    from google.genai import errors

    return [
        pytest.param(lambda: gemini_response("not json"), id="unparsable-json"),
        pytest.param(lambda: json_response({"plan": []}), id="wrong-shape"),
        pytest.param(lambda: json_response(None), id="json-null"),
        pytest.param(lambda: gemini_response("", finish_reason="STOP"), id="empty-text"),
        pytest.param(lambda: gemini_response(None, thought="draft"), id="thought-only"),
        pytest.param(lambda: gemini_response(None, block_reason="SAFETY"), id="prompt-blocked"),
        pytest.param(lambda: gemini_response(None, no_candidates=True), id="no-candidate"),
        pytest.param(
            lambda: gemini_response('{"steps": [', finish_reason="MAX_TOKENS"), id="max-tokens"
        ),
        pytest.param(lambda: gemini_response('{"steps": []}', finish_reason="SAFETY"), id="safety"),
        pytest.param(lambda: _api_error("client", 400), id="client-error"),
        pytest.param(lambda: _api_error("server", 503), id="server-error"),
        pytest.param(lambda: errors.APIError(429, {}), id="api-error"),
        pytest.param(lambda: httpx.ReadTimeout("timed out"), id="read-timeout"),
        pytest.param(lambda: TimeoutError(), id="timeout"),
        pytest.param(lambda: httpx.ConnectError("unreachable"), id="connect-error"),
    ]


# --- usable_text: the fail-closed response mapping --------------------------------


def test_stop_with_text_is_returned_verbatim() -> None:
    assert llm.usable_text(gemini_response('{"steps": []}')) == '{"steps": []}'


@pytest.mark.parametrize("finish_reason", UNUSABLE_FINISH)
def test_any_finish_reason_but_stop_fails_closed(finish_reason: str | None) -> None:
    with pytest.raises(llm.LLMOutputError, match="finish_reason"):
        llm.usable_text(gemini_response('{"steps": []}', finish_reason=finish_reason))


@pytest.mark.parametrize("block_reason", ["SAFETY", "PROHIBITED_CONTENT", "JAILBREAK", "OTHER"])
def test_blocked_prompt_fails_closed(block_reason: str) -> None:
    with pytest.raises(llm.LLMOutputError, match=f"block_reason={block_reason}"):
        llm.usable_text(gemini_response(None, block_reason=block_reason))


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(lambda: gemini_response(None, no_candidates=True), id="no-candidate"),
        pytest.param(lambda: gemini_response(None), id="no-parts"),
        pytest.param(lambda: gemini_response("   "), id="blank-text"),
        pytest.param(lambda: gemini_response(None, thought="secret"), id="thought-only"),
    ],
)
def test_no_answer_text_fails_closed(response: Any) -> None:
    with pytest.raises(llm.LLMOutputError):
        llm.usable_text(response())


def test_thought_parts_never_reach_the_parser() -> None:
    response = gemini_response('{"steps": []}', thought="internal reasoning")
    assert llm.usable_text(response) == '{"steps": []}'


def test_error_messages_carry_labels_only() -> None:
    with pytest.raises(llm.LLMOutputError) as caught:
        llm.usable_text(gemini_response("secret answer text", finish_reason="MAX_TOKENS"))
    assert "secret answer text" not in str(caught.value)


# --- the request: one JSON-mode call, no tools ------------------------------------


def test_generate_json_sends_one_json_mode_call__offline_fake_llm() -> None:
    from google.genai import types

    client = FakeGeminiClient(json_response({"steps": []}))
    text = asyncio.run(
        llm.generate_json(
            client, model="gemini-2.5-flash", system="SYS", user="USER", schema=PLAN_OUTPUT_SCHEMA
        )
    )
    assert json.loads(text) == {"steps": []}
    [call] = client.calls
    assert call["model"] == "gemini-2.5-flash"
    assert call["contents"] == [types.Content(role="user", parts=[types.Part(text="USER")])]
    config = call["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.system_instruction == "SYS"
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema is PLAN_OUTPUT_SCHEMA
    assert config.response_schema is None
    assert config.max_output_tokens == llm.MAX_OUTPUT_TOKENS == 16_000
    assert config.thinking_config.thinking_budget == llm.THINKING_BUDGET == 2_048
    assert config.thinking_config.thinking_level is None  # never both (2.5 rejects levels)
    assert config.tools is None and config.tool_config is None
    assert config.automatic_function_calling.disable is True
    assert config.temperature is None  # model default


def test_wire_request_goes_to_the_developer_api_with_the_key_in_a_header__offline() -> None:
    """The real SDK, HTTP captured by a mock transport (nothing leaves the process)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response_json('{"steps": []}'))

    client = GeminiPlanClient("unused", "gemini-2.5-flash", client=mock_transport_client(handler))
    text = asyncio.run(client.propose_plan(system="SYS", user="USER"))
    assert json.loads(text) == {"steps": []}
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    )
    assert request.headers["x-goog-api-key"] == FAKE_GEMINI_KEY
    assert FAKE_GEMINI_KEY not in str(request.url)
    body = json.loads(request.content)
    assert FAKE_GEMINI_KEY not in request.content.decode()
    assert set(body) == {"contents", "systemInstruction", "generationConfig"}  # no tools
    assert body["contents"] == [{"role": "user", "parts": [{"text": "USER"}]}]
    assert body["systemInstruction"]["parts"] == [{"text": "SYS"}]
    generation = body["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseJsonSchema"] == PLAN_OUTPUT_SCHEMA
    assert generation["maxOutputTokens"] == 16_000
    assert generation["thinkingConfig"] == {"thinking_budget": 2_048}
    assert "responseSchema" not in generation


@pytest.mark.parametrize(("status", "error"), [(400, "ClientError"), (503, "ServerError")])
def test_wire_http_errors_raise_the_sdk_error__offline(status: int, error: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": status, "message": "no"}})

    client = GeminiPlanClient("unused", "gemini-2.5-flash", client=mock_transport_client(handler))
    with pytest.raises(Exception) as caught:
        asyncio.run(client.propose_plan(system="SYS", user="USER"))
    assert type(caught.value).__name__ == error


# --- planner: same plans as before, fail closed otherwise -------------------------


def test_planner_success_matches_the_fake_model_path__offline_fake_llm() -> None:
    """Same model text -> the same validated plan as any other ModelClient."""
    text = plan_wire(team_search(), conf_search())

    async def _scenario() -> tuple[Any, Any]:
        gemini = GeminiPlanClient(
            "unused", "gemini-2.5-flash", client=FakeGeminiClient(gemini_response(text))
        )
        via_gemini = await plan(maya_ctx(), "atlas budget?", model=gemini, db=conf_rule_db())
        via_fake = await plan(
            maya_ctx(), "atlas budget?", model=FakeModelClient([text]), db=conf_rule_db()
        )
        return via_gemini, via_fake

    via_gemini, via_fake = asyncio.run(_scenario())
    assert isinstance(via_gemini, Planned)
    assert via_gemini == via_fake


@pytest.mark.parametrize("outcome", _failures())
def test_planner_fails_closed__offline_fake_llm(
    outcome: Any, caplog: pytest.LogCaptureFixture
) -> None:
    model = GeminiPlanClient(
        FAKE_GEMINI_KEY, "gemini-2.5-flash", client=FakeGeminiClient(outcome())
    )
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(plan(maya_ctx(), "atlas budget?", model=model, db=FakeTopicDb()))
    assert isinstance(result, Failed)  # never a partial plan
    assert FAKE_GEMINI_KEY not in caplog.text
    assert FAKE_GEMINI_KEY not in repr(result)
    assert "atlas budget" not in caplog.text  # no prompt text in logs


def test_unusable_output_is_a_plan_model_error() -> None:
    model = GeminiPlanClient(
        "unused",
        "gemini-2.5-flash",
        client=FakeGeminiClient(gemini_response("{}", finish_reason="SAFETY")),
    )
    with pytest.raises(PlanModelError, match="finish_reason=SAFETY"):
        asyncio.run(model.propose_plan(system="SYS", user="USER"))


def test_wire_timeout_fails_the_plan_closed__offline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    model = GeminiPlanClient("unused", "gemini-2.5-flash", client=mock_transport_client(handler))
    result = asyncio.run(plan(maya_ctx(), "atlas budget?", model=model, db=FakeTopicDb()))
    assert isinstance(result, Failed)


# --- responder: same answers as before, fail closed otherwise ---------------------


def test_responder_success_matches_the_scripted_draft__offline_fake_llm() -> None:
    expected_draft = ModelDraft(
        answer_text=GOOD_DRAFT["answer_text"],
        citations=["s1"],
        requested_tools=[ToolRequest(tool="send_external_message", arguments={"message": "hi"})],
    )
    generator = GeminiGenerator(
        "unused", "gemini-2.5-flash", client=FakeGeminiClient(json_response(GOOD_DRAFT))
    )
    evidence = [make_item("s1")]
    via_gemini = asyncio.run(respond(responder_ctx(), "when?", evidence, 0.5, generate=generator))
    via_draft = asyncio.run(
        respond(responder_ctx(), "when?", evidence, 0.5, generate=scripted(expected_draft))
    )
    assert via_gemini.abstained is False
    assert via_gemini == via_draft


@pytest.mark.parametrize("outcome", _failures())
def test_responder_fails_closed__offline_fake_llm(
    outcome: Any, caplog: pytest.LogCaptureFixture
) -> None:
    generator = GeminiGenerator(
        FAKE_GEMINI_KEY, "gemini-2.5-flash", client=FakeGeminiClient(outcome())
    )
    with caplog.at_level(logging.DEBUG):
        out = asyncio.run(
            respond(responder_ctx(), "when?", [make_item("s1")], 0.5, generate=generator)
        )
    assert out.abstained is True and out.abstain_reason == "error"
    assert out.citations == []  # never a fabricated citation
    assert FAKE_GEMINI_KEY not in caplog.text and FAKE_GEMINI_KEY not in out.model_dump_json()


@pytest.mark.parametrize(
    "payload",
    [
        {**GOOD_DRAFT, "notes": "extra"},
        {**GOOD_DRAFT, "citations": "s1"},
        {**GOOD_DRAFT, "citations": [1]},
        {**GOOD_DRAFT, "answer_text": None},
        {**GOOD_DRAFT, "requested_tools": ["send_external_message"]},
        {"citations": ["s1"], "requested_tools": []},
        ["not", "an", "object"],
    ],
)
def test_draft_parser_is_strict(payload: object) -> None:
    with pytest.raises(ValueError):
        parse_draft(json.dumps(payload))


def test_draft_without_requested_tools_means_none() -> None:
    draft = parse_draft(json.dumps({"answer_text": "x [s1]", "citations": ["s1"]}))
    assert draft.requested_tools == [] and draft.malformed_tool_requests == []


def test_citation_validator_stays_the_authority__offline_fake_llm() -> None:
    """A well-formed Gemini draft citing a foreign source is withheld, not passed."""
    payload = {**GOOD_DRAFT, "answer_text": "Freeze [s9].", "citations": ["s9"]}
    generator = GeminiGenerator(
        "unused", "gemini-2.5-flash", client=FakeGeminiClient(json_response(payload))
    )
    out = asyncio.run(respond(responder_ctx(), "when?", [make_item("s1")], 0.5, generate=generator))
    assert out.abstained is True and out.abstain_reason == "withheld"
    assert out.citations == []


def test_responder_draft_schema_is_closed() -> None:
    assert _DRAFT_SCHEMA["additionalProperties"] is False
    assert set(_DRAFT_SCHEMA["required"]) == {"answer_text", "citations", "requested_tools"}


# --- schemas stay inside the documented Gemini subset ------------------------------

# google-genai 2.31.0 `GenerateContentConfig.response_json_schema`: `enum` is
# for strings and numbers; the planner and responder schemas use strings only.
_GEMINI_KEYWORDS = {"type", "enum", "properties", "required", "additionalProperties", "items"}


def _nodes(schema: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [schema]
    for sub in schema.get("properties", {}).values():
        nodes.extend(_nodes(sub))
    if "items" in schema:
        nodes.extend(_nodes(schema["items"]))
    for arm in schema.get("anyOf", []):
        nodes.extend(_nodes(arm))
    return nodes


@pytest.mark.parametrize("schema", [PLAN_OUTPUT_SCHEMA, _DRAFT_SCHEMA], ids=["plan", "draft"])
def test_output_schemas_use_only_gemini_json_schema_keywords(schema: dict[str, Any]) -> None:
    for node in _nodes(schema):
        assert set(node) <= _GEMINI_KEYWORDS | {"anyOf"}, node
        assert all(isinstance(value, str) for value in node.get("enum", [])), node
        assert not isinstance(node.get("type"), list), node


# --- construction and settings -----------------------------------------------------


def _no_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    attempts: list[object] = []

    def refuse(self: socket.socket, address: object) -> None:
        attempts.append(address)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return attempts


def test_client_construction_is_offline_and_pinned_to_the_developer_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = _no_network(monkeypatch)
    # The SDK would otherwise route to Vertex AI; an explicit vertexai=False wins.
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    client = llm.gemini_client(FAKE_GEMINI_KEY)
    api = client._api_client
    assert api.vertexai is False
    assert api._http_options.base_url == "https://generativelanguage.googleapis.com/"
    assert api._http_options.api_version == "v1beta"
    assert api._http_options.timeout == 60_000
    assert api._http_options.retry_options.attempts == 2
    assert FAKE_GEMINI_KEY not in repr(client)
    assert attempts == []


def test_plan_client_model_comes_from_llm_model() -> None:
    client = plan_client_from_settings(
        make_settings(gemini_api_key=FAKE_GEMINI_KEY, llm_model="gemini-3.8-flash")
    )
    assert isinstance(client, GeminiPlanClient)
    assert client._model == "gemini-3.8-flash"
    default = plan_client_from_settings(make_settings(gemini_api_key=FAKE_GEMINI_KEY))
    assert isinstance(default, GeminiPlanClient) and default._model == "gemini-2.5-flash"
    assert plan_client_from_settings(make_settings()) is None
    assert plan_client_from_settings(make_settings(gemini_api_key="   ")) is None


def test_settings_default_model_and_key_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.api.core.config import Settings

    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GEMINI_KEY)
    monkeypatch.setenv("LLM_MODEL", "gemini-3.8-flash")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == FAKE_GEMINI_KEY
    assert settings.llm_model == "gemini-3.8-flash"
    assert FAKE_GEMINI_KEY not in repr(settings)
    assert FAKE_GEMINI_KEY not in settings.model_dump_json()
    assert Settings.model_fields["llm_model"].default == "gemini-2.5-flash"


def test_required_settings_name_the_gemini_key() -> None:
    from apps.api.orchestrator import REQUIRED_SETTINGS, missing_orchestrator_settings
    from tests.api.fakes.scenario import orchestrator_settings

    assert ("gemini_api_key", "GEMINI_API_KEY") in REQUIRED_SETTINGS
    assert [name for _, name in REQUIRED_SETTINGS if "LLM" in name or "_API_KEY" in name] == [
        "ARMORIQ_API_KEY",
        "GEMINI_API_KEY",
    ]
    assert missing_orchestrator_settings(orchestrator_settings(gemini_api_key=None)) == [
        "GEMINI_API_KEY"
    ]


def test_build_orchestrator_wires_gemini_from_settings__offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api.orchestrator import build_orchestrator
    from tests.api.fakes.scenario import orchestrator_settings

    attempts = _no_network(monkeypatch)
    orchestrator, db = build_orchestrator(orchestrator_settings(llm_model="gemini-3.8-flash"))
    try:
        assert isinstance(orchestrator._plan_model, GeminiPlanClient)
        assert isinstance(orchestrator._generate, GeminiGenerator)
        assert orchestrator._plan_model._model == "gemini-3.8-flash"
        assert orchestrator._generate._model == "gemini-3.8-flash"
    finally:
        db.close()
    assert attempts == []
