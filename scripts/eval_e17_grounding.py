"""E17: grounding evaluation and `MIN_SCORE` calibration (P3-08, #30).

Runs every labeled question in `seed/eval/` through the REAL tool-server
search path, as the case's persona, and calibrates `MIN_SCORE` on the train
split only, then reports the held-out split at that threshold.

Harness path (recorded in the output; spec §9 step 2): the tool server's own
app (`services.toolserver.main.create_app`) runs in-process and is called
over MCP streamable HTTP with a bearer and an HMAC-signed test `_ctx` per
call. Each call therefore passes the real dispatcher: `_ctx` verification,
argument validation, the member tier ceiling (manager -> restricted, senior
-> confidential, junior -> team), the `knowledge_search_*` handler, hybrid
retrieval against `DATABASE_URL_TOOLSERVER`, and a ledger row. ArmorIQ is NOT
involved: this is evidence of retrieval and responder behavior, never live
ArmorIQ evidence, and never proof of factual correctness. The bearer and the
`_ctx` secret are random per run and exist only in this process.

The responder decision uses the production code: `evidence_from_results`
(no `refutes` links, exactly what the orchestrator passes today) and
`respond(...)` with the chosen threshold. The model draft comes from an
offline extractive generator that cites every grounding source it is given
(no LLM call), so "actual citations" are the grounding set; the responder's
citation validation still runs on them.

Metrics (pure functions below, unit-tested in
`tests/toolserver/test_e17_metrics.py`). A search hit's score is the cosine
similarity of its chunk (P2-05); the responder grounds on hits with
`score >= MIN_SCORE`, so a case is ACCEPTED at threshold t when any allowed
hit scores >= t. Outcomes at t:

* answerable case: `correct_accept` (an expected source scores >= t),
  `misgrounded_accept` (accepted, but no expected source clears t), or
  `false_abstain` (nothing clears t);
* unanswerable case (unrelated, weak evidence, escalation without access):
  `false_accept` (something clears t) or `correct_abstain`.

Conflict cases are excluded from calibration: no threshold can produce the
`conflict` abstention (it needs a refutation link). They are reported in the
acceptance checks instead.

Calibration: candidate thresholds are a FIXED grid (0.30..0.95, step 0.01),
independent of any score. cost(t) = 2 x false accepts + false abstains +
misgrounded accepts (a false accept is weighted double: grounding fails
closed, D-23). The chosen threshold is the upper median of the grid values
with minimal TRAIN cost (for a separable train set: the middle of the gap,
rounded up). Held-out cases are rejected by `choose_threshold`, so they can
never influence the choice.

Usage (repository root; operator only, local database; DATABASE_URL_TOOLSERVER
set to the toolserver LOGIN role's DSN, never the owner):
    python scripts/eval_e17_grounding.py --split train
    python scripts/eval_e17_grounding.py --split heldout

Both splits are always scored (the threshold needs train); `--split` selects
which per-case rows are printed. The record is deterministic for a given
corpus, model, and eval set (no timestamps, no identifiers). The DSN is
never printed.

Exit codes: 0 record printed and acceptance items A1-A4 met; 2 prerequisites
missing (DSN unset, eval set invalid); 3 record printed but an acceptance
item A1-A4 is not met; 1 unexpected error (type only).
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import secrets
import sys
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "seed" / "eval"
SOURCES_DIR = REPO_ROOT / "seed" / "sources"
SPLIT_FILES = {"train": "e17_train.json", "heldout": "e17_heldout.json"}
SPLITS = tuple(SPLIT_FILES)

CASE_TYPES = frozenset(
    {"answerable", "unrelated", "weak_evidence", "conflict", "superseded", "escalation"}
)
ABSTAIN_REASONS = frozenset({"no_evidence", "conflict"})
CASE_KEYS = frozenset(
    {
        "id",
        "case_type",
        "persona",
        "tiers",
        "question",
        "expected_answerable",
        "expected_source_ids",
        "expected_abstain_reason",
    }
)
TIER_RANK = {"team": 0, "confidential": 1, "restricted": 2}
SEARCH_TOOL = {
    "team": "knowledge_search_team",
    "confidential": "knowledge_search_confidential",
    "restricted": "knowledge_search_restricted",
}


@dataclass(frozen=True)
class Persona:
    member_id: str
    app_role: str
    email_ref: str
    display_name: str
    ceiling: str  # highest tier the tool server's member ceiling allows


PERSONAS = {
    "maya": Persona("m-maya", "manager", "PERSONA_MAYA_EMAIL", "Maya", "restricted"),
    "sam": Persona("m-sam", "senior", "PERSONA_SAM_EMAIL", "Sam", "confidential"),
    "priya": Persona("m-priya", "junior", "PERSONA_PRIYA_EMAIL", "Priya", "team"),
}

SEARCH_LIMIT = 10  # retrieval.DEFAULT_LIMIT
WORKSPACE_ID = "workspace-primary"  # services.contracts.DEFAULT_WORKSPACE_ID
FALSE_ACCEPT_WEIGHT = 2
# Fixed candidate grid: 0.30, 0.31, ..., 0.95 (never derived from scores).
GRID: tuple[float, ...] = tuple(round(0.30 + step / 100, 2) for step in range(66))
# Rows of the printed candidate table (the chosen value is always added).
TABLE_STEP = 0.05

CORRECT_ACCEPT = "correct_accept"
MISGROUNDED_ACCEPT = "misgrounded_accept"
FALSE_ABSTAIN = "false_abstain"
FALSE_ACCEPT = "false_accept"
CORRECT_ABSTAIN = "correct_abstain"


# --- pure metric layer (no I/O; unit-tested) ------------------------------------


@dataclass(frozen=True)
class Hit:
    """One allowed search hit: score = cosine similarity, rank = RRF order."""

    source_id: str
    tier: str
    score: float
    rank: int = 1
    superseded: bool = False


@dataclass(frozen=True)
class ScoredCase:
    """A case's allowed hits (every search call the persona was allowed)."""

    case_id: str
    split: str
    case_type: str
    answerable: bool
    expected_source_ids: frozenset[str] = frozenset()
    hits: tuple[Hit, ...] = ()


