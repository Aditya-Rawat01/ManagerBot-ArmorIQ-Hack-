"""E5 probe: a misnamed tool fails closed, and which layer stops it (P1-08 §9.6, #9).

ProxyLead's own code can only plan catalog `ToolName`s, so a name mismatch
is introduced at the SDK-client seam: the gateway plans and invokes Maya's
`knowledge_search_team` as usual, and a wrapper around the gateway's SDK
client (its `client_factory` seam; never the raw SDK from here, ADR-018)
renames the action that ArmorIQ sees, at mint (`memory-mcp__<name>`) and at
invoke (`<name>`). Cases (one fresh one-step plan each):

    suffix     knowledge_search_team_e5          registered name + suffix
    qualified  memory-mcp__knowledge_search_team endpoint-qualified (U-6 form)
    case       Knowledge_Search_Team             wrong case

Expected for every case: BLOCK or an error, NEVER ALLOW and never executed.
The record names the enforcing layer with its evidence (gateway decision,
SDK exception class, proxy requests on the wire, read-only DB rows).

Evidence kept apart (P1-08 §10):
  * ArmorIQ evidence: the layer is `armoriq_policy_decision`,
    `armoriq_proxy_decision` or `armoriq_issuance`, with an ArmorIQ evidence
    reference, and the tool server shows no contact. Only this may be cited
    as the policy applying (with check_setup.py's per-member ALLOWs and E3).
  * Fail-closed evidence: `armoriq_sdk_plan_check`, `tool_server_rule`,
    `application_rule` or `unknown` still prove the mismatch fails closed,
    but are never cited as the ArmorIQ policy applying.

Usage (operator only, repository root; no arguments):
    python scripts/probe_v2_e5_setup.py

Configuration as E1 (API `Settings`, `.env` files, `services/toolserver/.env`).
Required: ARMORIQ_API_KEY, PERSONA_MAYA_EMAIL, CTX_SECRET,
DATABASE_URL_TOOLSERVER. Optional: ARMORIQ_AGENT_ID, TOOLSERVER_BASE_URL.

Exit codes:
    0  every case failed closed, zero executions confirmed from the database,
       and each case has a named layer (not `unknown`)
    1  an unexpected error stopped the probe (exception type only)
    2  prerequisites missing or unusable (names only), before any ArmorIQ call
    3  the probe ran, but a case was ALLOWED/executed, zero executions are not
       confirmed, or a layer is `unknown`; read the record
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import probe_v2_common as common
from scripts import probe_v2_e1_allowed as e1
from scripts import probe_v2_e2_offplan as e2

PREFIX = "e5"
BASE_TOOL = "knowledge_search_team"
CASES = (
    ("suffix", "knowledge_search_team_e5"),
    ("qualified", "memory-mcp__knowledge_search_team"),
    ("case", "Knowledge_Search_Team"),
)
REQUIRED_SETTINGS = (
    ("armoriq_api_key", "ARMORIQ_API_KEY"),
    ("persona_maya_email", "PERSONA_MAYA_EMAIL"),
    ("ctx_secret", "CTX_SECRET"),
)
HOLD_WAIT_S = 20.0  # a misnamed call must never wait long on a hold


class RenamingClient:
    """Wraps the gateway's SDK client: the action ArmorIQ sees is renamed.

    ``rename`` (shared with the probe, updated before each case) maps the
    catalog action to the misnamed one. Plan names are rewritten in
    `start_plan` (`<mcp>__<action>`), the action in `invoke_with_policy` /
    `invoke`. Everything else passes through untouched.
    """

    def __init__(self, inner: Any, rename: dict[str, str]) -> None:
        self._inner = inner
        self.rename = rename

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _action(self, action: str) -> str:
        return self.rename.get(action, action)

    def for_user(self, email: str) -> Any:
        return _RenamingScope(self._inner.for_user(email), self)

    def invoke_with_policy(self, mcp: str, action: str, *args: Any, **kwargs: Any) -> Any:
        return self._inner.invoke_with_policy(mcp, self._action(action), *args, **kwargs)

    def invoke(self, mcp: str, action: str, *args: Any, **kwargs: Any) -> Any:
        return self._inner.invoke(mcp, self._action(action), *args, **kwargs)


class _RenamingScope:
    def __init__(self, inner: Any, owner: RenamingClient) -> None:
        self._inner = inner
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def start_session(self, *args: Any, **kwargs: Any) -> Any:
        return _RenamingSession(self._inner.start_session(*args, **kwargs), self._owner)


class _RenamingSession:
    def __init__(self, inner: Any, owner: RenamingClient) -> None:
        self._inner = inner
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def start_plan(self, tool_calls: list[dict[str, Any]], *args: Any, **kwargs: Any) -> Any:
        renamed = []
        for call in tool_calls:
            mcp, _, action = str(call["name"]).partition("__")
            renamed.append({**call, "name": f"{mcp}__{self._owner._action(action)}"})
        return self._inner.start_plan(renamed, *args, **kwargs)


# --- pure verdict (offline-tested) ---------------------------------------------------


def case_verdict(obs: common.StepObservation, contact: str) -> dict[str, Any]:
    """Fail-closed verdict + evidence class for one misnamed case."""
    layer = obs.source
    if obs.observed == "ALLOW" or obs.executed:
        return {
            "result": "FAIL: the misnamed tool was ALLOWED",
            "fail_closed": False,
            "evidence_class": "none",
            "layer": layer,
        }
    if contact.startswith("unknown"):
        fail_closed: bool | None = None
        result = f"{obs.observed}, but zero executions not confirmed ({contact})"
    elif contact == "yes":
        fail_closed = None
        result = f"{obs.observed}; the tool server recorded rows for this call: investigate"
    else:
        fail_closed = True
        result = f"fail-closed: {obs.observed}"
    if obs.armoriq_evidenced and contact == "no":
        evidence_class = "ArmorIQ evidence (may be cited as the policy/registry applying)"
    elif layer == "tool_server_rule":
        evidence_class = (
            "fail-closed only: the proxy forwarded the misnamed call and the tool server "
            "refused it (an unknown name writes no attempt row; confirm in the Render log)"
        )
    else:
        evidence_class = f"fail-closed only ({layer}); never cited as the ArmorIQ policy"
    return {
        "result": result,
        "fail_closed": fail_closed,
        "evidence_class": evidence_class,
        "layer": layer,
    }


def exit_code(verdicts: list[Mapping[str, Any]]) -> int:
    if not verdicts:
        return common.EXIT_NOT_PASS
    for verdict in verdicts:
        if verdict["fail_closed"] is not True or str(verdict["layer"]).startswith("unknown"):
            return common.EXIT_NOT_PASS
    return common.EXIT_PASS


# --- live run (seams for offline tests) ----------------------------------------------


async def run_probe(
    settings: Any,
    *,
    direct: e2.DirectConfig,
    ledger_dsn: str | None,
    sdk_client_factory: Callable[[str], Any] | None = None,
    direct_client: Any = None,
    lookup_factory: Callable[[str], Any] | None = None,
) -> tuple[int, str]:
    from services.authorization import armoriq_gateway

    recorder = e2.SdkRecorder()
    observer = e2.Observer(recorder)
    holder: dict[str, Any] = {}
    rename: dict[str, str] = {}

    def factory(member_id: str) -> Any:
        inner = sdk_client_factory or holder["gateway"]._build_client
        return e2.RecordingClient(RenamingClient(inner(member_id), rename), recorder)

    configured = common.with_approval_wait(settings, HOLD_WAIT_S)
    gateway = armoriq_gateway.ArmorIQGateway(configured, client_factory=factory)
    holder["gateway"] = gateway
    client = direct_client if direct_client is not None else e2.build_direct_client(direct.base_url)
    try:
        await asyncio.to_thread(e2.preflight, client, ledger_dsn, lookup_factory)
    finally:
        if direct_client is None:
            client.close()
    started = e2.utc_now()
    runs: list[dict[str, Any]] = []
    for case, misnamed in CASES:
        ctx, plan = common.build_member_request(
            configured, "maya", BASE_TOOL, prefix=PREFIX, summary=f"E5 misnamed tool ({case})"
        )
        rename.clear()
        rename[BASE_TOOL] = misnamed
        handle = await gateway.start_plan(ctx, plan)
        window = None
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
        runs.append(
            {
                "case": case,
                "misnamed": misnamed,
                "ctx": ctx,
                "handle": handle,
                "obs": obs,
                "window": window,
            }
        )
    ended = e2.utc_now()
    request_ids = [run["ctx"].request_id for run in runs]
    jtis = [run["obs"].jti for run in runs if run["obs"].jti]
    evidence = await asyncio.to_thread(
        common.collect_evidence,
        ledger_dsn,
        request_ids=request_ids,
        jtis=jtis,
        started_at=started,
        ended_at=ended,
        tools=[BASE_TOOL, *(name for _, name in CASES)],
        lookup_factory=lookup_factory,
    )
    verdicts = []
    for run in runs:
        mine = common.rows_for(evidence, run["ctx"].request_id, run["obs"].jti)
        run["contact"] = common.tool_server_contact(evidence, mine)
        run["rows"] = mine
        run["verdict"] = case_verdict(run["obs"], run["contact"])
        verdicts.append(run["verdict"])
    unattributed = common.unattributed_rows(evidence, request_ids, jtis)
    code = exit_code(verdicts)
    if unattributed:
        code = common.EXIT_NOT_PASS
    return code, render(runs, evidence, unattributed, settings=settings, direct=direct,
                        started=started, ended=ended, code=code)  # fmt: skip


def render(
    runs: list[dict[str, Any]],
    evidence: Mapping[str, Any],
    unattributed: list[str],
    *,
    settings: Any,
    direct: e2.DirectConfig,
    started: str,
    ended: str,
    code: int,
) -> str:
    from services.authorization import armoriq_gateway

    cell = common.cell
    lines = [
        "### E5 — misnamed tool fails closed (probe record)",
        f"- Run at (UTC): {started} → {ended}",
        "- Operator: (fill in)",
        f"- SDK version: armoriq-sdk {armoriq_gateway.SDK_VERSION}",
        f"- Agent: {settings.armoriq_agent_id}; requester Maya "
        f"({common.masked_identity(settings, 'maya')}); base tool {BASE_TOOL} on memory-mcp",
        f"- Tool-server base URL (no secrets): {direct.base_url} ({direct.base_url_source})",
        "- Method: the gateway plans/invokes the catalog name; a wrapper at the gateway's "
        "SDK-client seam renames the action ArmorIQ sees (mint and invoke)",
        f"- DB evidence (read-only, toolserver role): {evidence.get('status')}",
        "- Unattributed rows in the run window: " + ("; ".join(unattributed) or "none"),
        f"- Probe exit code: {code} (0 = every case fail-closed, zero executions confirmed, "
        "named layer; 3 otherwise)",
        "- ArmorIQ policy evidence (separate): cite check_setup.py's per-member ALLOW lines "
        "and the E3 BLOCK/HOLD cells; a misname stopped below ArmorIQ proves fail-closed only",
        "",
        "| case | name ArmorIQ saw | observed | enforcing layer | ArmorIQ evidence ref "
        "| tool server reached | result | evidence class |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        obs, verdict = run["obs"], run["verdict"]
        where = " (at mint)" if obs.at_mint else ""
        lines.append(
            f"| `{run['case']}` | `{run['misnamed']}` | {obs.observed}{where} | {verdict['layer']} "
            f"| {cell(obs.evidence_ref or 'none')} | {run['contact']} | "
            f"**{cell(verdict['result'])}** | {cell(verdict['evidence_class'])} |"
        )
    lines += ["", "#### Case details (UTC; identifiers only)"]
    for run in runs:
        obs, ctx, handle = run["obs"], run["ctx"], run["handle"]
        lines.append(
            f"- `{run['case']}` request_id={ctx.request_id} plan_id={handle.plan_id} "
            f"armoriq_plan_id={handle.armoriq_plan_id} jti={obs.jti}"
        )
        lines.append(
            f"  - gateway: {obs.gateway_outcome} ({obs.gateway_source}); reason: {obs.reason}; "
            f"invocation={obs.invocation_status} code={obs.error_code}"
        )
        window = run["window"]
        if window is not None:
            lines.extend(f"  - {e2._sdk_line(call)}" for call in window.calls)
            lines.append(f"  - {e2._wire_line(window)}")
        lines.append(
            f"  - DB rows: ledger={len(run['rows']['ledger'])} "
            f"attempts={len(run['rows']['attempts'])}"
        )
    return "\n".join(lines)


def _fail(message: str) -> int:
    print(f"probe_v2_e5_setup: {message}", file=sys.stderr)
    return common.EXIT_PREREQUISITES


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        return _fail("no arguments accepted")
    try:
        settings = e1.load_settings()
        direct = e2.load_direct_config(settings)
        ledger_dsn = e1.load_ledger_dsn()
    except Exception as exc:
        return _fail(f"settings could not be loaded ({type(exc).__name__})")
    missing = common.missing_names(settings, REQUIRED_SETTINGS)
    if not ledger_dsn:
        missing.append("DATABASE_URL_TOOLSERVER")
    if missing:
        return _fail("missing required settings (environment or .env): " + ", ".join(missing))
    if direct.base_url_problem is not None:
        return _fail(direct.base_url_problem)
    try:
        with e1.gateway_logs_to_stderr():
            code, record = asyncio.run(run_probe(settings, direct=direct, ledger_dsn=ledger_dsn))
    except e2.PrerequisiteError as exc:
        return _fail(str(exc))
    except Exception as exc:
        print(f"probe_v2_e5_setup: probe error ({type(exc).__name__})", file=sys.stderr)
        return common.EXIT_ERROR
    print(record)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
