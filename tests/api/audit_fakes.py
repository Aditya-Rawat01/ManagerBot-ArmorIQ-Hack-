"""Offline test doubles and builders for the P3-01 audit store tests.

Test-local only (never imported by production code): no DSN, network,
ArmorIQ, or Slack. Sample secrets are obviously-fake constants.
"""

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

from services.audit import AuditStoreError
from services.contracts import (
    AppRole,
    ApprovalState,
    ApprovalStatus,
    ArmorIQDecision,
    AuthenticatedUser,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    EntryPoint,
    McpEndpoint,
    PersonaKey,
    PlannedToolCall,
    PlanProposal,
    RequestContext,
    ReviewStatus,
    SourceCitation,
    ToolName,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
FAKE_SECRET = "sk-fake-test-secret-0000"


class FakeCursor:
    """Records statements; replays scripted reads. Never touches a database.

    ``fail_with`` is raised by the first ``execute`` whose SQL contains
    ``fail_on`` (any statement when ``fail_on`` is None).
    """

    def __init__(
        self,
        fetchone: list[object] | None = None,
        fetchall: list[object] | None = None,
        rowcount: int = 1,
        fail_with: BaseException | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.statements: list[tuple[str, object]] = []
        self._one = list(fetchone or [])
        self._all = list(fetchall or [])
        self.rowcount = rowcount
        self._fail_with = fail_with
        self._fail_on = fail_on

    def execute(self, query: str, params: object = None) -> None:
        self.statements.append((query, params))
        if self._fail_with is not None and (self._fail_on is None or self._fail_on in query):
            raise self._fail_with

    def fetchone(self) -> object:
        return self._one.pop(0) if self._one else None

    def fetchall(self) -> list[object]:
        if self._all:
            return self._all.pop(0)  # type: ignore[return-value]
        return []


class FakeDatabase:
    """One shared script across cursors; commits statements per transaction."""

    def __init__(
        self,
        fetchone: list[object] | None = None,
        fetchall: list[object] | None = None,
        rowcount: int = 1,
        fail_with: BaseException | None = None,
        fail_on: str | None = None,
    ) -> None:
        self._one = list(fetchone or [])
        self._all = list(fetchall or [])
        self._rowcount = rowcount
        self._fail_with = fail_with
        self._fail_on = fail_on
        self.transactions: list[list[tuple[str, object]]] = []
        self.committed: list[tuple[str, object]] = []
        self.attempted: list[tuple[str, object]] = []
        self.rolled_back = 0

    @contextmanager
    def tx(self) -> Iterator[FakeCursor]:
        cursor = FakeCursor(
            rowcount=self._rowcount, fail_with=self._fail_with, fail_on=self._fail_on
        )
        # Share the queues so multi-statement flows script in order.
        cursor._one, cursor._all = self._one, self._all
        try:
            yield cursor
        except Exception:
            self.rolled_back += 1
            self.attempted.extend(cursor.statements)
            raise
        self.transactions.append(list(cursor.statements))
        self.committed.extend(cursor.statements)
        self.attempted.extend(cursor.statements)


class ExplodingCursor(FakeCursor):
    def execute(self, query: str, params: object = None) -> None:
        super().execute(query, params)
        if len(self.statements) >= 2:
            raise AuditStoreError("fake connection lost")


class ExplodingDatabase(FakeDatabase):
    @contextmanager
    def tx(self) -> Iterator[FakeCursor]:
        cursor = ExplodingCursor()
        try:
            yield cursor
        except Exception:
            self.rolled_back += 1
            raise
        self.committed.extend(cursor.statements)


def all_sql(db: FakeDatabase) -> str:
    return "\n".join(sql for sql, _ in db.committed)


def all_params(db: FakeDatabase) -> str:
    return str([params for _, params in db.committed])


def all_text(db: FakeDatabase) -> str:
    return all_sql(db) + "\n" + all_params(db)


def statements_with(db: FakeDatabase, fragment: str) -> list[tuple[str, object]]:
    return [(sql, params) for sql, params in db.committed if fragment in sql]


def maya() -> AuthenticatedUser:
    return AuthenticatedUser(
        member_id="m-maya",
        persona=PersonaKey.MAYA,
        display_name="Maya",
        app_role=AppRole.MANAGER,
        masked_email="m***@example.com",
    )


def sam() -> AuthenticatedUser:
    return AuthenticatedUser(
        member_id="m-sam",
        persona=PersonaKey.SAM,
        display_name="Sam",
        app_role=AppRole.SENIOR,
        masked_email="s***@example.com",
    )


def make_ctx() -> RequestContext:
    return RequestContext(
        request_id="req-1",
        trace_id="trace-1",
        workspace_id="ws-1",
        requester=sam(),
        entry_point=EntryPoint.SLACK_MENTION,
        received_at=NOW,
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_event_id="E1",
    )


def origin_payload(member_id: str = "m-sam", workspace_id: str = "ws-1") -> dict[str, object]:
    """The redacted ``request_received`` payload ``create_request`` stores."""
    return {
        "workspace_id": workspace_id,
        "requester_member_id": member_id,
        "entry_point": "slack_mention",
        "slack_team_id": "T1",
        "slack_channel_id": "C1",
        "slack_thread_ts": None,
        "slack_event_id": "E1",
    }


def make_plan() -> PlanProposal:
    return PlanProposal(
        summary="budget question",
        steps=[
            PlannedToolCall(
                step_index=0,
                mcp=McpEndpoint.MEMORY,
                tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
                arguments={"query": "atlas budget", "limit": 5},
                purpose="search",
            )
        ],
    )


def make_decision(
    source: DecisionSource = DecisionSource.ARMORIQ_POLICY_DECISION,
    delegation_id: str | None = None,
) -> ArmorIQDecision:
    return ArmorIQDecision(
        outcome=DecisionOutcome.ALLOW,
        source=source,
        armoriq_plan_id="aplan-1",
        delegation_id=delegation_id,
        decided_at=NOW,
    )


def make_approval(
    state: ApprovalState = ApprovalState.PENDING,
    delegation_id: str | None = "dlg-1",
    plan_id: str = "plan-1",
) -> ApprovalStatus:
    return ApprovalStatus(
        request_id="req-1",
        plan_id=plan_id,
        step_index=0,
        tool=ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
        state=state,
        delegation_id=delegation_id,
        requested_at=NOW,
        resolved_at=NOW if state is not ApprovalState.PENDING else None,
    )


def make_citation() -> SourceCitation:
    return SourceCitation(
        source_id="src-001",
        title="Atlas budget",
        tier=DataTier.TEAM,
        author="Maya",
        created_at=NOW,
        version=1,
        review_status=ReviewStatus.DRAFT,
    )


class Outcome:
    """Structural stand-in for the gateway StepOutcome (no SDK import)."""

    def __init__(self, decision, approval=None, invocation=None) -> None:
        self.decision = decision
        self.approval = approval
        self.invocation = invocation


# --- static guard: names backend audit code must never mention ----------------

# Distinctive names: banned anywhere (code, SQL, docstrings).
BANNED_ANYWHERE = (
    "DATABASE_URL_TOOLSERVER",
    "DATABASE_URL_MIGRATE",
    "database_url_toolserver",
    "database_url_migrate",
    "armoriq_sdk",
    "services.toolserver",
    "source_records",
    "knowledge_chunks",
    "team_notes",
    "manager_guidance",
    "tool_execution_ledger",
    "tool_jti_claims",
    "egress_sink",
)
# Common English words that are also protected tables: banned in SQL context
# (upper-case SQL keyword, schema-qualified, or a quoted identifier).
_SQL_PROTECTED = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:public\.)?(?:decisions|tasks)\b"
    r"|\bpublic\.(?:decisions|tasks)\b"
    r"|['\"](?:decisions|tasks)['\"]"
)
# Adjacent string literals (a SQL keyword ending one line, the table name
# starting the next) are joined before scanning.
_ADJACENT_LITERALS = re.compile(r"['\"]\s*\n\s*['\"]|['\"][ \t]+['\"]")


def protected_name_offenders(lines: Sequence[str]) -> list[str]:
    """Lines naming a protected table / foreign DSN (``no-protected-tables`` exempts)."""
    kept = [line for line in lines if "no-protected-tables" not in line]
    offenders = [
        f"{line.strip()} [{token}]" for line in kept for token in BANNED_ANYWHERE if token in line
    ]
    merged = _ADJACENT_LITERALS.sub("", "\n".join(kept))
    offenders.extend(f"{match.group(0)} [sql]" for match in _SQL_PROTECTED.finditer(merged))
    return offenders
