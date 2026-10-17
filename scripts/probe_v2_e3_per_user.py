"""E3 probe: per-member ALLOW / HOLD / BLOCK on the same tools (P1-09, #10).

Three members (Maya, Sam, Priya; one SDK client each, inside the real
`ArmorIQGateway`, ADR-018) x four tools, each in its own one-step plan:
`knowledge_search_team`, `knowledge_search_confidential`,
`knowledge_search_restricted`, `send_external_message` (honeypot). The
`knowledge_get_source_*` tools are optional in P1-09 and not driven here.

Expected (PRD §5.1 + honeypot):

    tool                    Maya    Sam    Priya
    team                    ALLOW   ALLOW  ALLOW
    confidential            ALLOW   HOLD   BLOCK
    restricted              ALLOW   BLOCK  BLOCK
    send_external_message   BLOCK   BLOCK  BLOCK

Per cell the record shows: observed decision, `DecisionSource`, ArmorIQ
evidence reference (SDK exception, delegation id, armoriq_plan_id), whether
the tool server was reached (read-only rows by request_id / jti, plus a
time-window scan like E2), and the ledger row (ALLOW only). A BLOCK/HOLD
cell MATCHES only under the ArmorIQ evidence rule (P1-09 §10): an
`armoriq_*` source, an ArmorIQ evidence reference, and no tool-server
contact. A cell stopped by `armoriq_sdk_plan_check`, `tool_server_rule`,
`application_rule` or `unknown` is FAIL, with that layer named.

Sam's HOLD never waits long: the gateway runs with an approval wait of
`--hold-wait-s` seconds (default 30). The SDK then stops polling and the
delegation stays pending in the console: leave it, or reject it (approval
is E4). The delegation id is printed on stderr as soon as the SDK reports it.

Options:
    --hold-wait-s S   seconds the SDK waits on Sam's HOLD (5..240, default 30)
    --concurrent N    also run 3 x N concurrent team searches (P1-09 step 4)
                      and check each ledger row's requester_member_id
                      (default 0 = off; the spec suggests 5)

Step 5 (tool-server tier ceiling) is a separate table, never counted in
the 12 cells: `knowledge_search_restricted` for Sam and Priya and
`knowledge_search_confidential` for Priya, sent DIRECTLY to the tool server
with a valid `_ctx` signed exactly as the gateway signs it (no ArmorIQ).
Expected: `out_of_scope` (tool_server_rule) with a rejection ledger row.

Usage (operator only, repository root):
    python scripts/probe_v2_e3_per_user.py [--hold-wait-s 30] [--concurrent 5]

Configuration is read like E1/E2 (API `Settings`: environment, then the
repo-root `.env` and `apps/api/.env`; the bearer and the DSN also from
`services/toolserver/.env`). Required: ARMORIQ_API_KEY, PERSONA_MAYA_EMAIL,
PERSONA_SAM_EMAIL, PERSONA_PRIYA_EMAIL, CTX_SECRET (must equal the deployed
tool server's), TOOLSERVER_BEARER_MEMORY, DATABASE_URL_TOOLSERVER. Optional:
ARMORIQ_AGENT_ID (default `proxylead`), TOOLSERVER_BASE_URL. Values are
never printed; members appear masked.

Exit codes (as E1/E2):
    0  all 12 cells MATCH under the evidence rule, no unattributed rows in the
       run window, and (when run) no concurrency attribution error: a
       candidate for the operator's PASS
    1  an unexpected error stopped the probe (exception type only)
    2  prerequisites missing or unusable (names only), checked before any
       ArmorIQ call: arguments, settings, base URL, `/health`, the database
    3  the probe ran, but a cell does not match, evidence is missing, or an
       attribution error was found; read the record
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import probe_v2_common as common
from scripts import probe_v2_e1_allowed as e1
from scripts import probe_v2_e2_offplan as e2

PREFIX = "e3"
MATRIX_TOOLS = (
    "knowledge_search_team",
    "knowledge_search_confidential",
    "knowledge_search_restricted",
    "send_external_message",
)
EXPECTED: dict[tuple[str, str], str] = {
    ("maya", "knowledge_search_team"): "ALLOW",
    ("maya", "knowledge_search_confidential"): "ALLOW",
    ("maya", "knowledge_search_restricted"): "ALLOW",
    ("maya", "send_external_message"): "BLOCK",
    ("sam", "knowledge_search_team"): "ALLOW",
    ("sam", "knowledge_search_confidential"): "HOLD",
    ("sam", "knowledge_search_restricted"): "BLOCK",
    ("sam", "send_external_message"): "BLOCK",
    ("priya", "knowledge_search_team"): "ALLOW",
    ("priya", "knowledge_search_confidential"): "BLOCK",
    ("priya", "knowledge_search_restricted"): "BLOCK",
    ("priya", "send_external_message"): "BLOCK",
}
# Step 5: (persona, member_id, app_role as the gateway signs it, tool).
CEILING_CASES = (
    ("sam", "m-sam", "senior", "knowledge_search_restricted"),
    ("priya", "m-priya", "junior", "knowledge_search_restricted"),
    ("priya", "m-priya", "junior", "knowledge_search_confidential"),
)
REQUIRED_SETTINGS = (
    ("armoriq_api_key", "ARMORIQ_API_KEY"),
    ("persona_maya_email", "PERSONA_MAYA_EMAIL"),
    ("persona_sam_email", "PERSONA_SAM_EMAIL"),
    ("persona_priya_email", "PERSONA_PRIYA_EMAIL"),
    ("ctx_secret", "CTX_SECRET"),
)
HOLD_WAIT_RANGE = (5.0, 240.0)
CONSOLE_PATH = "ArmorIQ console → Governance → Plans Governance → filter “Needs you”"


@dataclass
class Cell:
    """One matrix cell: what was sent and what came back (no secrets)."""

    persona: str
    tool: str
    expected: str
    masked: str
    member_id: str
    request_id: str
    trace_id: str
    plan_id: str
    armoriq_plan_id: str | None
    obs: common.StepObservation
    window: Any = None
    contact: str = "unknown"
    ledger: str = "-"
    verdict: str = "FAIL"
    note: str = ""
    rows: dict[str, list] = field(default_factory=dict)


# --- pure classification (offline-tested) --------------------------------------------


def cell_verdict(
    expected: str,
    obs: common.StepObservation,
    contact: str,
    *,
    ledger_ok: bool,
    ledger_problems: Iterable[str] = (),
) -> tuple[str, str]:
    """MATCH / FAIL for one cell under the ArmorIQ evidence rule (+ why)."""
    problems: list[str] = []
    if expected == "ALLOW":
        if obs.observed != "ALLOW":
            problems.append(f"observed {obs.observed} from {obs.source}")
        elif obs.source != "armoriq_policy_decision":
            problems.append(f"ALLOW from {obs.source}")
        if not obs.executed:
            problems.append(f"not executed (status={obs.invocation_status}, code={obs.error_code})")
        if not ledger_ok:
            problems.append("ledger: " + ("; ".join(ledger_problems) or "no correlated row"))
    else:
        if obs.observed != expected:
            problems.append(f"observed {obs.observed} from {obs.source}")
        if obs.executed:
            problems.append("EXECUTED on a non-ALLOW cell")
        if obs.source not in common.ARMORIQ_SOURCES:
            problems.append(f"stopped by {obs.source}: not an ArmorIQ decision")
        elif not obs.evidence_ref:
            problems.append("no ArmorIQ evidence reference")
        if contact != "no":
            problems.append(f"tool-server contact: {contact}")
    problems.extend(obs.flags)
    if problems:
        return "FAIL", "; ".join(problems)
    return "MATCH", "evidence rule met" if expected != "ALLOW" else "executed, ledger correlated"


def attribution_errors(
    runs: Iterable[tuple[str, str | None, Mapping[str, Any] | None]],
) -> list[str]:
    """Concurrency check: each executed run's ledger row names its own member.

    ``runs``: (expected member_id, jti, ledger row or None). A run without a
    jti or a row is an error too (attribution is never assumed).
    """
    errors: list[str] = []
    for member_id, jti, row in runs:
        if not jti:
            errors.append(f"{member_id}: no jti (call did not reach the tool server)")
        elif row is None:
            errors.append(f"{member_id}: no ledger row for jti {jti}")
        elif row.get("requester_member_id") != member_id:
            errors.append(
                f"jti {jti}: ledger requester_member_id={row.get('requester_member_id')!r}, "
                f"invoked by {member_id}"
            )
    return errors


def ceiling_verdict(reply: e2.McpReply, db_rows: list[dict]) -> tuple[str, str]:
    """Step 5: `enforced` only for the tool server's own `out_of_scope` code,
    corroborated by its rejection ledger row when the database is readable."""
    layer = e2.direct_layer(reply)
    if layer == "tool_server_rule" and reply.code == "out_of_scope":
        rejected = [row for row in db_rows if row.get("validation_outcome") == "out_of_scope"]
        if rejected:
            return "enforced", f"out_of_scope + ledger rejection row #{rejected[0].get('id')}"
        return "enforced", "out_of_scope (no DB corroboration)"
    if reply.is_error is False:
        return "NOT enforced", "the tool server EXECUTED an out-of-tier call"
    return "unknown", f"layer {layer}, code {reply.code}"


def exit_code(cells: list[Cell], attribution: list[str] | None, unattributed: list[str]) -> int:
    if any(cell.verdict != "MATCH" for cell in cells) or len(cells) != len(EXPECTED):
        return common.EXIT_NOT_PASS
    if attribution or unattributed:
        return common.EXIT_NOT_PASS
    return common.EXIT_PASS


# --- live run (seams for offline tests) ----------------------------------------------


def _announce(persona: str, tool: str, wait_s: float) -> Callable[[Any], None]:
    def announce(approval: Any) -> None:
        print(
            f"[e3] HOLD reported for {persona} {tool}: delegation_id="
            f"{getattr(approval, 'delegation_id', None)} request_id="
            f"{getattr(approval, 'request_id', None)}. E3 does not approve: leave it pending "
            f"or REJECT it ({CONSOLE_PATH}). The SDK stops waiting after {wait_s:.0f} s.",
            file=sys.stderr,
            flush=True,
        )

    return announce


async def run_cell(
    gateway: Any,
    settings: Any,
    observer: e2.Observer,
    persona: str,
    tool: str,
    *,
    hold_wait_s: float,
    prefix: str = PREFIX,
) -> Cell:
    ctx, plan = common.build_member_request(
        settings, persona, tool, prefix=prefix, summary=f"E3: one {tool} call"
    )
    handle = await gateway.start_plan(ctx, plan)
    window = None
    if handle.decision is not None:
        obs = common.observe_step(handle.decision, at_mint=True)
    else:
        holds = common.HoldRecorder(_announce(persona, tool, hold_wait_s))
        with observer.window() as window:
            outcome = await gateway.invoke_step(handle, 0, ctx, plan, on_hold=holds)
        obs = common.observe_step(
            outcome.decision,
            approval=outcome.approval,
            invocation=outcome.invocation,
            holds=holds.events,
            window=window,
        )
    return Cell(
        persona=persona,
        tool=tool,
        expected=EXPECTED.get((persona, tool), "ALLOW"),
        masked=ctx.requester.masked_email,
        member_id=ctx.requester.member_id,
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        plan_id=handle.plan_id,
        armoriq_plan_id=handle.armoriq_plan_id,
        obs=obs,
        window=window,
    )


def classify(cells: list[Cell], evidence: Mapping[str, Any]) -> None:
    for cell in cells:
        mine = common.rows_for(evidence, cell.request_id, cell.obs.jti)
        cell.rows = mine
        cell.contact = common.tool_server_contact(evidence, mine)
        ledger_ok, problems = False, ["DB " + str(evidence.get("status"))]
        if evidence.get("status") == "ok":
            run = e1.RunIdentifiers(
                request_id=cell.request_id,
                trace_id=cell.trace_id,
                plan_id=cell.plan_id,
                requester_member_id=cell.member_id,
                jti=cell.obs.jti,
                ledger_entry_id=cell.obs.ledger_entry_id,
                tool_name=cell.tool,
            )
            ledger_ok, problems, _ = common.ledger_check(mine, run)
        if mine["ledger"]:
            cell.ledger = ", ".join(
                f"#{row.get('id')} {row.get('execution_status')}" for row in mine["ledger"]
            )
        else:
            cell.ledger = "none" if evidence.get("status") == "ok" else "unknown"
        cell.verdict, cell.note = cell_verdict(
            cell.expected, cell.obs, cell.contact, ledger_ok=ledger_ok, ledger_problems=problems
        )


def direct_ceiling(
    client: Any, bearer: str, ctx_secret: str, request_id: str
) -> list[dict[str, Any]]:
    rows = []
    for index, (persona, member_id, role, tool) in enumerate(CEILING_CASES):
        case_request = f"{request_id}-ceiling{index}"
        token, jti = common.member_ctx(
            ctx_secret,
            member_id=member_id,
            persona=persona,
            app_role=role,
            tool=tool,
            aud="memory-mcp",
            request_id=case_request,
        )
        arguments = {**common.default_arguments(tool, PREFIX), "_ctx": token}
        with e2.Observer().window() as window:
            reply = common.post_tool_call(
                client, common.ENDPOINT_PATHS["memory-mcp"], bearer, tool, arguments, case_request
            )
        rows.append(
            {
                "persona": persona,
                "member_id": member_id,
                "tool": tool,
                "request_id": case_request,
                "jti": jti,
                "reply": reply,
                "window": window,
            }
        )
    return rows


def render(
    cells: list[Cell],
    ceiling: list[dict[str, Any]],
    concurrency: dict[str, Any] | None,
    evidence: Mapping[str, Any],
    unattributed: list[str],
    *,
    header: list[str],
    code: int,
) -> str:
    cell = common.cell
    lines = [
        "### E3 — per-member outcomes on the same tools (probe record)",
        *header,
        f"- DB evidence (read-only, toolserver role): {evidence.get('status')}"
        + (f"; notes: {evidence['notes']}" if evidence.get("notes") else ""),
        "- Unattributed rows in the run window: " + ("; ".join(unattributed) or "none"),
        f"- Matrix: {sum(c.verdict == 'MATCH' for c in cells)}/{len(EXPECTED)} cells MATCH",
        f"- Probe exit code: {code} (0 = all 12 cells match under the evidence rule, no "
        "unattributed rows, no attribution error; 3 = completed otherwise)",
        "- Outcome: operator-assigned in docs/EXPERIMENTS.md §E3 (PASS also needs the console "
        "attribution and the Render log: no tool-server line for any BLOCK/HOLD cell)",
        "",
        "#### Matrix (12 cells)",
        "",
        "| member | tool | expected | observed | DecisionSource | ArmorIQ evidence ref "
        "| tool server reached | ledger row | verdict |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in cells:
        lines.append(
            f"| {item.persona} ({item.masked}) | `{item.tool}` | {item.expected} | "
            f"{item.obs.observed} | {item.obs.source} | {cell(item.obs.evidence_ref or 'none')} | "
            f"{item.contact} | {cell(item.ledger)} | **{item.verdict}**: {cell(item.note)} |"
        )
    lines += ["", "#### Cell details (UTC; identifiers only)"]
    for item in cells:
        obs = item.obs
        span = f"[{item.window.started_at} → {item.window.ended_at}]" if item.window else "[mint]"
        lines.append(
            f"- {item.persona} `{item.tool}` {span} request_id={item.request_id} "
            f"plan_id={item.plan_id} armoriq_plan_id={item.armoriq_plan_id} jti={obs.jti}"
        )
        lines.append(
            f"  - gateway: {obs.gateway_outcome} ({obs.gateway_source}); reason: {obs.reason}; "
            f"approval={obs.approval_state}; delegation_id={obs.delegation_id}; "
            f"invocation={obs.invocation_status} code={obs.error_code} "
            f"ledger_entry_id={obs.ledger_entry_id}"
        )
        if item.window is not None:
            lines.extend(f"  - {e2._sdk_line(call)}" for call in item.window.calls)
            lines.append(f"  - {e2._wire_line(item.window)}")
        if item.rows:
            ledger = [
                f"#{row.get('id')} {row.get('execution_status')} {row.get('requester_member_id')}"
                for row in item.rows["ledger"]
            ]
            attempts = [
                f"#{row.get('id')} {row.get('outcome')} ({row.get('credential_id')})"
                for row in item.rows["attempts"]
            ]
            lines.append(
                f"  - DB rows: ledger={ledger or 'none'}; attempts={attempts or 'none'}; "
                f"egress_sink={len(item.rows['egress'])}"
            )
    if concurrency is not None:
        lines += [
            "",
            "#### Concurrency (P1-09 step 4)",
            f"- Runs: {concurrency['runs']} team searches (3 members x {concurrency['n']}), "
            f"executed: {concurrency['executed']}",
            "- Attribution errors (ledger requester_member_id vs invoking member): "
            + ("; ".join(concurrency["errors"]) or "none"),
            "- Console attribution: operator checks the ArmorIQ console (each plan under its "
            "own member).",
        ]
    lines += [
        "",
        "#### Tool-server tier ceiling (step 5; NOT part of the 12 cells)",
        "",
        "| member | tool (direct, valid `_ctx`, no ArmorIQ) | expected | tool-server reply "
        "| result |",
        "|---|---|---|---|---|",
    ]
    for item in ceiling:
        lines.append(
            f"| {item['persona']} | `{item['tool']}` | out_of_scope (tool_server_rule) | "
            f"{cell(e2.describe_reply(item['reply']))} | **{item['verdict']}**: "
            f"{cell(item['verdict_note'])} |"
        )
    return "\n".join(lines)


async def run_concurrency(gateway: Any, settings: Any, n: int) -> list[tuple[str, Cell]]:
    """3 x n concurrent team searches (the gateway serializes per member only)."""
    observer = e2.Observer()

    async def one(persona: str) -> tuple[str, Cell]:
        item = await run_cell(
            gateway,
            settings,
            observer,
            persona,
            "knowledge_search_team",
            hold_wait_s=0,
            prefix=f"{PREFIX}c",
        )
        return item.member_id, item

    tasks = [one(persona) for _ in range(n) for persona in common.PERSONA_ORDER]
    return list(await asyncio.gather(*tasks))


async def run_probe(
    settings: Any,
    *,
    direct: e2.DirectConfig,
    ledger_dsn: str | None,
    hold_wait_s: float = 30.0,
    concurrent: int = 0,
    sdk_client_factory: Callable[[str], Any] | None = None,
    direct_client: Any = None,
    lookup_factory: Callable[[str], Any] | None = None,
) -> tuple[int, str]:
    """Run the matrix (+ optional concurrency) and step 5; return (exit, record)."""
    from services.authorization import armoriq_gateway

    recorder = e2.SdkRecorder()
    observer = e2.Observer(recorder)
    holder: dict[str, Any] = {}

    def recording_factory(member_id: str) -> Any:
        inner = sdk_client_factory or holder["gateway"]._build_client
        return e2.RecordingClient(inner(member_id), recorder)

    short = common.with_approval_wait(settings, hold_wait_s)
    gateway = armoriq_gateway.ArmorIQGateway(short, client_factory=recording_factory)
    holder["gateway"] = gateway
    client = direct_client if direct_client is not None else e2.build_direct_client(direct.base_url)
    try:
        await asyncio.to_thread(e2.preflight, client, ledger_dsn, lookup_factory)
        started = e2.utc_now()
        cells = [
            await run_cell(gateway, short, observer, persona, tool, hold_wait_s=hold_wait_s)
            for persona in common.PERSONA_ORDER
            for tool in MATRIX_TOOLS
        ]
        concurrent_runs = await run_concurrency(gateway, short, concurrent) if concurrent else []
        ceiling = await asyncio.to_thread(
            direct_ceiling,
            client,
            direct.bearer or "",
            e1.secret_text(settings.ctx_secret),
            common.new_request_id(f"{PREFIX}d"),
        )
    finally:
        if direct_client is None:
            client.close()
    ended = e2.utc_now()
    every = [*cells, *(item for _, item in concurrent_runs)]
    request_ids = [item.request_id for item in every] + [row["request_id"] for row in ceiling]
    jtis = [item.obs.jti for item in every if item.obs.jti] + [row["jti"] for row in ceiling]
    evidence = await asyncio.to_thread(
        common.collect_evidence,
        ledger_dsn,
        request_ids=request_ids,
        jtis=jtis,
        started_at=started,
        ended_at=ended,
        tools=list(MATRIX_TOOLS),
        lookup_factory=lookup_factory,
    )
    classify(cells, evidence)
    for row in ceiling:
        mine = common.rows_for(evidence, row["request_id"], row["jti"])
        row["verdict"], row["verdict_note"] = ceiling_verdict(row["reply"], mine["ledger"])
    concurrency = None
    attribution: list[str] | None = None
    if concurrent:
        triples = []
        for member_id, item in concurrent_runs:
            ledger_rows = common.rows_for(evidence, item.request_id, item.obs.jti)["ledger"]
            triples.append((member_id, item.obs.jti, ledger_rows[0] if ledger_rows else None))
        attribution = attribution_errors(triples)
        concurrency = {
            "n": concurrent,
            "runs": len(concurrent_runs),
            "executed": sum(item.obs.executed for _, item in concurrent_runs),
            "errors": attribution,
        }
    unattributed = common.unattributed_rows(evidence, request_ids, jtis)
    code = exit_code(cells, attribution, unattributed)
    header = [
        f"- Run at (UTC): {started} → {ended}",
        "- Operator: (fill in)",
        f"- SDK version: armoriq-sdk {armoriq_gateway.SDK_VERSION}",
        f"- Agent: {settings.armoriq_agent_id}; endpoints memory-mcp + workspace-mcp "
        "(confirm the active policy in the console)",
        f"- Tool-server base URL (no secrets): {direct.base_url} ({direct.base_url_source})",
        "- Members (masked): "
        + ", ".join(f"{p}={common.masked_identity(settings, p)}" for p in common.PERSONA_ORDER),
        f"- HOLD wait: {hold_wait_s:.0f} s (then the delegation is left pending; approval is E4)",
    ]
    return code, render(
        cells, ceiling, concurrency, evidence, unattributed, header=header, code=code
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="probe_v2_e3_per_user", description="E3 probe")
    parser.add_argument("--hold-wait-s", type=float, default=30.0)
    parser.add_argument("--concurrent", type=int, default=0)
    args = parser.parse_args(argv)
    low, high = HOLD_WAIT_RANGE
    if not low <= args.hold_wait_s <= high:
        parser.error(f"--hold-wait-s must be between {low:.0f} and {high:.0f}")
    if not 0 <= args.concurrent <= 10:
        parser.error("--concurrent must be between 0 and 10")
    return args


def _fail(message: str) -> int:
    print(f"probe_v2_e3_per_user: {message}", file=sys.stderr)
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
                    direct=direct,
                    ledger_dsn=ledger_dsn,
                    hold_wait_s=args.hold_wait_s,
                    concurrent=args.concurrent,
                )
            )
    except e2.PrerequisiteError as exc:
        return _fail(str(exc))
    except Exception as exc:
        print(f"probe_v2_e3_per_user: probe error ({type(exc).__name__})", file=sys.stderr)
        return common.EXIT_ERROR
    print(record)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
