"""Setup conformance check for ProxyLead's ArmorIQ wiring (P1-08 §9.5, #9, E5).

Checks, without printing a secret, a token or an e-mail:
  1. the required settings are present (names only);
  2. ARMORIQ_AGENT_ID == "proxylead";
  3. the three persona e-mails are configured, lowercase, well-formed and distinct;
  4. each endpoint's live `tools/list` equals its `TOOL_CATALOG` subset
     (memory-mcp: 12 tools, workspace-mcp: 2), via a direct bearer-only call;
  5. one real ALLOW per member on `knowledge_search_team` through
     `ArmorIQGateway` (ADR-018), with evidence: source
     `armoriq_policy_decision`, an ArmorIQ evidence reference (armoriq_plan_id),
     EXECUTED, and exactly one ledger row correlated with the run (read-only,
     toolserver role).
Live checks (4, 5) run only when 1-3 pass. Nothing runs on import or in CI.

Usage (operator only, repository root; no arguments):
    python scripts/check_setup.py

Configuration is read like the E1 probe: the API `Settings` (process
environment, then the repo-root `.env` and `apps/api/.env`); the bearers and
DATABASE_URL_TOOLSERVER also from `services/toolserver/.env`.
Required: ARMORIQ_API_KEY, PERSONA_MAYA_EMAIL, PERSONA_SAM_EMAIL,
PERSONA_PRIYA_EMAIL, CTX_SECRET, TOOLSERVER_BEARER_MEMORY,
TOOLSERVER_BEARER_WORKSPACE, DATABASE_URL_TOOLSERVER. Optional:
ARMORIQ_AGENT_ID (default `proxylead`), TOOLSERVER_BASE_URL.

Exit codes:
    0  every check passed
    1  an unexpected error stopped the check (exception type only)
    2  prerequisites missing or unusable: arguments given, settings
       unreadable or unset (names only), a bad base URL, the tool server not
       answering `/health` 200, or the evidence database unusable
    3  the check ran and at least one check FAILED (a mismatch); read the list
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import probe_v2_common as common
from scripts import probe_v2_e1_allowed as e1
from scripts import probe_v2_e2_offplan as e2

EXPECTED_AGENT_ID = "proxylead"
ALLOW_TOOL = "knowledge_search_team"
HOLD_WAIT_S = 30.0  # a team search must never HOLD; never wait 15 minutes on one
REQUIRED_SETTINGS = (
    ("armoriq_api_key", "ARMORIQ_API_KEY"),
    ("persona_maya_email", "PERSONA_MAYA_EMAIL"),
    ("persona_sam_email", "PERSONA_SAM_EMAIL"),
    ("persona_priya_email", "PERSONA_PRIYA_EMAIL"),
    ("ctx_secret", "CTX_SECRET"),
)
BEARER_NAMES = {
    "memory-mcp": "TOOLSERVER_BEARER_MEMORY",
    "workspace-mcp": "TOOLSERVER_BEARER_WORKSPACE",
}
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class CheckResult:
    """One line of the checklist. ``ok`` None = skipped (not a pass)."""

    name: str
    ok: bool | None
    detail: str


# --- pure comparison logic (offline-tested: tests/api/test_check_setup_logic.py) ----


def expected_tools(endpoint: str) -> frozenset[str]:
    """The `TOOL_CATALOG` names served by one endpoint."""
    from services.contracts import TOOL_CATALOG

    return frozenset(
        name.value for name, spec in TOOL_CATALOG.items() if spec.endpoint.value == endpoint
    )


def tool_list_diff(endpoint: str, live: Iterable[str]) -> CheckResult:
    """Live `tools/list` names vs the catalog subset: missing, extra, duplicates."""
    names = list(live)
    expected = expected_tools(endpoint)
    seen = set(names)
    missing = sorted(expected - seen)
    extra = sorted(seen - expected)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    problems = []
    if missing:
        problems.append(f"missing {missing}")
    if extra:
        problems.append(f"extra {extra}")
    if duplicates:
        problems.append(f"duplicated {duplicates}")
    if problems:
        return CheckResult(f"tools/list {endpoint}", False, "; ".join(problems))
    return CheckResult(f"tools/list {endpoint}", True, f"{len(expected)} tools, exact match")


def agent_id_check(agent_id: str | None) -> CheckResult:
    value = (agent_id or "").strip()
    if value == EXPECTED_AGENT_ID:
        return CheckResult("ARMORIQ_AGENT_ID", True, f'"{EXPECTED_AGENT_ID}"')
    # The configured value is a name, not a secret: shown so a typo is visible.
    return CheckResult(
        "ARMORIQ_AGENT_ID", False, f'expected "{EXPECTED_AGENT_ID}", configured {value!r}'
    )


def persona_email_check(emails: Mapping[str, str | None]) -> CheckResult:
    """Configured, well-formed, lowercase and distinct. Setting NAMES only.

    ``emails`` maps the env name (PERSONA_MAYA_EMAIL ...) to the raw value.
    The policy is keyed by exact lowercase e-mails (PRD §5.3), so an
    uppercase letter or surrounding space is a mismatch, not a style issue.
    """
    problems: list[str] = []
    normalized: dict[str, str] = {}
    for name, raw in emails.items():
        value = raw or ""
        if not value.strip():
            problems.append(f"{name} unset")
            continue
        if value != value.strip():
            problems.append(f"{name} has surrounding whitespace")
        if not _EMAIL_SHAPE.match(value.strip()):
            problems.append(f"{name} is not an e-mail address")
        if value.strip() != value.strip().lower():
            problems.append(f"{name} is not lowercase")
        normalized[name] = value.strip().lower()
    by_value: dict[str, list[str]] = {}
    for name, value in normalized.items():
        by_value.setdefault(value, []).append(name)
    for names in by_value.values():
        if len(names) > 1:
            problems.append(f"{' and '.join(sorted(names))} are the same address")
    if problems:
        return CheckResult("persona e-mails", False, "; ".join(problems))
    return CheckResult("persona e-mails", True, f"{len(emails)} configured, lowercase, distinct")


def allow_check(
    persona: str,
    masked: str,
    obs: common.StepObservation | None,
    contact: str,
    ledger_ok: bool,
    mismatches: list[str],
) -> CheckResult:
    """One member's real ALLOW, under the ArmorIQ evidence rule."""
    name = f"ALLOW {persona} ({masked}) {ALLOW_TOOL}"
    if obs is None:
        return CheckResult(name, False, "no gateway result")
    problems = []
    if obs.observed != "ALLOW":
        problems.append(f"observed {obs.observed} ({obs.source}): {obs.reason}")
    elif obs.source != "armoriq_policy_decision":
        problems.append(f"source {obs.source}, not armoriq_policy_decision")
    if not obs.evidence_ref:
        problems.append("no ArmorIQ evidence reference")
    if not obs.executed:
        problems.append(f"not executed (status={obs.invocation_status}, code={obs.error_code})")
    if contact != "yes":
        problems.append(f"tool-server contact: {contact}")
    if not ledger_ok:
        problems.append("ledger: " + ("; ".join(mismatches) or "not correlated"))
    if problems:
        return CheckResult(name, False, "; ".join(problems))
    return CheckResult(
        name,
        True,
        f"armoriq_policy_decision; {obs.evidence_ref}; ledger #{obs.ledger_entry_id} correlated",
    )