def max_score(hits: Iterable[Hit]) -> float | None:
    """Best score among the hits, None when there are none."""
    scores = [hit.score for hit in hits]
    return max(scores) if scores else None


def accepts(hits: Iterable[Hit], threshold: float) -> bool:
    """The responder grounds on hits with score >= threshold (ties accept)."""
    return any(hit.score >= threshold for hit in hits)


def outcome(case: ScoredCase, threshold: float) -> str:
    """Outcome of one case at `threshold` (see the module docstring)."""
    accepted = accepts(case.hits, threshold)
    if not case.answerable:
        return FALSE_ACCEPT if accepted else CORRECT_ABSTAIN
    if not accepted:
        return FALSE_ABSTAIN
    grounded = any(
        hit.source_id in case.expected_source_ids and hit.score >= threshold for hit in case.hits
    )
    return CORRECT_ACCEPT if grounded else MISGROUNDED_ACCEPT


def calibration_cases(cases: Iterable[ScoredCase]) -> list[ScoredCase]:
    """Cases a threshold can decide (conflict cases need a refutation link)."""
    return [case for case in cases if case.case_type != "conflict"]


@dataclass(frozen=True)
class Metrics:
    threshold: float
    total: int
    answerable: int
    unanswerable: int
    false_accepts: tuple[str, ...] = ()
    false_abstains: tuple[str, ...] = ()
    misgrounded: tuple[str, ...] = ()

    @property
    def cost(self) -> int:
        return (
            FALSE_ACCEPT_WEIGHT * len(self.false_accepts)
            + len(self.false_abstains)
            + len(self.misgrounded)
        )

    @property
    def errors(self) -> int:
        return len(self.false_accepts) + len(self.false_abstains) + len(self.misgrounded)


