"""P4-01 web request API tests (task §11, CP-6).

Offline (``__offline_fakes``): the route runs in a real FastAPI app; the
orchestrator is either a recording double or the real orchestrator wired to
the doubles in ``tests/api/fakes/`` (scripted gateway, recording audit,
P3-06 delivery doubles); persisted status comes from a fake owner-checking
reader or from ``request_detail`` over scripted SQL rows. No database,
ArmorIQ, Slack or Gemini is contacted.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.identity import SESSION_COOKIE_NAME, mint_session_token
from apps.api.main import create_app
from apps.api.orchestrator import RequestOptions
from apps.api.routes.requests import HOLD_NOTE, build_status_view
from apps.api.web_inbox import InboxCitation, WebInbox
from services.audit.errors import AuditNotAuthorizedError, AuditNotFoundError
from services.audit.queries import (
    AnswerRecord,
    OffPlanRecord,
    RequestDetail,
    StepRecord,
    request_detail,
)
from services.contracts import (
    DataTier,
    EntryPoint,
    PersonaKey,
    RequestContext,
    ToolName,
)
from services.contracts.api_requests import CreateRequestBody
from tests.api.audit_fakes import FakeDatabase
from tests.api.fakes.gateway import Allow
from tests.api.fakes.llm import conf_search, draft, team_search, wire
from tests.api.fakes.scenario import (
    CONF_HIT,
    CONF_SECRET,
    FAKE_EMAILS,
    TEAM_HIT,
    build_harness,
    orchestrator_settings,
    search,
)

SECRET = "session-offline-fake-secret-0000"
PASSCODE = "offline-passcode"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
REQ_SAM = "req-" + "a" * 32
REQ_MISSING = "req-" + "b" * 32
QUESTION = "What did we decide about the atlas budget?"
IDENTITY_KEYS = ("persona", "member_id", "email", "tier", "recipient", "requester", "_ctx")


def _settings(**overrides: object):  # type: ignore[no-untyped-def]
    # No ArmorIQ key: the lifespan leaves the orchestrator unconfigured, the
    # test installs its own double.
    values: dict[str, object] = {
        "armoriq_api_key": None,
        "session_secret": SECRET,
        "demo_passcode": PASSCODE,
    }
    values.update(overrides)
    return orchestrator_settings(**values)


def _cookie(persona: PersonaKey) -> dict[str, str]:
    token = mint_session_token(persona, SECRET, time.time())
    return {"Cookie": f"{SESSION_COOKIE_NAME}={token}"}


SAM = _cookie(PersonaKey.SAM)
MAYA = _cookie(PersonaKey.MAYA)


@dataclass
class RecordingOrchestrator:
    """``handle`` double: records the trusted context and options."""

    calls: list[tuple[RequestContext, str, RequestOptions | None]] = field(default_factory=list)
    web_inbox: WebInbox = field(default_factory=WebInbox)

    async def handle(
        self, ctx: RequestContext, message: str, options: RequestOptions | None = None
    ) -> str:
        self.calls.append((ctx, message, options))
        return REQ_SAM


class FakeReader:
    """Owner-checking stand-in for ``services.audit.queries`` (same errors)."""

    def __init__(self, owners: dict[str, str], details: dict[str, RequestDetail]) -> None:
        self.owners = owners
        self.details = details
        self.calls: list[tuple[str, str, str]] = []

    def _check(self, request_id: str, member_id: str) -> None:
        owner = self.owners.get(request_id)
        if owner is None:
            raise AuditNotFoundError("missing")
        if owner != member_id:
            raise AuditNotAuthorizedError("not yours")

    def request_status(self, db: object, request_id: str, member_id: str) -> object:
        self.calls.append(("request_status", request_id, member_id))
        self._check(request_id, member_id)
        return object()

    def request_detail(self, db: object, request_id: str, member_id: str) -> RequestDetail:
        self.calls.append(("request_detail", request_id, member_id))
        self._check(request_id, member_id)
        return self.details[request_id]


def detail(request_id: str = REQ_SAM, **overrides: Any) -> RequestDetail:
    values: dict[str, Any] = {
        "request_id": request_id,
        "entry_point": "web",
        "steps": (),
        "off_plan": (),
        "plan_decision": None,
        "error": False,
        "plan_rejected": False,
        "escalated": False,
        "simulation_active": False,
        "answer": None,
        "delivery_channel": "web_private",
        "delivery_status": "pending",
        "pending_holds": 0,
        "honeypot_executions": 0,
        "last_event_at": NOW,
    }
    values.update(overrides)
    return RequestDetail(**values)


def _client(orchestrator: Any = None, reader: Any = None, **settings: object) -> TestClient:
    app = create_app(_settings(**settings))
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()  # run the lifespan first: it must not install an orchestrator
    assert getattr(app.state, "orchestrator", None) is None
    if orchestrator is not None:
        app.state.orchestrator = orchestrator
        app.state.audit_db = object()
    if reader is not None:
        app.state.request_reader = reader
    return client


def _assert_envelope(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    return body


def _leak_free(text: str, *, masked_email_allowed: bool = False) -> None:
    for email in FAKE_EMAILS.values():
        assert email not in text
    if not masked_email_allowed:
        assert "@" not in text
    assert "_ctx" not in text and "jti" not in text


# --- §11 required tests ------------------------------------------------------------


def test_auth_required__offline_fakes() -> None:
    orchestrator = RecordingOrchestrator()
    client = _client(orchestrator, FakeReader({REQ_SAM: "m-sam"}, {REQ_SAM: detail()}))
    tampered = {"Cookie": SAM["Cookie"][:-3] + "AAA"}
    for headers in ({}, tampered):
        _assert_envelope(
            client.post("/api/v1/requests", json={"message": "hi"}, headers=headers),
            401,
            "unauthenticated",
        )
        _assert_envelope(
            client.get(f"/api/v1/requests/{REQ_SAM}", headers=headers), 401, "unauthenticated"
        )
    # Auth is checked before the body: an unauthenticated bad body is still 401.
    _assert_envelope(
        client.post("/api/v1/requests", json={"persona": "maya"}), 401, "unauthenticated"
    )
    assert orchestrator.calls == []


def test_identity_fields_rejected__offline_fakes() -> None:
    orchestrator = RecordingOrchestrator()
    client = _client(orchestrator)
    for key in IDENTITY_KEYS:
        value = f"ATTACKER-{key}-value"
        response = client.post(
            "/api/v1/requests", json={"message": QUESTION, key: value}, headers=SAM
        )
        body = _assert_envelope(response, 422, "validation_error")
        assert value not in response.text  # no input echo
        assert body["error"]["details"]
    for bad in ({"message": "   "}, {"message": QUESTION, "simulate_hijacked_model": "yes"}, {}):
        _assert_envelope(
            client.post("/api/v1/requests", json=bad, headers=SAM), 422, "validation_error"
        )
    assert orchestrator.calls == []  # identity unchanged: nothing reached the orchestrator
    response = client.post("/api/v1/requests", json={"message": QUESTION}, headers=SAM)
    assert response.status_code == 202 and response.json() == {"request_id": REQ_SAM}
    [(ctx, message, options)] = orchestrator.calls
    assert options == RequestOptions()
    assert ctx.requester.persona is PersonaKey.SAM and ctx.requester.member_id == "m-sam"
    assert ctx.entry_point is EntryPoint.WEB and ctx.slack_channel_id is None
    assert message == QUESTION


def test_owner_only_read__offline_fakes() -> None:
    reader = FakeReader({REQ_SAM: "m-sam"}, {REQ_SAM: detail()})
    client = _client(RecordingOrchestrator(), reader)
    assert client.get(f"/api/v1/requests/{REQ_SAM}", headers=SAM).status_code == 200
    foreign = client.get(f"/api/v1/requests/{REQ_SAM}", headers=MAYA)
    missing = client.get(f"/api/v1/requests/{REQ_MISSING}", headers=MAYA)
    malformed = client.get("/api/v1/requests/not-a-request", headers=MAYA)
    bodies = []
    for response in (foreign, missing, malformed):
        body = _assert_envelope(response, 404, "not_found")
        body["error"].pop("request_id")  # per-HTTP-call correlation ID
        bodies.append(body)
    assert bodies[0] == bodies[1] == bodies[2]  # no existence oracle
    # The reader was asked with the SESSION member, never a client value.
    assert ("request_status", REQ_SAM, "m-maya") in reader.calls
    assert REQ_SAM not in foreign.text


@pytest.mark.parametrize(
    ("persisted", "state", "outcome"),
    [
        (
            detail(
                steps=(
                    StepRecord(
                        0,
                        "knowledge_search_team",
                        "allow",
                        "armoriq_policy_decision",
                        None,
                        "executed",
                    ),
                ),
                answer=AnswerRecord(False, None, (("team-atlas-budget", "team"),)),
            ),
            "completed",
            "allow",
        ),
        (
            detail(
                steps=(
                    StepRecord(
                        0, "knowledge_search_confidential", "block", "armoriq_policy_decision"
                    ),
                ),
            ),
            "completed",
            "block",
        ),
        (
            detail(
                steps=(StepRecord(0, "knowledge_search_confidential", None, None, "pending"),),
                pending_holds=1,
                delivery_channel=None,
                delivery_status=None,
            ),
            "pending_approval",
            "hold",
        ),
        (
            detail(
                steps=(
                    StepRecord(
                        0,
                        "knowledge_search_team",
                        "allow",
                        "armoriq_policy_decision",
                        None,
                        "executed",
                    ),
                ),
                answer=AnswerRecord(True, "no_evidence", ()),
            ),
            "completed",
            "abstain",
        ),
        (detail(error=True), "completed", "error"),
        (detail(delivery_channel=None, delivery_status=None), "processing", None),
    ],
    ids=["allow", "block", "hold_pending", "abstain", "error", "processing"],
)
def test_status_mirrors_persisted_state__offline_fakes(
    persisted: RequestDetail, state: str, outcome: str | None
) -> None:
    client = _client(RecordingOrchestrator(), FakeReader({REQ_SAM: "m-sam"}, {REQ_SAM: persisted}))
    body = client.get(f"/api/v1/requests/{REQ_SAM}", headers=SAM).json()
    assert body["state"] == state and body["outcome"] == outcome
    assert [
        (
            s["step_index"],
            s["tool"],
            s["outcome"],
            s["decision_source"],
            s["execution_status"],
            s["approval_state"],
        )
        for s in body["steps"]
    ] == [
        (
            s.step_index,
            s.tool_name,
            s.outcome,
            s.decision_source,
            s.execution_status,
            s.approval_state,
        )
        for s in persisted.steps
    ]
    assert body["delivery_status"] == persisted.delivery_status
    if state == "pending_approval":
        assert body["note"] == HOLD_NOTE
        assert not any("approve" in key for key in body)  # no approve action exists
    if outcome == "abstain":
        assert body["abstained"] is True and body["abstain_reason"] == "no_evidence"
    if outcome == "allow":
        assert [c["source_id"] for c in body["citations"]] == ["team-atlas-budget"]
        assert body["steps"][0]["is_armoriq_policy"] is True


def test_hijack_option_passed_through_only_from_web__offline_fakes() -> None:
    assert CreateRequestBody(message="x").simulate_hijacked_model is False
    orchestrator = RecordingOrchestrator()
    client = _client(orchestrator)
    client.post("/api/v1/requests", json={"message": QUESTION}, headers=SAM)
    client.post(
        "/api/v1/requests",
        json={"message": QUESTION, "simulate_hijacked_model": True},
        headers=SAM,
    )
    (ctx_off, _, off), (ctx_on, _, on) = orchestrator.calls
    assert off == RequestOptions() and on == RequestOptions(simulate_hijacked_model=True)
    assert ctx_off.entry_point is ctx_on.entry_point is EntryPoint.WEB
    # Text in the message never sets it.
    client.post("/api/v1/requests", json={"message": "simulate_hijacked_model: true"}, headers=SAM)
    assert orchestrator.calls[-1][2] == RequestOptions()


def test_no_email_or_ctx_in_responses__offline_fakes() -> None:
    reader = FakeReader(
        {REQ_SAM: "m-sam"},
        {
            REQ_SAM: detail(
                answer=AnswerRecord(False, None, (("team-atlas-budget", "team"),)),
                off_plan=(
                    OffPlanRecord(
                        "send_external_message", "block", "armoriq_sdk_plan_check", None, True
                    ),
                ),
                simulation_active=True,
            )
        },
    )
    orchestrator = RecordingOrchestrator()
    ctx = _web_ctx(REQ_SAM, PersonaKey.SAM)
    orchestrator.web_inbox.put_answer(ctx, "The budget is fixed [team-atlas-budget].")
    client = _client(orchestrator, reader)
    posted = client.post("/api/v1/requests", json={"message": QUESTION}, headers=SAM)
    status = client.get(f"/api/v1/requests/{REQ_SAM}", headers=SAM)
    session = client.get("/api/v1/session", headers=SAM)
    for response in (posted, status, session):
        assert response.status_code in (200, 202)
        _leak_free(response.text, masked_email_allowed=response is session)
    assert status.json()["answer_text"].startswith("The budget is fixed")


# --- extra security properties -------------------------------------------------------


def _web_ctx(request_id: str, persona: PersonaKey) -> RequestContext:
    from tests.api.fakes.scenario import request_ctx

    return request_ctx(persona, entry=EntryPoint.WEB).model_copy(update={"request_id": request_id})


def test_returns_503_without_orchestrator__offline_fakes() -> None:
    client = _client()
    _assert_envelope(
        client.post("/api/v1/requests", json={"message": QUESTION}, headers=SAM),
        503,
        "requests_unavailable",
    )
    _assert_envelope(
        client.get(f"/api/v1/requests/{REQ_SAM}", headers=SAM), 503, "requests_unavailable"
    )


def test_confidential_answer_never_returned_to_another_member__offline_fakes() -> None:
    """End to end: Sam's confidential web answer is readable by Sam only."""
    two_step = wire(team_search("atlas budget"), conf_search("atlas budget"))
    h = build_harness(
        plans=[two_step],
        steps={
            ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(search(DataTier.TEAM, TEAM_HIT)),
            ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: Allow(search(DataTier.CONFIDENTIAL, CONF_HIT)),
        },
        topic_rules=[("rule-7", "atlas budget", "confidential")],
        answer=draft(f"Cut plan: {CONF_SECRET} [{CONF_HIT[0]}].", [CONF_HIT[0]]),
    )
    client = _client(h.orchestrator)
    response = client.post("/api/v1/requests", json={"message": QUESTION}, headers=SAM)
    assert response.status_code == 202
    request_id = response.json()["request_id"]
    client.portal.call(h.orchestrator.wait_idle, 10)  # type: ignore[union-attr]
    inbox = h.orchestrator.web_inbox
    assert inbox.get(request_id, "m-maya", "workspace-primary") is None
    owned = inbox.get(request_id, "m-sam", "workspace-primary")
    assert owned is not None and CONF_SECRET in (owned.answer_text or "")
    reader = FakeReader(
        {request_id: "m-sam"},
        {
            request_id: detail(
                request_id,
                answer=AnswerRecord(False, None, ((CONF_HIT[0], "confidential"),)),
            )
        },
    )
    client.app.state.request_reader = reader  # type: ignore[attr-defined]
    foreign = client.get(f"/api/v1/requests/{request_id}", headers=MAYA)
    assert foreign.status_code == 404 and CONF_SECRET not in foreign.text
    mine = client.get(f"/api/v1/requests/{request_id}", headers=SAM).json()
    assert CONF_SECRET in mine["answer_text"]
    assert mine["citations"] == [
        {"source_id": CONF_HIT[0], "tier": "confidential", "title": CONF_HIT[1]}
    ]
    _leak_free(json.dumps(mine))