def exit_code(results: Iterable[CheckResult], missing: Iterable[str] = ()) -> int:
    """2 when settings are missing, 3 when any check failed or a check was
    skipped after a failure, 0 only when every check passed."""
    if list(missing):
        return common.EXIT_PREREQUISITES
    items = list(results)
    if not items or any(item.ok is not True for item in items):
        return common.EXIT_NOT_PASS
    return common.EXIT_PASS


def render(results: Iterable[CheckResult], *, code: int, header: Iterable[str] = ()) -> str:
    lines = ["### check_setup — ProxyLead ArmorIQ setup conformance", *header, ""]
    for item in results:
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[item.ok]
        lines.append(f"- [{mark}] {item.name}: {item.detail}")
    lines.append("")
    lines.append(f"Exit code: {code} (0 = every check passed; 3 = a check failed or was skipped)")
    return "\n".join(lines)


# --- live checks (operator path; offline tests use fakes through the seams) ---------


def static_checks(settings: Any) -> list[CheckResult]:
    emails = {
        env: e1.secret_text(getattr(settings, f"persona_{persona}_email", None)) or None
        for persona, env in common.PERSONA_EMAIL_ENV.items()
    }
    agent_id = getattr(settings, "armoriq_agent_id", None)
    return [agent_id_check(agent_id), persona_email_check(emails)]


