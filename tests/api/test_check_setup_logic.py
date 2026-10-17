"""Offline tests for `scripts/check_setup.py` (P1-08 §11): pure comparison logic.

No credentials, no network, no database: never E5 evidence. Name-set diffs,
wrong agent id, persona e-mail problems (names only), the ALLOW evidence
rule and the exit-code mapping. A guard fails any test that would build the
real SDK client, a real HTTP client or a real Neon pool.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.core.config import Settings
from scripts import check_setup
from scripts import probe_v2_common as common
from services.contracts import TOOL_CATALOG

FAKE_EMAILS = {
    "PERSONA_MAYA_EMAIL": "maya.fake@example.invalid",
    "PERSONA_SAM_EMAIL": "sam.fake@example.invalid",
    "PERSONA_PRIYA_EMAIL": "priya.fake@example.invalid",
}
PROBE_ENV = (
    "ARMORIQ_API_KEY",
    "ARMORIQ_AGENT_ID",
    "CTX_SECRET",
    "TOOLSERVER_BEARER_MEMORY",
    "TOOLSERVER_BEARER_WORKSPACE",
    "DATABASE_URL_TOOLSERVER",
    "TOOLSERVER_BASE_URL",
    *FAKE_EMAILS,
)


@pytest.fixture(autouse=True)
def _never_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("offline test tried to go live")

    monkeypatch.setattr("services.authorization.armoriq_gateway.ArmorIQClient", _refuse)
    monkeypatch.setattr(check_setup.e2, "build_direct_client", _refuse)
    monkeypatch.setattr(common, "ProbeEvidenceLookup", _refuse)
    for name in PROBE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", (tmp_path / "none.env",))
    monkeypatch.setattr(check_setup.e1, "TOOLSERVER_ENV_FILE", tmp_path / "toolserver.env")


MEMORY = sorted(n.value for n, s in TOOL_CATALOG.items() if s.endpoint.value == "memory-mcp")
WORKSPACE = ["create_followup_task", "send_external_message"]


def test_catalog_split_is_twelve_and_two() -> None:
    assert len(check_setup.expected_tools("memory-mcp")) == 12
    assert check_setup.expected_tools("workspace-mcp") == frozenset(WORKSPACE)


def test_exact_tool_list_passes_in_any_order() -> None:
    result = check_setup.tool_list_diff("memory-mcp", list(reversed(MEMORY)))
    assert result.ok is True
    assert check_setup.tool_list_diff("workspace-mcp", WORKSPACE).ok is True


def test_missing_tools_are_named() -> None:
    live = [name for name in MEMORY if not name.startswith("knowledge_mark")]
    result = check_setup.tool_list_diff("memory-mcp", live)
    assert result.ok is False
    assert "missing ['knowledge_mark_superseded']" in result.detail


def test_extra_and_cross_endpoint_tools_are_named() -> None:
    result = check_setup.tool_list_diff(
        "workspace-mcp", [*WORKSPACE, "scaffold_ping", "knowledge_search_team"]
    )
    assert result.ok is False
    assert "extra ['knowledge_search_team', 'scaffold_ping']" in result.detail


def test_qualified_or_suffixed_names_are_a_mismatch() -> None:
    result = check_setup.tool_list_diff(
        "workspace-mcp", ["workspace-mcp__create_followup_task", "send_external_message_v2"]
    )
    assert result.ok is False
    assert "missing" in result.detail and "extra" in result.detail


def test_duplicate_tool_names_are_flagged() -> None:
    result = check_setup.tool_list_diff("workspace-mcp", [*WORKSPACE, "create_followup_task"])
    assert result.ok is False
    assert "duplicated ['create_followup_task']" in result.detail


def test_empty_tool_list_is_a_mismatch() -> None:
    assert check_setup.tool_list_diff("memory-mcp", []).ok is False


@pytest.mark.parametrize("value", ["proxylead"])
def test_agent_id_matches(value: str) -> None:
    assert check_setup.agent_id_check(value).ok is True


@pytest.mark.parametrize("value", ["ProxyLead", "proxylead-dev", "", None, "eae6515b"])
def test_wrong_agent_id_fails(value: str | None) -> None:
    result = check_setup.agent_id_check(value)
    assert result.ok is False
    assert 'expected "proxylead"' in result.detail


def test_distinct_lowercase_emails_pass() -> None:
    assert check_setup.persona_email_check(FAKE_EMAILS).ok is True


def test_duplicate_emails_fail_naming_settings_only() -> None:
    emails = {**FAKE_EMAILS, "PERSONA_PRIYA_EMAIL": "SAM.fake@example.invalid"}
    result = check_setup.persona_email_check(emails)
    assert result.ok is False
    assert "PERSONA_PRIYA_EMAIL and PERSONA_SAM_EMAIL are the same address" in result.detail
    assert "PERSONA_PRIYA_EMAIL is not lowercase" in result.detail
    for value in (*FAKE_EMAILS.values(), "SAM.fake"):
        assert value not in result.detail


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        (None, "PERSONA_SAM_EMAIL unset"),
        ("   ", "PERSONA_SAM_EMAIL unset"),
        ("not-an-address", "PERSONA_SAM_EMAIL is not an e-mail address"),
        (" sam.fake@example.invalid", "PERSONA_SAM_EMAIL has surrounding whitespace"),
    ],
)
def test_email_problems_are_named(value: str | None, problem: str) -> None:
    result = check_setup.persona_email_check({**FAKE_EMAILS, "PERSONA_SAM_EMAIL": value})
    assert result.ok is False
    assert problem in result.detail


def _obs(**overrides: object) -> common.StepObservation:
    values: dict[str, object] = {
        "observed": "ALLOW",
        "source": "armoriq_policy_decision",
        "gateway_outcome": "allow",
        "gateway_source": "armoriq_policy_decision",
        "reason": None,
        "evidence_ref": "armoriq_plan_id=ap-1",
        "invocation_status": "executed",
        "ledger_entry_id": "7",
        "jti": "j1",
    }
    values.update(overrides)
    return common.StepObservation(**values)  # type: ignore[arg-type]


def test_allow_with_full_evidence_passes() -> None:
    result = check_setup.allow_check("maya", "m***@e***.invalid", _obs(), "yes", True, [])
    assert result.ok is True
    assert "ledger #7 correlated" in result.detail


@pytest.mark.parametrize(
    ("overrides", "contact", "ledger_ok", "problem"),
    [
        ({"observed": "BLOCK", "source": "armoriq_policy_decision"}, "no", False, "observed BLOCK"),
        ({"source": "unknown"}, "yes", True, "source unknown"),
        ({"evidence_ref": None}, "yes", True, "no ArmorIQ evidence reference"),
        ({"invocation_status": "unknown"}, "yes", True, "not executed"),
        ({}, "unknown (no DB evidence)", False, "tool-server contact"),
        ({}, "yes", False, "ledger"),
    ],
)
def test_allow_without_evidence_fails(
    overrides: dict, contact: str, ledger_ok: bool, problem: str
) -> None:
    result = check_setup.allow_check(
        "sam", "s***@e***.invalid", _obs(**overrides), contact, ledger_ok, []
    )
    assert result.ok is False
    assert problem in result.detail


def test_exit_code_mapping() -> None:
    passed = check_setup.CheckResult("a", True, "")
    failed = check_setup.CheckResult("b", False, "")
    skipped = check_setup.CheckResult("c", None, "")
    assert check_setup.exit_code([passed, passed]) == 0
    assert check_setup.exit_code([passed, failed]) == 3
    assert check_setup.exit_code([passed, skipped]) == 3
    assert check_setup.exit_code([]) == 3
    assert check_setup.exit_code([passed], missing=["CTX_SECRET"]) == 2


def test_render_marks_each_check() -> None:
    text = check_setup.render(
        [
            check_setup.CheckResult("one", True, "ok"),
            check_setup.CheckResult("two", False, "bad"),
            check_setup.CheckResult("three", None, "skipped"),
        ],
        code=3,
    )
    assert "- [PASS] one: ok" in text
    assert "- [FAIL] two: bad" in text
    assert "- [SKIP] three: skipped" in text
    assert "Exit code: 3" in text


def test_no_settings_exit_2_naming_only_missing_settings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check_setup.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    for name in (
        "ARMORIQ_API_KEY",
        *FAKE_EMAILS,
        "CTX_SECRET",
        "TOOLSERVER_BEARER_MEMORY",
        "TOOLSERVER_BEARER_WORKSPACE",
        "DATABASE_URL_TOOLSERVER",
    ):
        assert name in captured.err
    assert "ARMORIQ_AGENT_ID" not in captured.err  # has a default


def test_arguments_are_refused() -> None:
    assert check_setup.main(["--live"]) == 2


def test_wrong_agent_id_skips_live_checks_and_exits_3() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        armoriq_agent_id="proxylead-typo",
        persona_maya_email=FAKE_EMAILS["PERSONA_MAYA_EMAIL"],
        persona_sam_email=FAKE_EMAILS["PERSONA_SAM_EMAIL"],
        persona_priya_email=FAKE_EMAILS["PERSONA_PRIYA_EMAIL"],
    )
    import asyncio

    direct = check_setup.e2.DirectConfig(
        bearer=None, base_url="https://toolserver.example.invalid", base_url_source="test"
    )
    code, report = asyncio.run(
        check_setup.run_check(
            settings,
            bearers={},
            ledger_dsn=None,
            direct=direct,
            http_client=SimpleNamespace(),
        )
    )
    assert code == 3
    assert "[FAIL] ARMORIQ_AGENT_ID" in report
    assert "[SKIP] live checks" in report
    for value in FAKE_EMAILS.values():
        assert value not in report
