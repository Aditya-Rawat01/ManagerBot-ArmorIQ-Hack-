"""Offline E5 probe and check_setup end-to-end tests (never E5 evidence).

The real `ArmorIQGateway` runs with fake SDK clients; the E5 renaming
wrapper sits at the gateway's `client_factory` seam exactly as in the live
run. `check_setup.run_check` runs end to end over the same fakes.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from apps.api.core.config import Settings
from scripts import check_setup
from scripts import probe_v2_common as common
from scripts import probe_v2_e2_offplan as e2
from scripts import probe_v2_e5_setup as probe
from tests.api.fakes.probe_sdk import (
    FAKE_BEARER,
    FAKE_BEARER_WORKSPACE,
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
    "CTX_SECRET",
    "DATABASE_URL_TOOLSERVER",
    "TOOLSERVER_BASE_URL",
    "TOOLSERVER_BEARER_MEMORY",
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


def _direct() -> e2.DirectConfig:
    return e2.DirectConfig(
        bearer=FAKE_BEARER, base_url=OFFLINE_BASE_URL, base_url_source="offline test"
    )


def _run_e5(
    *, policy=matrix_policy, mint_policy=None
) -> tuple[int, str, FakeToolServer, list[FakeSdkClient]]:
    server = FakeToolServer()
    clients: list[FakeSdkClient] = []

    def factory(member_id: str) -> FakeSdkClient:
        clients.append(FakeSdkClient(server, policy=policy, mint_policy=mint_policy))
        return clients[-1]

    with httpx.Client(
        base_url=OFFLINE_BASE_URL, transport=httpx.MockTransport(server.http)
    ) as client:
        code, record = asyncio.run(
            probe.run_probe(
                offline_settings(),
                direct=_direct(),
                ledger_dsn=FAKE_DSN,
                sdk_client_factory=factory,
                direct_client=client,
                lookup_factory=lambda dsn: FakeEvidenceLookup(server),
            )
        )
    return code, record, server, clients


def _case_row(record: str, case: str) -> str:
    return next(line for line in record.splitlines() if line.startswith(f"| `{case}` |"))


def test_misnamed_tools_blocked_by_policy_are_armoriq_evidence() -> None:
    code, record, server, clients = _run_e5()
    assert code == 0, record
    # ArmorIQ saw the misnamed action at mint and at invoke; never the catalog name.
    minted = [name for client in clients for names in client.minted for name in names]
    assert minted == [f"memory-mcp__{name}" for _, name in probe.CASES]
    invoked = [action for client in clients for _, action in client.invoked]
    assert invoked == [name for _, name in probe.CASES]
    for case, _ in probe.CASES:
        row = _case_row(record, case)
        assert "armoriq_policy_decision" in row
        assert "SDK PolicyBlockedException" in row
        assert "fail-closed: BLOCK" in row
        assert "ArmorIQ evidence (may be cited" in row
    assert not server.ledger_rows and not server.attempt_rows
    assert_no_fake_values(record)


def test_forwarded_misname_refused_by_the_tool_server_is_fail_closed_only() -> None:
    code, record, server, _ = _run_e5(policy=lambda email, action: "allow")
    assert code == 0, record  # fail-closed with a named layer
    for case, _ in probe.CASES:
        row = _case_row(record, case)
        assert "tool_server_rule" in row
        assert "fail-closed only" in row
        assert "ArmorIQ evidence (may be cited" not in row
    assert server.unknown_names == [name for _, name in probe.CASES]
    assert not server.ledger_rows


def test_unknown_layer_exits_3() -> None:
    code, record, _, _ = _run_e5(policy=lambda email, action: "proxy404")
    assert code == 3
    assert "| unknown |" in _case_row(record, "suffix")


def test_mint_block_is_recorded_at_mint() -> None:
    code, record, _, clients = _run_e5(mint_policy=lambda email, names: "unknown tool name")
    assert code == 0, record
    assert "(at mint)" in _case_row(record, "suffix")
    assert all(not client.invoked for client in clients)


def test_case_verdict_pure() -> None:
    def obs(**overrides: object) -> common.StepObservation:
        values: dict[str, object] = {
            "observed": "BLOCK",
            "source": "armoriq_policy_decision",
            "gateway_outcome": "block",
            "gateway_source": "armoriq_policy_decision",
            "reason": None,
            "evidence_ref": "SDK PolicyBlockedException",
        }
        values.update(overrides)
        return common.StepObservation(**values)  # type: ignore[arg-type]

    allowed = probe.case_verdict(obs(observed="ALLOW", invocation_status="executed"), "yes")
    assert allowed["fail_closed"] is False and allowed["result"].startswith("FAIL")
    unconfirmed = probe.case_verdict(obs(), "unknown (no DB evidence)")
    assert unconfirmed["fail_closed"] is None
    plan_check = probe.case_verdict(obs(source="armoriq_sdk_plan_check", evidence_ref=None), "no")
    assert plan_check["fail_closed"] is True
    assert "never cited as the ArmorIQ policy" in plan_check["evidence_class"]
    assert probe.exit_code([]) == 3
    assert probe.exit_code([plan_check]) == 0
    assert probe.exit_code([plan_check, unconfirmed]) == 3
    assert probe.exit_code([{**plan_check, "layer": "unknown"}]) == 3


def test_renaming_client_rewrites_plan_and_invoke_only() -> None:
    seen: dict[str, object] = {}

    class Inner:
        marker = "passthrough"

        def for_user(self, email: str) -> "Inner":
            return self

        def start_session(self, options: object = None) -> SimpleNamespace:
            return SimpleNamespace(
                start_plan=lambda calls, goal=None: seen.setdefault("plan", calls),
                close=lambda: None,
            )

        def invoke_with_policy(self, mcp: str, action: str, *args: object, **kw: object) -> str:
            seen["action"] = action
            return "ok"

    rename = {"knowledge_search_team": "knowledge_search_team_e5"}
    client = probe.RenamingClient(Inner(), rename)
    session = client.for_user("x").start_session()
    session.start_plan([{"name": "memory-mcp__knowledge_search_team", "args": {}}], goal="g")
    assert seen["plan"] == [{"name": "memory-mcp__knowledge_search_team_e5", "args": {}}]
    assert client.invoke_with_policy("memory-mcp", "knowledge_search_team", "tok") == "ok"
    assert seen["action"] == "knowledge_search_team_e5"
    assert client.marker == "passthrough"
    rename.clear()
    client.invoke_with_policy("memory-mcp", "knowledge_search_team", "tok")
    assert seen["action"] == "knowledge_search_team"


def test_no_settings_exit_2_naming_only_missing_settings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert probe.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    for name in ("ARMORIQ_API_KEY", "PERSONA_MAYA_EMAIL", "CTX_SECRET", "DATABASE_URL_TOOLSERVER"):
        assert name in captured.err
    assert probe.main(["--x"]) == 2


# --- check_setup end to end over the fakes -------------------------------------------


def _run_check(server: FakeToolServer, *, policy=matrix_policy) -> tuple[int, str]:
    def factory(member_id: str) -> FakeSdkClient:
        return FakeSdkClient(server, policy=policy)

    bearers = {"memory-mcp": FAKE_BEARER, "workspace-mcp": FAKE_BEARER_WORKSPACE}
    with httpx.Client(
        base_url=OFFLINE_BASE_URL, transport=httpx.MockTransport(server.http)
    ) as client:
        return asyncio.run(
            check_setup.run_check(
                offline_settings(),
                bearers=bearers,
                ledger_dsn=FAKE_DSN,
                direct=_direct(),
                http_client=client,
                sdk_client_factory=factory,
                lookup_factory=lambda dsn: FakeEvidenceLookup(server),
            )
        )


def test_check_setup_passes_against_a_conforming_setup() -> None:
    server = FakeToolServer()
    code, report = _run_check(server)
    assert code == 0, report
    assert report.count("[PASS]") == 7
    assert "[PASS] tools/list memory-mcp: 12 tools, exact match" in report
    for persona in ("maya", "sam", "priya"):
        assert f"[PASS] ALLOW {persona} (" in report
    executed = {r["requester_member_id"] for r in server.ledger_rows}
    assert executed == {"m-maya", "m-sam", "m-priya"}
    assert_no_fake_values(report)


def test_check_setup_fails_on_a_tool_list_mismatch() -> None:
    lists = {
        "memory-mcp": [
            n for n in check_setup.expected_tools("memory-mcp") if n != "knowledge_mark_superseded"
        ],
        "workspace-mcp": ["create_followup_task", "send_external_message", "scaffold_ping"],
    }
    code, report = _run_check(FakeToolServer(tool_lists=lists))
    assert code == 3
    assert "[FAIL] tools/list memory-mcp: missing ['knowledge_mark_superseded']" in report
    assert "[FAIL] tools/list workspace-mcp: extra ['scaffold_ping']" in report


def test_check_setup_fails_when_a_member_is_not_allowed() -> None:
    def priya_blocked(email: str, action: str) -> str:
        return "block" if email == FAKE_EMAILS["priya"] else matrix_policy(email, action)

    code, report = _run_check(FakeToolServer(), policy=priya_blocked)
    assert code == 3
    assert "[FAIL] ALLOW priya (" in report
    assert "observed BLOCK (armoriq_policy_decision)" in report
    assert_no_fake_values(report)