async def member_allows(
    settings: Any,
    *,
    ledger_dsn: str | None,
    sdk_client_factory: Callable[[str], Any] | None = None,
    lookup_factory: Callable[[str], Any] | None = None,
) -> list[CheckResult]:
    """One planned `knowledge_search_team` per member, then read-only evidence."""
    from services.authorization import armoriq_gateway

    recorder = e2.SdkRecorder()
    observer = e2.Observer(recorder)
    holder: dict[str, Any] = {}

    def recording_factory(member_id: str) -> Any:
        inner = sdk_client_factory or holder["gateway"]._build_client
        return e2.RecordingClient(inner(member_id), recorder)

    gateway = armoriq_gateway.ArmorIQGateway(
        common.with_approval_wait(settings, HOLD_WAIT_S), client_factory=recording_factory
    )
    holder["gateway"] = gateway
    started = e2.utc_now()
    runs: list[tuple[str, Any, Any, common.StepObservation]] = []
    for persona in common.PERSONA_ORDER:
        ctx, plan = common.build_member_request(
            settings, persona, ALLOW_TOOL, prefix="setup", summary="check_setup: team search"
        )
        handle = await gateway.start_plan(ctx, plan)
        if handle.decision is not None:
            obs = common.observe_step(handle.decision, at_mint=True)
        else:
            holds = common.HoldRecorder()
            with observer.window() as window:
                outcome = await gateway.invoke_step(handle, 0, ctx, plan, on_hold=holds)
            obs = common.observe_step(
                outcome.decision,
                approval=outcome.approval,
                invocation=outcome.invocation,
                holds=holds.events,
                window=window,
            )
        runs.append((persona, ctx, handle, obs))
    evidence = await asyncio.to_thread(
        common.collect_evidence,
        ledger_dsn,
        request_ids=[ctx.request_id for _, ctx, _, _ in runs],
        jtis=[obs.jti for *_, obs in runs if obs.jti],
        started_at=started,
        ended_at=e2.utc_now(),
        tools=[ALLOW_TOOL],
        lookup_factory=lookup_factory,
    )
    results = []
    for persona, ctx, handle, obs in runs:
        mine = common.rows_for(evidence, ctx.request_id, obs.jti)
        run = e1.RunIdentifiers(
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            plan_id=handle.plan_id,
            requester_member_id=ctx.requester.member_id,
            jti=obs.jti,
            ledger_entry_id=obs.ledger_entry_id,
            tool_name=ALLOW_TOOL,
        )
        ok, mismatches, _ = common.ledger_check(mine, run)
        results.append(
            allow_check(
                persona,
                ctx.requester.masked_email,
                obs,
                common.tool_server_contact(evidence, mine),
                ok,
                mismatches if evidence["status"] == "ok" else [f"DB {evidence['status']}"],
            )
        )
    return results


def endpoint_checks(client: Any, bearers: Mapping[str, str | None]) -> list[CheckResult]:
    results = []
    for endpoint, path in common.ENDPOINT_PATHS.items():
        names, why = common.list_tool_names(client, path, bearers.get(endpoint))
        if names is None:
            results.append(CheckResult(f"tools/list {endpoint}", False, f"no list ({why})"))
        else:
            results.append(tool_list_diff(endpoint, names))
    return results


async def run_check(
    settings: Any,
    *,
    bearers: Mapping[str, str | None],
    ledger_dsn: str | None,
    direct: e2.DirectConfig,
    http_client: Any = None,
    sdk_client_factory: Callable[[str], Any] | None = None,
    lookup_factory: Callable[[str], Any] | None = None,
) -> tuple[int, str]:
    """Static checks, then (only if they pass) the live checks. Seams for tests."""
    from services.authorization import armoriq_gateway

    header = [
        f"- Run at (UTC): {datetime.now(UTC).isoformat()}",
        f"- SDK version: armoriq-sdk {armoriq_gateway.SDK_VERSION}",
        f"- Tool-server base URL (no secrets): {direct.base_url} ({direct.base_url_source})",
        "- Members (masked): "
        + ", ".join(f"{p}={common.masked_identity(settings, p)}" for p in common.PERSONA_ORDER),
    ]
    results = static_checks(settings)
    if not all(item.ok for item in results):
        results.append(CheckResult("live checks", None, "skipped: fix the failed checks first"))
        code = exit_code(results)
        return code, render(results, code=code, header=header)
    client = http_client if http_client is not None else e2.build_direct_client(direct.base_url)
    try:
        await asyncio.to_thread(e2.preflight, client, ledger_dsn, lookup_factory)
        results.extend(await asyncio.to_thread(endpoint_checks, client, bearers))
    finally:
        if http_client is None:
            client.close()
    results.extend(
        await member_allows(
            settings,
            ledger_dsn=ledger_dsn,
            sdk_client_factory=sdk_client_factory,
            lookup_factory=lookup_factory,
        )
    )
    code = exit_code(results)
    return code, render(results, code=code, header=header)


def _fail(message: str) -> int:
    print(f"check_setup: {message}", file=sys.stderr)
    return common.EXIT_PREREQUISITES


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        return _fail("no arguments accepted")
    try:
        settings = e1.load_settings()
        direct = e2.load_direct_config(settings)
        bearers = common.load_bearers()
        ledger_dsn = e1.load_ledger_dsn()
    except Exception as exc:
        # Type name only: a validation message can echo the offending value.
        return _fail(f"settings could not be loaded ({type(exc).__name__})")
    missing = common.missing_names(settings, REQUIRED_SETTINGS)
    missing += [BEARER_NAMES[name] for name, value in bearers.items() if not value]
    if not ledger_dsn:
        missing.append("DATABASE_URL_TOOLSERVER")
    if missing:
        return _fail("missing required settings (environment or .env): " + ", ".join(missing))
    if direct.base_url_problem is not None:
        return _fail(direct.base_url_problem)
    try:
        with e1.gateway_logs_to_stderr():
            code, report = asyncio.run(
                run_check(settings, bearers=bearers, ledger_dsn=ledger_dsn, direct=direct)
            )
    except e2.PrerequisiteError as exc:
        return _fail(str(exc))
    except Exception as exc:
        print(f"check_setup: check error ({type(exc).__name__})", file=sys.stderr)
        return common.EXIT_ERROR
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
