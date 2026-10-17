"""Offline E3 probe tests (no credentials, no network: never E3 evidence).

The probe runs over the REAL `ArmorIQGateway` with fake SDK clients (one
per member), a fake tool server that verifies the real `_ctx`, and a fake
read-only evidence lookup (`tests/api/fakes/probe_sdk.py`).
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from apps.api.core.config import Settings
from scripts import probe_v2_common as common
from scripts import probe_v2_e2_offplan as e2
from scripts import probe_v2_e3_per_user as probe
from tests.api.fakes.probe_sdk import (
    FAKE_BEARER,
    FAKE_DSN,
    FAKE_EMAILS,
    OFFLINE_BASE_URL,
    FakeEvidenceLookup,
    FakeSdkClient,
    FakeToolServer,
    assert_no_fake_values,
    matrix_policy,
    offline_settings,
)

PROBE_ENV = (
    "ARMORIQ_API_KEY",
    "PERSONA_MAYA_EMAIL",
    "PERSONA_SAM_EMAIL",
    "PERSONA_PRIYA_EMAIL",
    "CTX_SECRET",
    "TOOLSERVER_BEARER_MEMORY",
    "DATABASE_URL_TOOLSERVER",
    "TOOLSERVER_BASE_URL",
    "ARMORIQ_AGENT_ID",
)


@pytest.fixture(autouse=True)
def _never_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("offline test tried to go live")

    monkeypatch.setattr("services.authorization.armoriq_gateway.ArmorIQClient", _refuse)
    monkeypatch.setattr(e2, "build_direct_client", _refuse)
    monkeypatch.setattr(e2, "E2EvidenceLookup", _refuse)
    monkeypatch.setattr(common, "ProbeEvidenceLookup", _refuse)
    for name in PROBE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", (tmp_path / "none.env",))
    monkeypatch.setattr(e2.e1, "TOOLSERVER_ENV_FILE", tmp_path / "toolserver.env")


def _run(
    *,
    policy=matrix_policy,
    hold_resolution: str = "timeout",
    concurrent: int = 0,
    server: FakeToolServer | None = None,
    lookup_fail: bool = False,
) -> tuple[int, str, FakeToolServer, dict[str, FakeSdkClient]]:
    server = server or FakeToolServer()
    clients: dict[str, FakeSdkClient] = {}

    def factory(member_id: str) -> FakeSdkClient:
        clients[member_id] = FakeSdkClient(server, policy=policy, hold_resolution=hold_resolution)
        return clients[member_id]

    direct = e2.DirectConfig(
        bearer=FAKE_BEARER, base_url=OFFLINE_BASE_URL, base_url_source="offline test"
    )
    with httpx.Client(
        base_url=OFFLINE_BASE_URL, transport=httpx.MockTransport(server.http)
    ) as client:
        code, record = asyncio.run(
            probe.run_probe(
                offline_settings(),
                direct=direct,
                ledger_dsn=FAKE_DSN,
                hold_wait_s=5,
                concurrent=concurrent,
                sdk_client_factory=factory,
                direct_client=client,
                lookup_factory=lambda dsn: FakeEvidenceLookup(server, fail=lookup_fail),
            )
        )
    return code, record, server, clients


def _matrix_rows(record: str) -> dict[tuple[str, str], str]:
    rows = {}
    for line in record.splitlines():
        for persona in ("maya", "sam", "priya"):
            if line.startswith(f"| {persona} (") and "| `" in line:
                tool = line.split("`")[1]
                rows[(persona, tool)] = line
    return rows


def test_full_matrix_matches_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    code, record, server, clients = _run()
    assert code == 0, record
    rows = _matrix_rows(record)
    assert len(rows) == 12
    for key, expected in probe.EXPECTED.items():
        assert f"| {expected} | {expected} |" in rows[key], rows[key]
        assert "**MATCH**" in rows[key]
    assert "12/12 cells MATCH" in record
    # One client per member (three separate SDK clients).
    assert sorted(clients) == ["m-maya", "m-priya", "m-sam"]
    # Only the four ALLOW-expected... executed: 3 team + Maya conf + Maya restr.
    executed = [r for r in server.ledger_rows if r["execution_status"] == "executed"]
    assert len(executed) == 5
    assert not server.egress_rows
    err = capsys.readouterr().err
    assert "HOLD reported for sam knowledge_search_confidential" in err
    assert "delegation_id=dlg-1" in err
    assert_no_fake_values(record, err)


def test_hold_and_block_cells_carry_armoriq_evidence() -> None:
    _, record, _, _ = _run()
    rows = _matrix_rows(record)
    sam_conf = rows[("sam", "knowledge_search_confidential")]
    assert "armoriq_policy_decision" in sam_conf
    assert "delegation_id=dlg-1" in sam_conf
    assert "| no |" in sam_conf  # tool server not reached
    priya_restr = rows[("priya", "knowledge_search_restricted")]
    assert "SDK PolicyBlockedException" in priya_restr
    honeypot = rows[("maya", "send_external_message")]
    assert "BLOCK" in honeypot and "| no |" in honeypot


def test_ceiling_table_is_separate_and_enforced() -> None:
    _, record, server, _ = _run()
    section = record.split("#### Tool-server tier ceiling")[1]
    assert section.count("**enforced**") == 3
    assert "ledger rejection row" in section
    rejected = [r for r in server.ledger_rows if r["validation_outcome"] == "out_of_scope"]
    assert {r["requester_member_id"] for r in rejected} == {"m-sam", "m-priya"}


def test_ceiling_not_enforced_is_reported_but_does_not_change_the_matrix() -> None:
    code, record, _, _ = _run(server=FakeToolServer(enforce_ceiling=False))
    section = record.split("#### Tool-server tier ceiling")[1]
    assert section.count("**NOT enforced**") == 3
    assert code == 0  # the 12 cells still match; step 5 is reported separately


def test_sam_without_hold_fails_the_cell() -> None:
    def no_hold(email: str, action: str) -> str:
        if email == FAKE_EMAILS["sam"] and action == "knowledge_search_confidential":
            return "allow"
        return matrix_policy(email, action)

    code, record, _, _ = _run(policy=no_hold)
    assert code == 3
    row = _matrix_rows(record)[("sam", "knowledge_search_confidential")]
    assert "**FAIL**" in row and "EXECUTED on a non-ALLOW cell" in row


def test_block_by_the_tool_server_is_not_an_armoriq_decision() -> None:
    def too_permissive(email: str, action: str) -> str:
        if email == FAKE_EMAILS["priya"] and action == "knowledge_search_confidential":
            return "allow"
        return matrix_policy(email, action)

    code, record, _, _ = _run(policy=too_permissive)
    assert code == 3
    row = _matrix_rows(record)[("priya", "knowledge_search_confidential")]
    assert "tool_server_rule" in row
    assert "not an ArmorIQ decision" in row
    assert "tool-server contact: yes" in row


def test_approved_hold_during_e3_is_a_fail() -> None:
    code, record, _, _ = _run(hold_resolution="approved")
    assert code == 3
    row = _matrix_rows(record)[("sam", "knowledge_search_confidential")]
    assert "**FAIL**" in row


def test_concurrency_attribution_is_checked() -> None:
    code, record, _, _ = _run(concurrent=2)
    assert code == 0, record
    assert "6 team searches (3 members x 2), executed: 6" in record
    assert "Attribution errors (ledger requester_member_id vs invoking member): none" in record


def test_attribution_errors_pure() -> None:
    rows = [
        ("m-sam", "j1", {"requester_member_id": "m-sam"}),
        ("m-priya", "j2", {"requester_member_id": "m-sam"}),
        ("m-maya", None, None),
        ("m-maya", "j4", None),
    ]
    errors = probe.attribution_errors(rows)
    assert len(errors) == 3
    assert "jti j2: ledger requester_member_id='m-sam', invoked by m-priya" in errors


def _obs(**overrides: object) -> common.StepObservation:
    values: dict[str, object] = {
        "observed": "BLOCK",
        "source": "armoriq_policy_decision",
        "gateway_outcome": "block",
        "gateway_source": "armoriq_policy_decision",
        "reason": None,
        "evidence_ref": "SDK PolicyBlockedException",
        "invocation_status": "not_reached",
    }
    values.update(overrides)
    return common.StepObservation(**values)  # type: ignore[arg-type]


def test_cell_verdict_evidence_rule() -> None:
    assert probe.cell_verdict("BLOCK", _obs(), "no", ledger_ok=False)[0] == "MATCH"
    verdict, note = probe.cell_verdict(
        "BLOCK", _obs(source="armoriq_sdk_plan_check"), "no", ledger_ok=False
    )
    assert verdict == "FAIL" and "armoriq_sdk_plan_check" in note
    verdict, note = probe.cell_verdict("BLOCK", _obs(evidence_ref=None), "no", ledger_ok=False)
    assert verdict == "FAIL" and "no ArmorIQ evidence reference" in note
    verdict, note = probe.cell_verdict("BLOCK", _obs(), "unknown (no DB evidence)", ledger_ok=False)
    assert verdict == "FAIL" and "tool-server contact" in note
    verdict, _ = probe.cell_verdict("HOLD", _obs(), "no", ledger_ok=False)
    assert verdict == "FAIL"  # a BLOCK where a HOLD was expected


def test_cell_verdict_allow_needs_execution_and_ledger() -> None:
    allow = _obs(observed="ALLOW", invocation_status="executed", evidence_ref="armoriq_plan_id=x")
    assert probe.cell_verdict("ALLOW", allow, "yes", ledger_ok=True)[0] == "MATCH"
    verdict, note = probe.cell_verdict(
        "ALLOW", allow, "yes", ledger_ok=False, ledger_problems=["2 ledger row(s)"]
    )
    assert verdict == "FAIL" and "2 ledger row(s)" in note


def test_ceiling_verdict_pure() -> None:
    reply = e2.McpReply(200, "out_of_scope", "sse data frame", is_error=True)
    assert probe.ceiling_verdict(reply, [{"id": 4, "validation_outcome": "out_of_scope"}]) == (
        "enforced",
        "out_of_scope + ledger rejection row #4",
    )
    executed = e2.McpReply(200, None, "sse data frame", is_error=False)
    assert probe.ceiling_verdict(executed, [])[0] == "NOT enforced"
    assert probe.ceiling_verdict(e2.McpReply(None, None, "transport error (X)"), [])[0] == "unknown"


def test_preflight_database_failure_is_a_prerequisite_error() -> None:
    with pytest.raises(e2.PrerequisiteError):
        _run(lookup_fail=True)


def test_no_settings_exit_2_naming_only_missing_settings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert probe.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    for name in (
        "ARMORIQ_API_KEY",
        "PERSONA_MAYA_EMAIL",
        "PERSONA_SAM_EMAIL",
        "PERSONA_PRIYA_EMAIL",
        "CTX_SECRET",
        "TOOLSERVER_BEARER_MEMORY",
        "DATABASE_URL_TOOLSERVER",
    ):
        assert name in captured.err
    assert "TOOLSERVER_BASE_URL" not in captured.err


@pytest.mark.parametrize(
    "argv", [["--hold-wait-s", "1"], ["--concurrent", "50"], ["--unknown"], ["positional"]]
)
def test_bad_arguments_exit_2(argv: list[str]) -> None:
    assert probe.main(argv) == 2
