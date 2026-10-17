"""P4-04 manager console API tests (API half; task §10 and §11 backend rows).

Offline (``__offline_fakes``): the routes run in a real FastAPI app behind
the P4-01 session dependency; audit reads come from a recording fake reader
(or the real P3-01 readers over scripted ``FakeDatabase`` rows), the topic
map from a small fake over the real ``validate_topic``, and the tool-server
probe is monkeypatched. No database, ArmorIQ, Slack or Gemini is contacted.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from apps.api import topic_map as real_topic_map
from apps.api.identity import SESSION_COOKIE_NAME, mint_session_token
from apps.api.main import create_app
from apps.api.topic_map import TopicRule
from services.audit.errors import AuditNotAuthorizedError, AuditStoreError
from services.audit.queries import (
    PendingHold,
    RequestSummary,
    TimelineEntry,
    UnansweredItem,
)
from services.contracts import DEFAULT_WORKSPACE_ID, AppRole, DataTier, PersonaKey
from services.contracts.api_manager import TOPIC_NOTE
from tests.api.audit_fakes import FakeDatabase, all_sql, origin_payload
from tests.api.fakes.scenario import FAKE_EMAILS, orchestrator_settings

SECRET = "session-offline-fake-secret-0000"
PASSCODE = "offline-passcode"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
REQ = "req-" + "a" * 32
PLAN = "plan-1"

REQ_LIST = "/api/v1/manager/requests"
REQ_TIMELINE = f"/api/v1/manager/requests/{REQ}/timeline"
REQ_HOLDS = "/api/v1/manager/holds"
REQ_UNANSWERED = "/api/v1/manager/unanswered"
REQ_RULES = "/api/v1/manager/topic-rules"
REQ_HEALTH = "/api/v1/manager/health"

VALID_RULE = {"topic": "pricing", "max_tier": "team"}
ENDPOINTS: tuple[tuple[str, str, dict[str, str] | None], ...] = (
    ("GET", REQ_LIST, None),
    ("GET", REQ_TIMELINE, None),
    ("GET", REQ_HOLDS, None),
    ("GET", REQ_UNANSWERED, None),
    ("GET", REQ_RULES, None),
    ("PUT", REQ_RULES, VALID_RULE),
    ("GET", REQ_HEALTH, None),
)
ENDPOINT_IDS = [f"{method} {url}" for method, url, _ in ENDPOINTS]

#: Tables only the tool server may read (SHARED_INTERFACES §7): a manager
#: endpoint must never reach them, directly or through a reader.
PROTECTED_TABLES = (
    "source_records",
    "knowledge_chunks",
    "decisions",
    "manager_guidance",
    "team_notes",
)

_UNSET = object()


def _settings(**overrides: object):  # type: ignore[no-untyped-def]
    # No ArmorIQ key: the lifespan leaves the orchestrator unconfigured and
    # each test installs its own readers.
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


MAYA = _cookie(PersonaKey.MAYA)
SAM = _cookie(PersonaKey.SAM)
PRIYA = _cookie(PersonaKey.PRIYA)


def _client(
    reader: Any = None,
    topics: Any = None,
    db: Any = _UNSET,
    **settings: object,
) -> TestClient:
    app = create_app(_settings(**settings))
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()  # run the lifespan first: it must not install an orchestrator
    assert getattr(app.state, "orchestrator", None) is None
    assert getattr(app.state, "slack_runtime", "missing") is None
    app.state.audit_db = object() if db is _UNSET else db
    if reader is not None:
        app.state.manager_reader = reader
    if topics is not None:
        app.state.topic_map = topics
    return client


def _assert_envelope(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    return body


def _no_fake_emails(text: str) -> None:
    for email in FAKE_EMAILS.values():
        assert email not in text
    assert "example.test" not in text


class FakeReader:
    """Recording stand-in for ``services.audit.queries`` (manager readers)."""

    def __init__(
        self,
        *,
        summaries: tuple[RequestSummary, ...] = (),
        holds: tuple[PendingHold, ...] = (),
        unanswered_items: tuple[UnansweredItem, ...] = (),
        entries: tuple[TimelineEntry, ...] = (),
        fail_with: BaseException | None = None,
    ) -> None:
        self.summaries = list(summaries)
        self.holds = list(holds)
        self.unanswered_items = list(unanswered_items)
        self.entries = list(entries)
        self.fail_with = fail_with
        self.calls: list[tuple[str, Any]] = []

    def _start(self, name: str, actor: Any, *extra: Any) -> None:
        self.calls.append((name, actor, *extra))
        if self.fail_with is not None:
            raise self.fail_with

    def recent_requests(self, db: Any, actor: Any, limit: int = 50) -> list[RequestSummary]:
        self._start("recent_requests", actor, limit)
        return list(self.summaries)

    def timeline(self, db: Any, request_id: str, actor: Any) -> list[TimelineEntry]:
        self._start("timeline", actor, request_id)
        return list(self.entries)

    def pending_holds(self, db: Any, actor: Any) -> list[PendingHold]:
        self._start("pending_holds", actor)
        return list(self.holds)

    def unanswered(self, db: Any, actor: Any, only_open: bool = True) -> list[UnansweredItem]:
        self._start("unanswered", actor, only_open)
        return list(self.unanswered_items)


class FakeTopicMap:
    """Recording stand-in for ``apps.api.topic_map`` over the real validator."""

    def __init__(self, *, rules: tuple[TopicRule, ...] = ()) -> None:
        self.rules = list(rules)
        self.calls: list[tuple[str, Any]] = []

    def validate_topic(self, topic: str) -> str:
        # The real normalization/refusals: an unusable topic must 422.
        return real_topic_map.validate_topic(topic)

    def list_rules(self, db: Any, workspace_id: str, actor: Any) -> list[TopicRule]:
        self.calls.append(("list_rules", actor, workspace_id))
        return list(self.rules)

    def upsert_rule(
        self,
        db: Any,
        *,
        actor: Any,
        workspace_id: str,
        topic: str,
        max_tier: DataTier,
    ) -> int:
        self.calls.append(("upsert_rule", actor, workspace_id, topic, max_tier))
        return 7


def _entry(**overrides: Any) -> TimelineEntry:
    values: dict[str, Any] = {
        "occurred_at": NOW,
        "kind": "decision",
        "event_type": "authorization_decided",
        "plan_id": PLAN,
        "step_index": 0,
        "decision_source": "armoriq_policy_decision",
        "payload": {},
        "tool_name": "knowledge_search_team",
        "jti": "jti-1",
    }
    values.update(overrides)
    return TimelineEntry(**values)


def _by_name(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in body["connectors"]}


# --- §10 authorization (parametrized over every endpoint) ----------------------


@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
def test_anonymous_gets_401__every_endpoint(method: str, url: str, body: Any) -> None:
    reader, topics = FakeReader(), FakeTopicMap()
    client = _client(reader, topics)
    response = client.request(method, url, json=body)
    _assert_envelope(response, 401, "unauthenticated")
    _no_fake_emails(response.text)
    assert reader.calls == [] and topics.calls == []  # auth failed before any read


@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
@pytest.mark.parametrize("cookie", [SAM, PRIYA], ids=["sam", "priya"])
def test_non_manager_gets_403__every_endpoint(
    cookie: dict[str, str], method: str, url: str, body: Any
) -> None:
    reader, topics = FakeReader(), FakeTopicMap()
    client = _client(reader, topics)
    response = client.request(method, url, json=body, headers=cookie)
    _assert_envelope(response, 403, "forbidden")
    assert "Manager access only." in response.text
    _no_fake_emails(response.text)
    assert reader.calls == [] and topics.calls == []  # no data path was consulted


def test_manager_check_runs_before_body_validation() -> None:
    # A Sam session with an otherwise-invalid body gets 403, not 422: the
    # role gate never depends on the body.
    client = _client(FakeReader(), FakeTopicMap())
    response = client.put(
        REQ_RULES, headers=SAM, json={"topic": "pricing", "max_tier": "team", "member_id": "m-sam"}
    )
    _assert_envelope(response, 403, "forbidden")


def test_put_body_rejects_identity_keys_and_never_echoes_values() -> None:
    topics = FakeTopicMap()
    client = _client(FakeReader(), topics)
    response = client.put(
        REQ_RULES,
        headers=MAYA,
        json={"topic": "pricing", "max_tier": "team", "member_id": "m-maya", "app_role": "manager"},
    )
    body = _assert_envelope(response, 422, "validation_error")
    # The envelope names the offending keys, never the submitted values.
    assert "m-maya" not in response.text
    _no_fake_emails(response.text)
    assert all(set(entry) == {"loc", "msg", "type"} for entry in body["error"]["details"])
    assert "upsert_rule" not in [call[0] for call in topics.calls]  # nothing was written


# --- requests list -------------------------------------------------------------


def test_requests_list__session_identity_and_persona_mapping() -> None:
    reader = FakeReader(
        summaries=(
            RequestSummary(
                request_id=REQ,
                trace_id="trace-1",
                workspace_id=DEFAULT_WORKSPACE_ID,
                entry_point="web",
                requester_member_id="m-sam",
                last_event_at=NOW,
            ),
            RequestSummary(
                request_id="req-" + "b" * 32,
                trace_id="trace-2",
                workspace_id=DEFAULT_WORKSPACE_ID,
                entry_point="",
                requester_member_id="m-nobody",
                last_event_at=NOW,
            ),
        )
    )
    client = _client(reader, FakeTopicMap())
    response = client.get(f"{REQ_LIST}?limit=5&member_id=m-sam&persona=sam", headers=MAYA)
    assert response.status_code == 200, response.text
    first, second = response.json()
    assert first["request_id"] == REQ
    assert first["entry_point"] == "web"
    assert first["requester_persona"] == "sam"
    assert first["last_event_at"].startswith("2026-10-18T12:00:00")
    # Unknown or unreadable identity fields stay None, never a guess or a 500.
    assert second["entry_point"] is None and second["requester_persona"] is None
    name, actor, limit = reader.calls[0]
    assert name == "recent_requests" and limit == 5
    # The session is the only identity source: query junk changes nothing.
    assert actor.member_id == "m-maya" and actor.app_role is AppRole.MANAGER


# --- timeline ------------------------------------------------------------------


def test_timeline__correlation_fields_and_wording() -> None:
    entries = (
        _entry(
            kind="request",
            event_type="request_received",
            plan_id=None,
            step_index=None,
            decision_source=None,
            payload={"workspace_id": "ws-1"},
            tool_name=None,
            jti=None,
        ),
        _entry(
            payload={
                "outcome": "allow",
                "ledger_entry_id": "led-1",
                "execution_status": "executed",
                "tool_server_reached": True,
                "validation_outcome": "validated",
                "delegation_id": "dlg-9",
            }
        ),
        _entry(
            kind="delivery",
            event_type="delivery_delivered",
            plan_id=None,
            step_index=None,
            decision_source=None,
            payload={
                "channel": "web_private",
                "delivered_to_member_id": "m-sam",
                "tier_delivered": "team",
            },
            tool_name=None,
            jti=None,
        ),
    )
    client = _client(FakeReader(entries=entries), FakeTopicMap())
    response = client.get(REQ_TIMELINE, headers=MAYA)
    assert response.status_code == 200, response.text
    _no_fake_emails(response.text)
    first, second, third = response.json()
    assert [first["kind"], second["kind"], third["kind"]] == ["request", "decision", "delivery"]
    assert first["jti"] is None and first["ledger_entry_id"] is None
    assert first["is_armoriq_policy"] is False  # only decisions can be ArmorIQ policy
    assert second["decision_source"] == "armoriq_policy_decision"
    assert second["is_armoriq_policy"] is True
    assert second["enforcement_layer"] == "ArmorIQ policy"
    assert second["jti"] == "jti-1"
    assert second["ledger_entry_id"] == "led-1"
    assert second["execution_status"] == "executed"
    assert second["tool_server_reached"] is True
    assert second["validation_outcome"] == "validated"
    assert second["delegation_id"] == "dlg-9"
    assert second["outcome"] == "allow"
    assert third["delivery_status"] == "delivered"
    assert third["delivery_channel"] == "web_private"
    assert third["delivered_to_persona"] == "sam"
    assert third["tier_delivered"] == "team"


def test_timeline__malformed_missing_and_empty_are_404() -> None:
    reader = FakeReader(entries=())
    client = _client(reader, FakeTopicMap())
    assert (
        client.get("/api/v1/manager/requests/not-a-request-id/timeline", headers=MAYA).status_code
        == 404
    )
    assert reader.calls == []  # malformed IDs never reach a reader
    _assert_envelope(client.get(REQ_TIMELINE, headers=MAYA), 404, "not_found")
    assert [call[0] for call in reader.calls] == ["timeline"]
    assert reader.calls[0][2] == REQ  # the reader saw the requested ID


# --- holds ---------------------------------------------------------------------


def test_holds__console_link_and_no_approval_action() -> None:
    hold = PendingHold(
        approval_id=1,
        request_id=REQ,
        plan_id=PLAN,
        step_index=1,
        tool_name="read_source_confidential",
        delegation_id="dlg-1",
        requested_at=NOW,
        requested_by_member_id="m-priya",
    )
    client = _client(FakeReader(holds=(hold,)), FakeTopicMap())
    response = client.get(REQ_HOLDS, headers=MAYA)
    assert response.status_code == 200, response.text
    [row] = response.json()
    assert row["armoriq_console_url"] == "https://platform.armoriq.ai"  # D-22 target
    assert row["delegation_id"] == "dlg-1"
    assert row["requester_persona"] == "priya"
    assert row["tool"] == "read_source_confidential"
    assert row["approval_state"] == "pending"
    assert "Approval happens only in the ArmorIQ console" in row["note"]
    assert "cannot approve or reject" in row["note"]
    # No action field, and no action route: approving is not this API's job.
    assert not {"approve", "approve_hold", "reject", "reject_hold", "resolve"} & set(row)
    paths = client.get("/openapi.json").json()["paths"]
    assert [path for path in paths if path.startswith(REQ_HOLDS + "/")] == []


# --- unanswered ----------------------------------------------------------------


def test_unanswered__requester_reason_and_status_pass_through() -> None:
    items = (
        UnansweredItem(
            question_id=9,
            request_id=REQ,
            question="What did we decide about the atlas budget?",
            reason="conflict",
            status="open",
            created_at=NOW,
            requester_member_id="m-priya",
        ),
        UnansweredItem(
            question_id=10,
            request_id=REQ,
            question="And the second one?",
            reason=None,
            status="answered",
            created_at=NOW,
            requester_member_id=None,
        ),
    )
    reader = FakeReader(unanswered_items=items)
    client = _client(reader, FakeTopicMap())
    first, second = client.get(f"{REQ_UNANSWERED}?only_open=false", headers=MAYA).json()
    assert first["question_id"] == "9" and first["requester_persona"] == "priya"
    assert first["question"] == "What did we decide about the atlas budget?"
    assert first["reason"] == "conflict" and first["status"] == "open"
    assert first["created_at"].startswith("2026-10-18T12:00:00")
    assert second["requester_persona"] is None and second["reason"] is None
    assert second["status"] == "answered"
    assert reader.calls[0][2] is False  # only_open passed through
    client.get(REQ_UNANSWERED, headers=MAYA)  # default
    assert reader.calls[1][2] is True


# --- topic rules ---------------------------------------------------------------


def test_topic_rules__list_carries_the_access_note() -> None:
    rules = (
        TopicRule(
            id="1", workspace_id=DEFAULT_WORKSPACE_ID, topic="pricing", max_tier=DataTier.TEAM
        ),
        TopicRule(
            id="2",
            workspace_id=DEFAULT_WORKSPACE_ID,
            topic="hiring plan",
            max_tier=DataTier.CONFIDENTIAL,
        ),
    )
    topics = FakeTopicMap(rules=rules)
    client = _client(FakeReader(), topics)
    body = client.get(REQ_RULES, headers=MAYA).json()
    assert body["note"] == TOPIC_NOTE
    assert body["rules"] == [
        {"rule_id": "1", "topic": "pricing", "max_tier": "team"},
        {"rule_id": "2", "topic": "hiring plan", "max_tier": "confidential"},
    ]
    _, actor, workspace_id = topics.calls[0]
    assert actor.member_id == "m-maya" and workspace_id == DEFAULT_WORKSPACE_ID


def test_topic_rules__put_normalizes_and_uses_the_session_author() -> None:
    topics = FakeTopicMap()
    client = _client(FakeReader(), topics)
    response = client.put(
        REQ_RULES, headers=MAYA, json={"topic": "  Q4 Pricing  ", "max_tier": "confidential"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"rule_id": "7", "topic": "q4 pricing", "max_tier": "confidential"}
    _, actor, workspace_id, topic, max_tier = topics.calls[0]
    assert actor.member_id == "m-maya"  # created_by comes from the session only
    assert workspace_id == DEFAULT_WORKSPACE_ID
    assert topic == "q4 pricing" and max_tier is DataTier.CONFIDENTIAL


def test_topic_rules__unusable_topic_is_422_and_writes_nothing() -> None:
    topics = FakeTopicMap()
    client = _client(FakeReader(), topics)
    _assert_envelope(
        client.put(REQ_RULES, headers=MAYA, json={"topic": "ai", "max_tier": "team"}),
        422,
        "invalid_topic",
    )
    assert topics.calls == []  # refused before any read or write


def test_topic_rules__put_preflight_allows_put() -> None:
    client = _client(FakeReader(), FakeTopicMap())
    response = client.options(
        REQ_RULES,
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert "PUT" in response.headers["access-control-allow-methods"]


# --- health --------------------------------------------------------------------


def test_health__failed_probes_report_unavailable_never_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _down(url: str, timeout: float | None = None) -> Any:
        raise URLError("connection refused")

    monkeypatch.setattr("apps.api.routes.manager.urlopen", _down)
    client = _client(FakeReader(), FakeTopicMap(), db=None, toolserver_base_url="http://ts.test:1")
    body = client.get(REQ_HEALTH, headers=MAYA).json()
    connectors = _by_name(body)
    assert connectors["Tool server"]["status"] == "unavailable"
    assert connectors["Tool server"]["label"] == "Unavailable"
    assert connectors["Backend database"]["status"] == "unavailable"
    assert connectors["Slack"]["status"] == "unavailable"
    assert {
        name: connectors[name]["status"] for name in ("Gmail", "Google Drive", "Google Meet")
    } == {"Gmail": "planned", "Google Drive": "planned", "Google Meet": "planned"}
    assert connectors["Gmail"]["label"] == "Planned"
    assert body["service"] == "proxylead-backend" and body["environment"] == "test"


def test_health__connected_when_probes_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, float | None]] = []

    class _Response:
        status = 200

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _open(url: str, timeout: float | None = None) -> _Response:
        calls.append((url, timeout))
        return _Response()

    monkeypatch.setattr("apps.api.routes.manager.urlopen", _open)
    client = _client(
        FakeReader(),
        FakeTopicMap(),
        db=FakeDatabase(),
        toolserver_base_url="http://ts.test:8080/",
    )
    client.app.state.slack_runtime = SimpleNamespace(thread=SimpleNamespace(is_alive=lambda: True))
    body = client.get(REQ_HEALTH, headers=MAYA).json()
    connectors = _by_name(body)
    assert connectors["Tool server"]["status"] == "connected"
    assert connectors["Backend database"]["status"] == "connected"
    assert connectors["Slack"]["status"] == "connected"
    assert connectors["Tool server"]["label"] == "Connected and tested"
    assert calls == [("http://ts.test:8080/health", 2.0)]  # trailing slash, fixed timeout


def test_health__unset_tool_server_is_unavailable_without_a_probe() -> None:
    client = _client(FakeReader(), FakeTopicMap(), toolserver_base_url=None)
    body = client.get(REQ_HEALTH, headers=MAYA).json()
    assert _by_name(body)["Tool server"]["status"] == "unavailable"


# --- failure mapping and the no-protected-reads guarantee ----------------------


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AuditNotAuthorizedError("role drift"), 403, "forbidden"),
        (AuditStoreError("store down"), 503, "manager_unavailable"),
    ],
)
def test_reader_failures_map_to_envelopes(error: BaseException, status: int, code: str) -> None:
    client = _client(FakeReader(fail_with=error), FakeTopicMap())
    response = client.get(REQ_LIST, headers=MAYA)
    _assert_envelope(response, status, code)
    _no_fake_emails(response.text)


def test_missing_audit_db_is_503() -> None:
    client = _client(FakeReader(), FakeTopicMap(), db=None)
    _assert_envelope(client.get(REQ_UNANSWERED, headers=MAYA), 503, "manager_unavailable")


def test_manager_reads_never_touch_protected_tables() -> None:
    """Real readers over scripted rows: read-only, no protected table."""
    db = FakeDatabase(
        fetchall=[
            # /requests: recent_requests (origin payload join)
            [(REQ, "trace-1", origin_payload(), NOW)],
            # /timeline: request, decision and delivery rows
            [(NOW, "request_received", None, None, None, {"workspace_id": "ws-1"}, None)],
            [
                (
                    NOW,
                    "authorization_decided",
                    PLAN,
                    0,
                    "jti-1",
                    "armoriq_policy_decision",
                    {"outcome": "allow"},
                    "knowledge_search_team",
                )
            ],
            [(NOW, "web_private", "m-sam", "delivered", PLAN)],
            # /holds
            [(1, REQ, PLAN, 0, "read_source_team", "dlg-1", NOW, "m-sam")],
            # /unanswered: queue rows + companion abstention events
            [(9, REQ, "m-sam", "why?", "open", NOW)],
            [(REQ, None, "no_evidence")],
            # /topic-rules
            [(1, DEFAULT_WORKSPACE_ID, "pricing", "team")],
        ]
    )
    client = _client(db=db)  # real readers and the real topic map module
    for url in (REQ_LIST, REQ_TIMELINE, REQ_HOLDS, REQ_UNANSWERED, REQ_RULES):
        response = client.get(url, headers=MAYA)
        assert response.status_code == 200, f"{url}: {response.text}"
    sql = all_sql(db)
    for table in PROTECTED_TABLES:
        assert table not in sql, f"manager SQL reached {table}"
    assert "request_events" in sql and "approval_requests" in sql and "topic_access_rules" in sql
    # The console is read-only: not one INSERT/UPDATE/DELETE reached the store.
    for statement, _params in db.committed:
        assert statement.lstrip().upper().startswith(("SELECT", "WITH")), statement