def evaluate(cases: Iterable[ScoredCase], threshold: float) -> Metrics:
    """Outcome counts over the calibration cases (case ids sorted)."""
    usable = calibration_cases(cases)
    by_outcome: dict[str, list[str]] = {}
    for case in usable:
        by_outcome.setdefault(outcome(case, threshold), []).append(case.case_id)
    answerable = sum(1 for case in usable if case.answerable)
    return Metrics(
        threshold=threshold,
        total=len(usable),
        answerable=answerable,
        unanswerable=len(usable) - answerable,
        false_accepts=tuple(sorted(by_outcome.get(FALSE_ACCEPT, []))),
        false_abstains=tuple(sorted(by_outcome.get(FALSE_ABSTAIN, []))),
        misgrounded=tuple(sorted(by_outcome.get(MISGROUNDED_ACCEPT, []))),
    )


def choose_threshold(train: Iterable[ScoredCase], grid: Sequence[float] = GRID) -> float:
    """MIN_SCORE from TRAIN cases only: the upper median of the minimal-cost
    grid values. Raises ValueError on a held-out case, no usable train case,
    or an empty grid (calibration needs data; it never guesses)."""
    cases = list(train)
    if any(case.split != "train" for case in cases):
        raise ValueError("only train cases may choose the threshold")
    usable = calibration_cases(cases)
    if not usable:
        raise ValueError("no train cases to calibrate on")
    candidates = sorted(set(grid))
    if not candidates:
        raise ValueError("empty candidate grid")
    costs = [(evaluate(usable, value).cost, value) for value in candidates]
    best = min(cost for cost, _ in costs)
    tied = [value for cost, value in costs if cost == best]
    return tied[len(tied) // 2]


@dataclass(frozen=True)
class Calibration:
    threshold: float
    train: Metrics
    heldout: Metrics
    sweep: tuple[tuple[Metrics, Metrics], ...] = field(default=())


def calibrate(cases: Iterable[ScoredCase], grid: Sequence[float] = GRID) -> Calibration:
    """Choose on train, evaluate held-out at the chosen value; plus the sweep."""
    all_cases = list(cases)
    unknown = sorted({case.split for case in all_cases} - set(SPLITS))
    if unknown:
        raise ValueError(f"unknown split(s): {unknown}")
    train = [case for case in all_cases if case.split == "train"]
    heldout = [case for case in all_cases if case.split == "heldout"]
    threshold = choose_threshold(train, grid)
    sweep = tuple((evaluate(train, value), evaluate(heldout, value)) for value in sorted(set(grid)))
    return Calibration(
        threshold=threshold,
        train=evaluate(train, threshold),
        heldout=evaluate(heldout, threshold),
        sweep=sweep,
    )


# --- eval set --------------------------------------------------------------------


class EvalSetError(ValueError):
    """The eval set is malformed (exit 2)."""


def validate_case(case: Any, where: str) -> list[str]:
    """Problems with one case object (empty = valid)."""
    if not isinstance(case, dict):
        return [f"{where}: not an object"]
    problems = []
    keys = set(case)
    if keys != CASE_KEYS:
        problems.append(
            f"{where}: keys missing {sorted(CASE_KEYS - keys)} unknown {sorted(keys - CASE_KEYS)}"
        )
        return problems
    if not isinstance(case["id"], str) or not case["id"].strip():
        problems.append(f"{where}: id must be a non-empty string")
    if case["case_type"] not in CASE_TYPES:
        problems.append(f"{where}: unknown case_type")
    if case["persona"] not in PERSONAS:
        problems.append(f"{where}: unknown persona")
    tiers = case["tiers"]
    if (
        not isinstance(tiers, list)
        or not tiers
        or any(tier not in TIER_RANK for tier in tiers)
        or len(set(tiers)) != len(tiers)
    ):
        problems.append(f"{where}: tiers must be distinct known tiers")
    if not isinstance(case["question"], str) or not case["question"].strip():
        problems.append(f"{where}: question must be a non-empty string")
    if not isinstance(case["expected_answerable"], bool):
        problems.append(f"{where}: expected_answerable must be a boolean")
    expected = case["expected_source_ids"]
    if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
        problems.append(f"{where}: expected_source_ids must be a list of strings")
    reason = case["expected_abstain_reason"]
    if case["expected_answerable"] is True:
        if reason is not None or not expected:
            problems.append(f"{where}: answerable cases name sources and no abstain reason")
    elif reason not in ABSTAIN_REASONS:
        problems.append(f"{where}: unanswerable cases need an abstain reason")
    if (case["case_type"] == "conflict") != (reason == "conflict"):
        problems.append(f"{where}: conflict cases (only) expect the conflict abstention")
    return problems


def load_cases(eval_dir: Path = EVAL_DIR) -> list[dict[str, Any]]:
    """Both split files, validated; each case gains its `split`."""
    cases: list[dict[str, Any]] = []
    problems: list[str] = []
    for split, name in SPLIT_FILES.items():
        path = eval_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EvalSetError(f"{name}: unreadable ({type(exc).__name__})") from exc
        if not isinstance(data, list):
            raise EvalSetError(f"{name}: must be a JSON list of cases")
        for index, case in enumerate(data):
            found = validate_case(case, f"{name}[{index}]")
            problems.extend(found)
            if not found:
                cases.append({**case, "split": split})
    ids = [case["id"] for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        problems.append(f"duplicate case ids: {duplicates}")
    if problems:
        raise EvalSetError("; ".join(problems))
    return cases


def superseded_seed_ids(sources_dir: Path = SOURCES_DIR) -> frozenset[str]:
    """Seed source ids whose status is `superseded` (data files, read only)."""
    found = set()
    for path in sorted(sources_dir.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") == "superseded":
            found.add(record["source_id"])
    return frozenset(found)


# --- tool path (impure: in-process tool server + database) -------------------------


@dataclass
class CallOutcome:
    tier: str
    tool: str
    allowed: bool
    code: str | None
    result: Any = None  # KnowledgeSearchResult when allowed


@dataclass
class CaseRun:
    case: dict[str, Any]
    calls: list[CallOutcome]
    response: Any = None  # ResponderOutput


class ToolPath:
    """The real tool server app, in-process, behind bearer + signed `_ctx`."""

    def __init__(self, dsn: str) -> None:
        from fastapi.testclient import TestClient

        from services.toolserver.config import Settings
        from services.toolserver.main import create_app

        self._bearer = secrets.token_hex(32)
        self._ctx_secret = secrets.token_hex(32)
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="test",
            log_level="ERROR",
            mcp_allowed_hosts="testserver",
            mcp_enable_diagnostic_tools=False,
            toolserver_bearer_memory=self._bearer,
            toolserver_bearer_workspace=secrets.token_hex(32),
            ctx_secret=self._ctx_secret,
            database_url_toolserver=dsn,
        )
        self._client = TestClient(create_app(settings), raise_server_exceptions=False)

    def __enter__(self) -> ToolPath:
        self._client.__enter__()  # lifespan: pool + embedding warm-up
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.__exit__(*exc)

    def _ctx(self, persona: Persona, tool: str, request_id: str, step: int) -> str:
        from services.authorization.ctx_envelope import CtxEnvelope, sign_ctx
        from services.contracts import McpEndpoint

        now = time.time()
        envelope = CtxEnvelope(
            request_id=request_id,
            trace_id=f"{request_id}-trace",
            workspace_id=WORKSPACE_ID,
            requester_member_id=persona.member_id,
            armoriq_member_email_ref=persona.email_ref,
            app_role=persona.app_role,
            plan_id=f"{request_id}-plan",
            step_index=step,
            tool=tool,
            aud=McpEndpoint.MEMORY.value,
            iat=now - 1.0,
            exp=now + 120.0,
            jti=uuid.uuid4().hex,
        )
        return sign_ctx(envelope, self._ctx_secret)

    def search(self, persona: Persona, tier: str, query: str, request_id: str, step: int):
        from services.contracts import KnowledgeSearchResult

        tool = SEARCH_TOOL[tier]
        arguments = {
            "query": query,
            "limit": SEARCH_LIMIT,
            "_ctx": self._ctx(persona, tool, request_id, step),
        }
        response = self._client.post(
            "/memory/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self._bearer}",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"tool server HTTP {response.status_code}")
        message = _sse_message(response.text)
        result = message["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return CallOutcome(tier, tool, False, json.loads(text)["code"])
        return CallOutcome(tier, tool, True, None, KnowledgeSearchResult.model_validate_json(text))


def _sse_message(body: str) -> dict[str, Any]:
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise RuntimeError("no SSE data frame")


def run_searches(path: ToolPath, cases: list[dict[str, Any]]) -> list[CaseRun]:
    runs = []
    for case in sorted(cases, key=lambda item: (SPLITS.index(item["split"]), item["id"])):
        persona = PERSONAS[case["persona"]]
        calls = [
            path.search(persona, tier, case["question"], f"e17-{case['id']}", step)
            for step, tier in enumerate(case["tiers"])
        ]
        runs.append(CaseRun(case, calls))
    return runs


def scored(run: CaseRun) -> ScoredCase:
    hits = tuple(
        Hit(
            source_id=hit.source.source_id,
            tier=hit.source.tier.value,
            score=hit.score,
            rank=hit.rank,
            superseded=hit.source.superseded_by is not None,
        )
        for call in run.calls
        if call.allowed
        for hit in call.result.hits
    )
    case = run.case
    return ScoredCase(
        case_id=case["id"],
        split=case["split"],
        case_type=case["case_type"],
        answerable=case["expected_answerable"],
        expected_source_ids=frozenset(case["expected_source_ids"]),
        hits=hits,
    )


# --- responder (production code, offline extractive generator) ---------------------

_EVIDENCE_ID = re.compile(r'<EVIDENCE_[0-9a-f]+ id="([^"]*)"')


def extractive_generator(system: str, evidence_block: str, question_block: str) -> Any:
    """Offline stand-in for the LLM: cites every grounding block it is given."""
    from apps.api.responder import ModelDraft

    found = _EVIDENCE_ID.findall(evidence_block)
    ids = list(dict.fromkeys(html.unescape(value) for value in found))
    answer = " ".join(f"[{source_id}]" for source_id in ids)
    return ModelDraft(answer_text=f"Per the records: {answer}" if ids else "", citations=ids)


def run_responder(runs: list[CaseRun], threshold: float) -> None:
    from datetime import UTC, datetime

    from apps.api.responder import evidence_from_results, respond
    from services.contracts import AppRole, AuthenticatedUser, EntryPoint, PersonaKey
    from services.contracts import RequestContext as Ctx

    async def answer_all() -> None:
        for run in runs:
            persona = PERSONAS[run.case["persona"]]
            ctx = Ctx(
                request_id=f"e17-{run.case['id']}",
                trace_id=f"e17-{run.case['id']}-trace",
                workspace_id=WORKSPACE_ID,
                requester=AuthenticatedUser(
                    member_id=persona.member_id,
                    persona=PersonaKey(run.case["persona"]),
                    display_name=persona.display_name,
                    app_role=AppRole(persona.app_role),
                    masked_email="x***@example.test",
                ),
                entry_point=EntryPoint.WEB,
                received_at=datetime(2026, 10, 17, tzinfo=UTC),
            )
            evidence = evidence_from_results([call.result for call in run.calls if call.allowed])
            run.response = await respond(
                ctx, run.case["question"], evidence, threshold, generate=extractive_generator
            )

    asyncio.run(answer_all())


# --- acceptance checks (PRD §8.3) --------------------------------------------------


@dataclass
class Acceptance:
    name: str
    met: bool
    evidence: str


def acceptance(
    runs: list[CaseRun], threshold: float, superseded: frozenset[str]
) -> list[Acceptance]:
    a1_bad, a2_bad, a3_bad, a4_bad, consistency_bad = [], [], [], [], []
    for run in runs:
        case_id = run.case["id"]
        persona = PERSONAS[run.case["persona"]]
        returned = {
            hit.source.source_id for call in run.calls if call.allowed for hit in call.result.hits
        }
        best = max_score(scored(run).hits)
        out = run.response
        if not out.abstained and (best is None or best < threshold):
            a1_bad.append(case_id)
        if any(cited not in returned for cited in out.citations):
            a2_bad.append(case_id)
        for call in run.calls:
            if call.allowed:
                for hit in call.result.hits:
                    if hit.source.superseded_by is not None or hit.source.source_id in superseded:
                        a3_bad.append(case_id)
                    if TIER_RANK[hit.source.tier.value] > TIER_RANK[persona.ceiling]:
                        a4_bad.append(case_id)
            above = TIER_RANK[call.tier] > TIER_RANK[persona.ceiling]
            if above and (call.allowed or call.code != "out_of_scope"):
                a4_bad.append(case_id)
            if not above and not call.allowed:
                a4_bad.append(f"{case_id}(allowed tier refused: {call.code})")
        if any(cited in superseded for cited in out.citations):
            a3_bad.append(case_id)
        if run.case["case_type"] != "conflict":
            predicted = outcome(scored(run), threshold) in (FALSE_ABSTAIN, CORRECT_ABSTAIN)
            if predicted != out.abstained:
                consistency_bad.append(case_id)
    denied = sum(
        1
        for run in runs
        for call in run.calls
        if TIER_RANK[call.tier] > TIER_RANK[PERSONAS[run.case["persona"]].ceiling]
    )
    return [
        Acceptance(
            "A1 no answer unless an allowed result clears MIN_SCORE",
            not a1_bad,
            f"violations: {sorted(set(a1_bad)) or 'none'}",
        ),
        Acceptance(
            "A2 every citation is a source returned in this request",
            not a2_bad,
            f"violations: {sorted(set(a2_bad)) or 'none'}",
        ),
        Acceptance(
            "A3 superseded records never presented as current",
            not a3_bad,
            f"superseded seed ids {sorted(superseded)}; returned/cited in: "
            f"{sorted(set(a3_bad)) or 'none'}",
        ),
        Acceptance(
            "A4 no unauthorized tier content or metadata",
            not a4_bad,
            f"{denied} above-ceiling calls, all refused out_of_scope with no result body; "
            f"violations: {sorted(set(a4_bad)) or 'none'}",
        ),
        Acceptance(
            "metric/responder consistency (non-conflict cases)",
            not consistency_bad,
            f"mismatches: {sorted(consistency_bad) or 'none'}",
        ),
    ]


# --- record ----------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _ids(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "none"


def corpus_facts(dsn: str) -> dict[str, Any]:
    """Read-only counts and the recorded embedding model (toolserver role)."""
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT count(*),
                      count(*) FILTER (WHERE status = 'active'
                          AND provenance ->> 'review_status' = 'authoritative'),
                      array_agg(DISTINCT provenance ->> 'embed_tool')
               FROM public.source_records WHERE workspace_id = %s""",
            (WORKSPACE_ID,),
        )
        sources, searchable, tools = cur.fetchone()
        cur.execute(
            "SELECT tier, count(*) FROM public.knowledge_chunks WHERE workspace_id = %s "
            "GROUP BY tier ORDER BY tier",
            (WORKSPACE_ID,),
        )
        chunks = dict(cur.fetchall())
        conn.rollback()
    return {
        "sources": sources,
        "searchable": searchable,
        "embed_tools": sorted(tool for tool in (tools or []) if tool),
        "chunks": chunks,
    }


def table_rows(calibration: Calibration) -> list[tuple[Metrics, Metrics]]:
    rows = []
    for train, heldout in calibration.sweep:
        on_step = abs(round(train.threshold / TABLE_STEP) * TABLE_STEP - train.threshold) < 1e-9
        if on_step or train.threshold == calibration.threshold:
            rows.append((train, heldout))
    return rows


def print_record(
    runs: list[CaseRun],
    calibration: Calibration,
    checks: list[Acceptance],
    facts: dict[str, Any],
    split: str,
) -> None:
    out = print
    cases = [run.case for run in runs]
    counts = {
        name: sum(1 for case in cases if case["split"] == name and case["case_type"] != "conflict")
        for name in SPLITS
    }
    out("# E17 grounding evaluation record (P3-08)")
    out(
        "harness: in-process tool server (create_app) over MCP streamable HTTP, bearer + "
        "HMAC-signed test _ctx (random per-run secret); real dispatcher (ctx verification, "
        "argument validation, member tier ceiling, handler, ledger) and hybrid retrieval on "
        "DATABASE_URL_TOOLSERVER. ArmorIQ not involved: NOT ArmorIQ evidence."
    )
    out(
        "responder: apps.api.responder.respond + evidence_from_results (no refutes links, as "
        "the orchestrator passes) with an offline extractive generator citing every grounding "
        "source (no LLM call)."
    )
    out(f"embedding model (source provenance): {', '.join(facts['embed_tools']) or 'unknown'}")
    chunks = ", ".join(f"{tier} {count}" for tier, count in facts["chunks"].items())
    out(
        f"corpus: {facts['sources']} source records, {facts['searchable']} searchable "
        f"(active + authoritative); chunks: {chunks}"
    )
    for name in SPLITS:
        in_split = [case for case in cases if case["split"] == name]
        types = sorted({case["case_type"] for case in in_split})
        per_type = ", ".join(
            f"{kind} {sum(1 for case in in_split if case['case_type'] == kind)}" for kind in types
        )
        out(f"{name}: {len(in_split)} cases ({counts[name]} calibration; {per_type})")
    out(
        f"search: limit {SEARCH_LIMIT} per call; grid {GRID[0]:.2f}..{GRID[-1]:.2f} step 0.01; "
        f"cost = {FALSE_ACCEPT_WEIGHT}*FA + FAb + MG; ties -> upper median"
    )
    out("")
    out("## Candidate thresholds (FA = false accepts, FAb = false abstains, MG = misgrounded)")
    out(
        "| MIN_SCORE | train FA | train FAb | train MG | train cost "
        "| held-out FA | held-out FAb | held-out MG |"
    )
    out("|---|---|---|---|---|---|---|---|")
    for train, heldout in table_rows(calibration):
        mark = " **chosen**" if train.threshold == calibration.threshold else ""
        out(
            f"| {train.threshold:.2f}{mark} | {len(train.false_accepts)} | "
            f"{len(train.false_abstains)} | {len(train.misgrounded)} | {train.cost} | "
            f"{len(heldout.false_accepts)} | {len(heldout.false_abstains)} | "
            f"{len(heldout.misgrounded)} |"
        )
    out("")
    out(f"## Chosen MIN_SCORE (train only): {calibration.threshold:.2f}")
    for name, metrics in (("train", calibration.train), ("held-out", calibration.heldout)):
        out(
            f"- {name}: {metrics.total} calibration cases ({metrics.answerable} answerable, "
            f"{metrics.unanswerable} unanswerable); false accepts {len(metrics.false_accepts)}"
            f" [{_ids(metrics.false_accepts)}]; false abstains {len(metrics.false_abstains)}"
            f" [{_ids(metrics.false_abstains)}]; misgrounded {len(metrics.misgrounded)}"
            f" [{_ids(metrics.misgrounded)}]"
        )
    train_cases = [scored(run) for run in runs if run.case["split"] == "train"]
    usable = calibration_cases(train_cases)
    ranges = []
    for label, wanted in (("answerable", True), ("unanswerable", False)):
        values = [max_score(case.hits) for case in usable if case.answerable is wanted]
        present = [value for value in values if value is not None]
        span = f"{_fmt(min(present))}..{_fmt(max(present))}" if present else "-"
        ranges.append(f"{label} {span} ({len(values) - len(present)} with no hits)")
    out(f"- train max-score ranges: {'; '.join(ranges)}")
    out("")
    out("## Acceptance (PRD §8.3)")
    for check in checks:
        out(f"- {check.name}: {'MET' if check.met else 'NOT MET'} ({check.evidence})")
    conflicts = [run for run in runs if run.case["case_type"] == "conflict"]
    for run in conflicts:
        reason = run.response.abstain_reason
        out(
            f"- conflict {run.case['id']} ({run.case['split']}): abstain_reason={reason} "
            f"citations={run.response.citations} -> "
            f"{'MET' if reason == 'conflict' else 'NOT MET (no refutation-link producer)'}"
        )
    out("")
    out(f"## Per-case results ({split}) at MIN_SCORE {calibration.threshold:.2f}")
    out("(#rank = RRF rank inside that tier's search call; max = best allowed score)")
    out(
        "| case | type | persona | calls (tier:status) | expected | top hits "
        "(source score #rank) | max | outcome | abstained (reason) | citations |"
    )
    out("|---|---|---|---|---|---|---|---|---|---|")
    for run in runs:
        case = run.case
        if split != "all" and case["split"] != split:
            continue
        item = scored(run)
        calls = " ".join(f"{call.tier}:{'ok' if call.allowed else call.code}" for call in run.calls)
        top = sorted(item.hits, key=lambda hit: (-hit.score, hit.source_id))[:3]
        tops = "; ".join(f"{hit.source_id} {hit.score:.4f} #{hit.rank}" for hit in top) or "none"
        is_conflict = case["case_type"] == "conflict"
        verdict = "conflict-case" if is_conflict else outcome(item, calibration.threshold)
        expected = ", ".join(case["expected_source_ids"]) or "abstain"
        response = run.response
        abstained = f"yes ({response.abstain_reason})" if response.abstained else "no"
        out(
            f"| {case['id']} | {case['case_type']} | {case['persona']} | {calls} | {expected} | "
            f"{tops} | {_fmt(max_score(item.hits))} | {verdict} | {abstained} | "
            f"{', '.join(response.citations) or '-'} |"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=(*SPLITS, "all"), default="all")
    parser.add_argument("--eval-dir", type=Path, default=EVAL_DIR)
    args = parser.parse_args(argv)
    dsn = os.environ.get("DATABASE_URL_TOOLSERVER", "").strip()
    if not dsn:
        print("e17: DATABASE_URL_TOOLSERVER is not set (toolserver login DSN)", file=sys.stderr)
        return 2
    try:
        cases = load_cases(args.eval_dir)
    except EvalSetError as exc:
        print(f"e17: invalid eval set: {exc}", file=sys.stderr)
        return 2
    try:
        with ToolPath(dsn) as path:
            runs = run_searches(path, cases)
        calibration = calibrate([scored(run) for run in runs])
        run_responder(runs, calibration.threshold)
        checks = acceptance(runs, calibration.threshold, superseded_seed_ids())
        facts = corpus_facts(dsn)
    except Exception as exc:  # never echo the DSN or a driver message
        print(f"e17: failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print_record(runs, calibration, checks, facts, args.split)
    return 0 if all(check.met for check in checks[:4]) else 3


if __name__ == "__main__":
    sys.exit(main())
