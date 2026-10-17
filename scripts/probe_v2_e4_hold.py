"""E4 probe: HOLD → ArmorIQ console approval/rejection → SDK resume (P1-10, #11).

Sam calls `knowledge_search_confidential` through the real `ArmorIQGateway`
(ADR-018) with waiting enabled. The moment the SDK reports the HOLD, the
probe prints on stderr the delegation id, the request id, what to do in
the console and the deadline, so the person holding the Maya account can
act live. The approval itself happens ONLY in the ArmorIQ console: this
probe never approves, never re-plans, never retries.

Scenarios (`--scenario`, required):
    approve  Maya approves in the console. Expected: the SDK resumes the SAME
             plan and step (same jti, plan_id, step 0), the tool server
             executes exactly once (one ledger row), and a duplicate of the
             approved invocation (the same `_ctx` replayed directly) is
             deduplicated by jti (`replay`, original_entry_id = that row).
             Approve within 5 minutes: the signed `_ctx` lives CTX_TTL_S
             (300 s) and is not re-signed on resume.
    reject   Maya rejects. Expected: no execution, no ledger/attempt row.
    timeout  Nobody acts. Expected: the SDK wait ends (`timeout`), no
             execution, no new plan.
    expiry   Short intent-token lifetime (`--token-validity-s`, default 60);
             Maya approves AFTER the printed token-expiry time. Expected: the
             resume fails closed (no execution). The record shows the
             requested vs granted token lifetime (PRD §7.1).
Options: `--wait-s` (SDK approval wait; default 240 for approve/reject/
expiry, 60 for timeout; 10..290), `--token-validity-s` (default: the
TOKEN_VALIDITY_S setting; 60 for expiry).

When approve → resume cannot be shown (nobody approved in time, or the
resume did not execute), the verdict is the documented fallback "pending,
resume unverified" (Arch §12 E4), never a PASS.

Usage (operator only, repository root):
    python scripts/probe_v2_e4_hold.py --scenario approve
    python scripts/probe_v2_e4_hold.py --scenario reject
    python scripts/probe_v2_e4_hold.py --scenario timeout --wait-s 60
    python scripts/probe_v2_e4_hold.py --scenario expiry --token-validity-s 60

Configuration as E1/E2 (API `Settings`, `.env` files, `services/toolserver/.env`).
Required: ARMORIQ_API_KEY, PERSONA_SAM_EMAIL, CTX_SECRET, DATABASE_URL_TOOLSERVER,
and TOOLSERVER_BEARER_MEMORY (the duplicate check). Optional: ARMORIQ_AGENT_ID,
TOOLSERVER_BASE_URL, PERSONA_MAYA_EMAIL (shown masked as the approver).

Exit codes:
    0  the scenario's PASS candidate (the operator still records console facts)
    1  an unexpected error stopped the probe (exception type only)
    2  prerequisites missing or unusable (names only), before any ArmorIQ call
    3  the probe ran, but the scenario is not shown (includes the fallback
       "pending, resume unverified"); read the record
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import probe_v2_common as common
from scripts import probe_v2_e1_allowed as e1
from scripts import probe_v2_e2_offplan as e2

PREFIX = "e4"
TOOL = "knowledge_search_confidential"
PERSONA = "sam"
SCENARIOS = ("approve", "reject", "timeout", "expiry")
DEFAULT_WAIT_S = {"approve": 240.0, "reject": 240.0, "timeout": 60.0, "expiry": 240.0}
WAIT_RANGE = (10.0, 290.0)  # stays below CTX_TTL_S (300 s): a later resume is context_expired
EXPIRY_TOKEN_VALIDITY_S = 60
INSTRUCTIONS = {
    "approve": "APPROVE it now",
    "reject": "REJECT it now",
    "timeout": "do NOTHING (let the wait run out)",
    "expiry": "wait until the token-expiry time below, THEN approve it",
}
CONSOLE_PATH = "ArmorIQ console → Governance → Plans Governance → filter “Needs you”"
REQUIRED_SETTINGS = (
    ("armoriq_api_key", "ARMORIQ_API_KEY"),
    ("persona_sam_email", "PERSONA_SAM_EMAIL"),
    ("ctx_secret", "CTX_SECRET"),
)


@dataclass(frozen=True)
class E4Facts:
    """What one run showed (identifiers and states only)."""

    hold_seen: bool
    delegation_id: str | None
    approval_state: str | None
    executed: bool
    invocation_status: str | None
    error_code: str | None
    contact: str
    ledger_rows: int
    ledger_ok: bool
    same_plan_step: bool
    result_after_hold: bool
    replay_code: str | None = None
    replay_matches: bool | None = None


# --- pure verdict (offline-tested) ---------------------------------------------------


def e4_verdict(scenario: str, facts: E4Facts) -> tuple[str, bool]:
    """(label, pass_candidate) for one scenario. Never PASS without evidence."""
    if not facts.hold_seen:
        if facts.executed:
            return "FAIL: executed WITHOUT a HOLD (the policy does not hold Sam here)", False
        return (
            f"FAIL: no HOLD observed (invocation={facts.invocation_status}, "
            f"code={facts.error_code})",
            False,
        )
    state = facts.approval_state
    if scenario == "approve":
        if not facts.executed:
            if state in (None, "pending", "timeout"):
                return "FALLBACK: pending, resume unverified (no approval within the wait)", False
            if state == "approved":
                return (
                    f"FALLBACK: approved but the resume did not execute "
                    f"(status={facts.invocation_status}, code={facts.error_code}); "
                    "resume unverified",
                    False,
                )
            return f"NOT SHOWN: the HOLD ended {state}, not approved", False
        problems = []
        if state != "approved":
            problems.append(f"approval state {state} (expected approved)")
        if not facts.result_after_hold:
            problems.append("a result was returned before the hold was reported")
        if facts.ledger_rows != 1 or not facts.ledger_ok:
            problems.append(f"{facts.ledger_rows} ledger row(s), correlated={facts.ledger_ok}")
        if not facts.same_plan_step:
            problems.append("ledger row does not show the same plan/step/jti")
        if facts.replay_code != "replay" or not facts.replay_matches:
            problems.append(
                f"duplicate not shown deduplicated (code={facts.replay_code}, "
                f"matches={facts.replay_matches})"
            )
        if problems:
            return "NOT PASS: resumed and executed, but " + "; ".join(problems), False
        return (
            "PASS candidate: approved in the console, resumed on the same plan/step, executed "
            "exactly once, duplicate deduplicated by jti",
            True,
        )
    zero = not facts.executed and facts.ledger_rows == 0 and facts.contact == "no"
    not_shown = f"NOT SHOWN: state={state}, executed={facts.executed}, contact={facts.contact}"
    if scenario == "reject":
        if state == "rejected" and zero:
            return "PASS candidate: rejected in the console; zero executions", True
        return not_shown, False
    if scenario == "timeout":
        if state == "timeout" and zero:
            return "PASS candidate: wait timed out; zero executions; no new plan", True
        return not_shown, False
    # expiry: after an approval the SDK retries with the (expired) token; that
    # failure carries no approval state (state None), so the operator confirms
    # in the record that the approval came after the printed expiry time.
    if facts.executed:
        return "NOT SHOWN: executed (approved before the token expired?); re-run", False
    if state in ("pending", "timeout"):
        return "NOT SHOWN: nobody approved within the wait; re-run and approve after expiry", False
    if facts.ledger_rows == 0 and facts.contact in ("no", "yes"):
        return (
            f"PASS candidate: resume after token expiry failed closed (state={state}, "
            f"invocation={facts.invocation_status}, code={facts.error_code}); confirm the "
            "approval time in the console",
            True,
        )
    return f"NOT SHOWN: state={state}, ledger rows={facts.ledger_rows}", False


def same_plan_step(row: Mapping[str, Any] | None, *, plan_id: str, jti: str | None) -> bool:
    if row is None or jti is None:
        return False
    return (
        str(row.get("plan_id")) == plan_id
        and str(row.get("step_index")) == "0"
        and str(row.get("jti")) == jti
    )


def hold_banner(
    scenario: str,
    approval: Any,
    *,
    deadline: datetime,
    ctx_expires: datetime | None,
    token_expires: datetime | None,
) -> str:
    lines = [
        "",
        "=" * 72,
        f"[e4] HOLD reported by the SDK ({datetime.now(UTC).isoformat(timespec='seconds')})",
        f"[e4]   delegation_id = {getattr(approval, 'delegation_id', None)}",
        f"[e4]   request_id    = {getattr(approval, 'request_id', None)}",
        f"[e4]   tool          = {TOOL} (requester: Sam)",
        f"[e4]   ACTION (Maya account): {INSTRUCTIONS[scenario]}",
        f"[e4]   where: {CONSOLE_PATH}; match the delegation id",
        f"[e4]   the SDK stops waiting at {deadline.isoformat(timespec='seconds')}",
    ]
    if ctx_expires is not None:
        lines.append(
            f"[e4]   signed `_ctx` expires at {ctx_expires.isoformat(timespec='seconds')} "
            "(a later approval reaches the tool server as context_expired)"
        )
    if token_expires is not None:
        expires = token_expires.isoformat(timespec="seconds")
        lines.append(f"[e4]   intent token expires at {expires}")
    lines.append("=" * 72)
    return "\n".join(lines)


# --- live run (seams for offline tests) ----------------------------------------------


async def run_probe(
    settings: Any,
    *,
    scenario: str,
    wait_s: float,
    token_validity_s: int | None,
    direct: e2.DirectConfig,
    ledger_dsn: str | None,
    sdk_client_factory: Callable[[str], Any] | None = None,
    direct_client: Any = None,
    lookup_factory: Callable[[str], Any] | None = None,
    ctx_signer_inner: Any = None,
    announce: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    from services.authorization import armoriq_gateway

    configured = common.with_approval_wait(settings, wait_s)
    if token_validity_s is not None:
        configured = common.with_token_validity(configured, token_validity_s)
    capture = e2.CtxCapture(ctx_signer_inner)
    recorder = e2.SdkRecorder()
    observer = e2.Observer(recorder, capture)
    holder: dict[str, Any] = {}

    def recording_factory(member_id: str) -> Any:
        inner = sdk_client_factory or holder["gateway"]._build_client
        return e2.RecordingClient(inner(member_id), recorder)

    gateway = armoriq_gateway.ArmorIQGateway(
        configured, client_factory=recording_factory, ctx_signer=capture
    )
    holder["gateway"] = gateway
    emit = announce or (lambda text: print(text, file=sys.stderr, flush=True))
    client = direct_client if direct_client is not None else e2.build_direct_client(direct.base_url)
    replay: e2.McpReply | None = None
    try:
        await asyncio.to_thread(e2.preflight, client, ledger_dsn, lookup_factory)
        started = e2.utc_now()
        ctx, plan = common.build_member_request(
            configured, PERSONA, TOOL, prefix=PREFIX, summary=f"E4 {scenario}: Sam confidential"
        )
        minted_at = datetime.now(UTC)
        handle = await gateway.start_plan(ctx, plan)
        outcome = None
        holds = common.HoldRecorder()
        hold_times: list[datetime] = []
        if handle.decision is None:
            deadline = datetime.now(UTC) + timedelta(seconds=wait_s)

            def on_hold(approval: Any) -> None:
                hold_times.append(datetime.now(UTC))
                signed = capture.captured[-1] if capture.captured else None
                ctx_expires = datetime.fromtimestamp(signed.expires_at, tz=UTC) if signed else None
                emit(
                    hold_banner(
                        scenario,
                        approval,
                        deadline=deadline,
                        ctx_expires=ctx_expires,
                        token_expires=handle.token_expires_at,
                    )
                )

            holds = common.HoldRecorder(on_hold)
            with observer.window() as window:
                outcome = await gateway.invoke_step(handle, 0, ctx, plan, on_hold=holds)
            returned_at = datetime.now(UTC)
            obs = common.observe_step(
                outcome.decision,
                approval=outcome.approval,
                invocation=outcome.invocation,
                holds=holds.events,
                window=window,
            )
            signed = [item for item in window.signed if item.jti == obs.jti]
            if scenario == "approve" and obs.executed and signed and direct.bearer:
                replay = await asyncio.to_thread(
                    common.post_tool_call,
                    client,
                    common.ENDPOINT_PATHS["memory-mcp"],
                    direct.bearer,
                    TOOL,
                    {**dict(plan.steps[0].arguments), "_ctx": signed[-1].token},
                    f"{ctx.request_id}-dup",
                )
        else:
            window = None
            returned_at = datetime.now(UTC)
            obs = common.observe_step(handle.decision, at_mint=True)
    finally:
        if direct_client is None:
            client.close()
    ended = e2.utc_now()
    evidence = await asyncio.to_thread(
        common.collect_evidence,
        ledger_dsn,
        request_ids=[ctx.request_id],
        jtis=[obs.jti] if obs.jti else [],
        started_at=started,
        ended_at=ended,
        tools=[TOOL],
        lookup_factory=lookup_factory,
    )
    mine = common.rows_for(evidence, ctx.request_id, obs.jti)
    run = e1.RunIdentifiers(
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        plan_id=handle.plan_id,
        requester_member_id=ctx.requester.member_id,
        jti=obs.jti,
        ledger_entry_id=obs.ledger_entry_id,
        tool_name=TOOL,
    )
    ledger_ok, ledger_problems, _ = common.ledger_check(mine, run)
    executed_rows = [row for row in mine["ledger"] if row.get("execution_status") == "executed"]
    facts = E4Facts(
        hold_seen=obs.observed == "HOLD",
        delegation_id=obs.delegation_id,
        approval_state=obs.approval_state,
        executed=obs.executed,
        invocation_status=obs.invocation_status,
        error_code=obs.error_code,
        contact=common.tool_server_contact(evidence, mine),
        ledger_rows=len(executed_rows) if evidence.get("status") == "ok" else -1,
        ledger_ok=ledger_ok,
        same_plan_step=same_plan_step(
            executed_rows[0] if len(executed_rows) == 1 else None,
            plan_id=handle.plan_id,
            jti=obs.jti,
        ),
        result_after_hold=bool(hold_times) and returned_at >= hold_times[0],
        replay_code=replay.code if replay is not None else None,
        replay_matches=(
            replay.original_entry_id == obs.ledger_entry_id if replay is not None else None
        ),
    )
    label, ok = e4_verdict(scenario, facts)
    early_ok = facts.result_after_hold or not obs.executed
    code = common.EXIT_PASS if ok else common.EXIT_NOT_PASS
    granted = (
        f"{(handle.token_expires_at - minted_at).total_seconds():.0f} s"
        if handle.token_expires_at
        else "not exposed"
    )
    lines = [
        f"### E4 — HOLD → console → resume: scenario `{scenario}` (probe record)",
        f"- Run at (UTC): {started} → {ended}",
        "- Operator / person acting as Maya in the console: (fill in)",
        f"- SDK version: armoriq-sdk {armoriq_gateway.SDK_VERSION}",
        f"- Requester: Sam ({ctx.requester.masked_email}); approver: Maya "
        f"({common.masked_identity(settings, 'maya')}), console only",
        f"- request_id={ctx.request_id} plan_id={handle.plan_id} "
        f"armoriq_plan_id={handle.armoriq_plan_id} jti={obs.jti}",
        f"- Token lifetime: requested {configured.token_validity_s} s, granted {granted} "
        f"(expires_at={handle.token_expires_at}); SDK approval wait {wait_s:.0f} s; "
        f"`_ctx` lifetime {armoriq_gateway.CTX_TTL_S} s",
        f"- Hold signal: observed={facts.hold_seen}; delegation_id={obs.delegation_id}; "
        f"reported at {hold_times[0].isoformat() if hold_times else 'n.a.'}; "
        f"gateway returned at {returned_at.isoformat()}",
        "- Protected result before the hold was reported: "
        + ("no (the gateway returned only after the SDK wait ended)" if early_ok else "CHECK"),
        f"- Final gateway decision: {obs.gateway_outcome} ({obs.gateway_source}); "
        f"reason: {obs.reason}; approval state: {obs.approval_state}",
        f"- Invocation: status={obs.invocation_status} code={obs.error_code} "
        f"ledger_entry_id={obs.ledger_entry_id}",
        f"- Tool server reached (DB rows by request_id/jti): {facts.contact}; DB "
        f"{evidence.get('status')}",
        f"- Ledger rows (executed): {facts.ledger_rows}; correlated: {ledger_ok}"
        + (f" ({'; '.join(ledger_problems)})" if ledger_problems and obs.executed else ""),
        f"- Same plan/step on resume (ledger plan_id/step/jti): {facts.same_plan_step}",
        "- Duplicate of the approved invocation (same `_ctx` replayed directly): "
        + (e2.describe_reply(replay) if replay is not None else "not run (nothing executed)"),
        "- Approver identity exposed to the app: no (the gateway's ApprovalStatus has no "
        "approver field; record what the console shows, U-11)",
        f"- Verdict: {label}",
        f"- Probe exit code: {code} (0 = PASS candidate for this scenario; 3 otherwise)",
    ]
    if window is not None:
        at = next(i for i, line in enumerate(lines) if line.startswith("- Invocation:")) + 1
        lines[at:at] = [
            *(f"  - {e2._sdk_line(call)}" for call in window.calls),
            f"  - {e2._wire_line(window)}",
        ]
    lines.append(
        "- Console facts to record by hand: page path and labels (D-22); who approved (U-11); "
        "whether Sam could approve his own request (self-approval)"
    )
    return code, "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="probe_v2_e4_hold", description="E4 probe")
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--wait-s", type=float, default=None)
    parser.add_argument("--token-validity-s", type=int, default=None)
    args = parser.parse_args(argv)
    if args.wait_s is None:
        args.wait_s = DEFAULT_WAIT_S[args.scenario]
    low, high = WAIT_RANGE
    if not low <= args.wait_s <= high:
        parser.error(f"--wait-s must be between {low:.0f} and {high:.0f}")
    if args.token_validity_s is None and args.scenario == "expiry":
        args.token_validity_s = EXPIRY_TOKEN_VALIDITY_S
    if args.token_validity_s is not None and not 10 <= args.token_validity_s <= 86_400:
        parser.error("--token-validity-s must be between 10 and 86400")
    return args


def _fail(message: str) -> int:
    print(f"probe_v2_e4_hold: {message}", file=sys.stderr)
    return common.EXIT_PREREQUISITES


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return common.EXIT_PREREQUISITES if exc.code else common.EXIT_PASS
    try:
        settings = e1.load_settings()
        direct = e2.load_direct_config(settings)
        ledger_dsn = e1.load_ledger_dsn()
    except Exception as exc:
        return _fail(f"settings could not be loaded ({type(exc).__name__})")
    missing = common.missing_names(settings, REQUIRED_SETTINGS)
    if direct.bearer is None:
        missing.append("TOOLSERVER_BEARER_MEMORY")
    if not ledger_dsn:
        missing.append("DATABASE_URL_TOOLSERVER")
    if missing:
        return _fail("missing required settings (environment or .env): " + ", ".join(missing))
    if direct.base_url_problem is not None:
        return _fail(direct.base_url_problem)
    try:
        with e1.gateway_logs_to_stderr():
            code, record = asyncio.run(
                run_probe(
                    settings,
                    scenario=args.scenario,
                    wait_s=args.wait_s,
                    token_validity_s=args.token_validity_s,
                    direct=direct,
                    ledger_dsn=ledger_dsn,
                )
            )
    except e2.PrerequisiteError as exc:
        return _fail(str(exc))
    except Exception as exc:
        print(f"probe_v2_e4_hold: probe error ({type(exc).__name__})", file=sys.stderr)
        return common.EXIT_ERROR
    print(record)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