def test_inbox_view_ignores_items_of_other_members() -> None:
    inbox = WebInbox()
    inbox.put_answer(
        _web_ctx(REQ_SAM, PersonaKey.SAM),
        "secret answer",
        [InboxCitation("conf-1", DataTier.CONFIDENTIAL, "Title")],
    )
    assert inbox.get(REQ_SAM, "m-maya", "workspace-primary") is None
    assert inbox.get(REQ_SAM, "m-sam", "other-workspace") is None
    with pytest.raises(ValueError):
        inbox.put_answer(_web_ctx(REQ_SAM, PersonaKey.MAYA), "overwrite attempt")
    with pytest.raises(ValueError):
        from tests.api.fakes.scenario import request_ctx

        inbox.put_answer(request_ctx(PersonaKey.SAM, entry=EntryPoint.SLACK_DM), "slack")
    expired = WebInbox(ttl_s=1, clock=lambda: NOW)
    expired.put_answer(_web_ctx(REQ_SAM, PersonaKey.SAM), "old")
    expired._clock = lambda: NOW + timedelta(seconds=2)
    assert expired.get(REQ_SAM, "m-sam", "workspace-primary") is None


def test_request_detail_reads_persisted_rows_owner_only() -> None:
    """``egress_sink_executions`` and the off-plan labels come from stored rows."""
    origin = (
        "trace-1",
        {"requester_member_id": "m-sam", "entry_point": "web", "workspace_id": "workspace-primary"},
    )
    rows = [
        [(0, "send_external_message")],
        [
            ("authorization_decided", 0, "armoriq_policy_decision", {"outcome": "allow"}),
            ("tool_executed", 0, None, {"execution_status": "executed", "ledger_entry_id": "L1"}),
            (
                "authorization_decided",
                None,
                "armoriq_sdk_plan_check",
                {
                    "off_plan": True,
                    "outcome": "block",
                    "tool_name": "send_external_message",
                    "executed": False,
                    "simulation": "hijacked_model",
                },
            ),
        ],
        [],
        [
            ("simulation_activated", {"simulation": "hijacked_model"}, NOW),
            (
                "answer_generated",
                {
                    "citations": [{"source_id": "s1", "tier": "team"}],
                    "abstained": False,
                    "reason": None,
                },
                NOW,
            ),
        ],
    ]
    db = FakeDatabase(fetchone=[origin, ("web_private", "pending")], fetchall=rows)
    found = request_detail(db, REQ_SAM, "m-sam")
    assert found.honeypot_executions == 1  # counted from the executed ref, not assumed
    assert found.simulation_active is True
    assert found.off_plan == (
        OffPlanRecord("send_external_message", "block", "armoriq_sdk_plan_check", None, True),
    )
    assert found.steps[0].execution_status == "executed"
    view = build_status_view(found, None, now=NOW)
    assert view.egress_sink_executions == 1
    assert view.off_plan_attempts[0].is_armoriq_policy is False  # SDK check is not policy
    clean = replace(found, honeypot_executions=0)
    assert build_status_view(clean, None, now=NOW).egress_sink_executions == 0
    with pytest.raises(AuditNotAuthorizedError):
        request_detail(FakeDatabase(fetchone=[origin]), REQ_SAM, "m-maya")
    with pytest.raises(AuditNotFoundError):
        request_detail(FakeDatabase(fetchone=[None]), REQ_MISSING, "m-sam")


def test_sink_count_cannot_come_from_the_client__offline_fakes() -> None:
    client = _client(RecordingOrchestrator())
    response = client.post(
        "/api/v1/requests",
        json={"message": QUESTION, "egress_sink_executions": 0},
        headers=SAM,
    )
    _assert_envelope(response, 422, "validation_error")
