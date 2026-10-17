"""Offline E4 probe tests (no credentials, no network: never E4 evidence).

The real `ArmorIQGateway` runs with a fake SDK client whose HOLD resolves
as scripted (approved / rejected / timeout / approved with an expired
token); the fake tool server verifies the real `_ctx` and dedupes by jti.
Approval in the real flow happens only in the ArmorIQ console.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from apps.api.core.config import Settings
from scripts import probe_v2_common as common
from scripts import probe_v2_e2_offplan as e2
from scripts import probe_v2_e4_hold as probe
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
    "CTX_SECRET",
    "TOOLSERVER_BEARER_MEMORY",
    "DATABASE_URL_TOOLSERVER",
    "TOOLSERVER_BASE_URL",
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
    scenario: str,
    *,
    hold_resolution: str,
    policy=matrix_policy,
    token_validity_s: int | None = None,
) -> tuple[int, str, list[str], FakeToolServer]:
    server = FakeToolServer()
    announced: list[str] = []

    def factory(member_id: str) -> FakeSdkClient:
        return FakeSdkClient(server, policy=policy, hold_resolution=hold_resolution)

    direct = e2.DirectConfig(
        bearer=FAKE_BEARER, base_url=OFFLINE_BASE_URL, base_url_source="offline test"
    )
    with httpx.Client(
        base_url=OFFLINE_BASE_URL, transport=httpx.MockTransport(server.http)
    ) as client:
        code, record = asyncio.run(
            probe.run_probe(
                offline_settings(),
                scenario=scenario,
                wait_s=10,
                token_validity_s=token_validity_s,
                direct=direct,
                ledger_dsn=FAKE_DSN,
                sdk_client_factory=factory,
                direct_client=client,
                lookup_factory=lambda dsn: FakeEvidenceLookup(server),
                announce=announced.append,
            )
        )
    return code, record, announced, server


def test_approve_resumes_same_step_executes_once_and_dedupes_the_duplicate() -> None:
    code, record, announced, server = _run("approve", hold_resolution="approved")
    assert code == 0, record
    assert "PASS candidate: approved in the console" in record
    executed = [r for r in server.ledger_rows if r["execution_status"] == "executed"]
    assert len(executed) == 1
    assert executed[0]["requester_member_id"] == "m-sam"
    assert "Same plan/step on resume (ledger plan_id/step/jti): True" in record
    assert "code=replay" in record and f"original_entry_id={executed[0]['id']}" in record
    # The operator banner came while the SDK was waiting, with the delegation id.
    assert len(announced) == 1
    assert "delegation_id = dlg-1" in announced[0]
    assert "APPROVE it now" in announced[0]
    assert "Plans Governance" in announced[0]
    assert_no_fake_values(record, *announced)


def test_approve_without_an_approval_is_the_fallback() -> None:
    code, record, _, server = _run("approve", hold_resolution="timeout")
    assert code == 3
    assert "FALLBACK: pending, resume unverified" in record
    assert not server.ledger_rows


def test_reject_has_zero_executions() -> None:
    code, record, announced, server = _run("reject", hold_resolution="rejected")
    assert code == 0, record
    assert "PASS candidate: rejected in the console; zero executions" in record
    assert "REJECT it now" in announced[0]
    assert not server.ledger_rows and not server.attempt_rows


def test_timeout_has_zero_executions_and_no_new_plan() -> None:
    code, record, announced, server = _run("timeout", hold_resolution="timeout")
    assert code == 0, record
    assert "PASS candidate: wait timed out" in record
    assert "do NOTHING" in announced[0]
    assert not server.ledger_rows


def test_expiry_resume_fails_closed() -> None:
    code, record, announced, server = _run(
        "expiry", hold_resolution="approved_token_expired", token_validity_s=60
    )
    assert code == 0, record
    assert "resume after token expiry failed closed" in record
    assert "Token lifetime: requested 60 s" in record
    assert "intent token expires at" in announced[0]
    assert not server.ledger_rows


def test_executed_without_hold_is_a_fail() -> None:
    def no_hold(email: str, action: str) -> str:
        return "allow" if email == FAKE_EMAILS["sam"] else matrix_policy(email, action)

    code, record, announced, _ = _run("approve", hold_resolution="approved", policy=no_hold)
    assert code == 3
    assert "executed WITHOUT a HOLD" in record
    assert not announced


def _facts(**overrides: object) -> probe.E4Facts:
    values: dict[str, object] = {
        "hold_seen": True,
        "delegation_id": "dlg-1",
        "approval_state": "approved",
        "executed": True,
        "invocation_status": "executed",
        "error_code": None,
        "contact": "yes",
        "ledger_rows": 1,
        "ledger_ok": True,
        "same_plan_step": True,
        "result_after_hold": True,
        "replay_code": "replay",
        "replay_matches": True,
    }
    values.update(overrides)
    return probe.E4Facts(**values)  # type: ignore[arg-type]


def test_verdict_approve_needs_every_fact() -> None:
    assert probe.e4_verdict("approve", _facts())[1] is True
    for overrides in (
        {"ledger_rows": 2},
        {"same_plan_step": False},
        {"replay_code": "invalid_context"},
        {"replay_matches": False},
        {"approval_state": "pending"},
    ):
        label, ok = probe.e4_verdict("approve", _facts(**overrides))
        assert ok is False, overrides
        assert label.startswith("NOT PASS")


def test_verdict_approve_without_execution_is_fallback() -> None:
    label, ok = probe.e4_verdict(
        "approve", _facts(executed=False, approval_state="approved", error_code="context_expired")
    )
    assert ok is False
    assert "resume unverified" in label


@pytest.mark.parametrize(
    ("scenario", "state", "ok"),
    [
        ("reject", "rejected", True),
        ("reject", "timeout", False),
        ("timeout", "timeout", True),
        ("timeout", "rejected", False),
        ("expiry", None, True),
        ("expiry", "timeout", False),
    ],
)
def test_verdict_zero_execution_scenarios(scenario: str, state: str | None, ok: bool) -> None:
    facts = _facts(
        approval_state=state, executed=False, contact="no", ledger_rows=0, invocation_status=None
    )
    assert probe.e4_verdict(scenario, facts)[1] is ok


def test_verdict_without_hold_never_passes() -> None:
    for scenario in probe.SCENARIOS:
        assert probe.e4_verdict(scenario, _facts(hold_seen=False))[1] is False


def test_same_plan_step_pure() -> None:
    row = {"plan_id": "p-1", "step_index": 0, "jti": "j1"}
    assert probe.same_plan_step(row, plan_id="p-1", jti="j1") is True
    assert probe.same_plan_step(row, plan_id="p-2", jti="j1") is False
    assert probe.same_plan_step(row, plan_id="p-1", jti=None) is False
    assert probe.same_plan_step(None, plan_id="p-1", jti="j1") is False


def test_parse_args_defaults() -> None:
    args = probe.parse_args(["--scenario", "timeout"])
    assert args.wait_s == 60 and args.token_validity_s is None
    args = probe.parse_args(["--scenario", "expiry"])
    assert args.token_validity_s == 60 and args.wait_s == 240


@pytest.mark.parametrize(
    "argv",
    [[], ["--scenario", "maybe"], ["--scenario", "approve", "--wait-s", "600"],
     ["--scenario", "expiry", "--token-validity-s", "1"]],
)  # fmt: skip
def test_bad_arguments_exit_2(argv: list[str]) -> None:
    assert probe.main(argv) == 2


def test_no_settings_exit_2_naming_only_missing_settings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert probe.main(["--scenario", "reject"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    for name in (
        "ARMORIQ_API_KEY",
        "PERSONA_SAM_EMAIL",
        "CTX_SECRET",
        "TOOLSERVER_BEARER_MEMORY",
        "DATABASE_URL_TOOLSERVER",
    ):
        assert name in captured.err
    assert "PERSONA_MAYA_EMAIL" not in captured.err  # optional (masked approver only)
