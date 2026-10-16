"""Offline planner fakes (P3-02; reusable by P3-04 / P3-09 injection tests).

Test-local only: no network, no API key, no database. ``FakeModelClient``
replays canned plan JSON and records every (system, user) prompt it was
given; ``FakeTopicDb`` replays ``topic_access_rules`` rows and records the
thread each transaction ran on. Wire JSON follows the planner's structured
output shape: ``{"steps": [{"tool": name, "arguments": {...}}]}``.
"""

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

from apps.api.planner import ModelClient
from services.contracts import (
    AppRole,
    AuthenticatedUser,
    EntryPoint,
    PersonaKey,
    RequestContext,
    ToolName,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

Step = tuple[str, object]


class FakeModelClient(ModelClient):
    """Canned JSON out (or a raised exception), prompts recorded."""

    def __init__(self, responses: Sequence[str | Exception] = ()) -> None:
        self._responses = list(responses)
        self.prompts: list[tuple[str, str]] = []

    async def propose_plan(self, *, system: str, user: str) -> str:
        self.prompts.append((system, user))
        if not self._responses:
            raise AssertionError("FakeModelClient: unexpected model call")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def wire(*steps: Step) -> str:
    return json.dumps({"steps": [{"tool": tool, "arguments": args} for tool, args in steps]})


def team_search(query: str = "atlas budget", limit: int = 5) -> Step:
    return (ToolName.KNOWLEDGE_SEARCH_TEAM.value, {"query": query, "limit": limit})


def conf_search(query: str = "atlas budget", limit: int = 5) -> Step:
    return (ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL.value, {"query": query, "limit": limit})


def restricted_search(query: str = "atlas budget", limit: int = 5) -> Step:
    return (ToolName.KNOWLEDGE_SEARCH_RESTRICTED.value, {"query": query, "limit": limit})


_NAMES = {PersonaKey.MAYA: "Maya", PersonaKey.SAM: "Sam", PersonaKey.PRIYA: "Priya"}


def make_user(
    persona: PersonaKey = PersonaKey.SAM,
    role: AppRole = AppRole.SENIOR,
    member: str | None = None,
) -> AuthenticatedUser:
    return AuthenticatedUser(
        member_id=member or f"m-{persona.value}",
        persona=persona,
        display_name=_NAMES[persona],
        app_role=role,
        masked_email="x***@example.com",
    )


def make_ctx(
    persona: PersonaKey = PersonaKey.SAM,
    role: AppRole = AppRole.SENIOR,
    entry: EntryPoint = EntryPoint.SLACK_MENTION,
    *,
    member: str | None = None,
) -> RequestContext:
    slack_refs: dict[str, object] = (
        {}
        if entry is EntryPoint.WEB
        else {"slack_team_id": "T1", "slack_channel_id": "C1", "slack_event_id": "E1"}
    )
    return RequestContext(
        request_id="req-1",
        trace_id="trace-1",
        workspace_id="ws-1",
        requester=make_user(persona, role, member),
        entry_point=entry,
        received_at=NOW,
        **slack_refs,  # type: ignore[arg-type]
    )


def maya_ctx(entry: EntryPoint = EntryPoint.SLACK_MENTION) -> RequestContext:
    return make_ctx(PersonaKey.MAYA, AppRole.MANAGER, entry)


def priya_ctx(entry: EntryPoint = EntryPoint.SLACK_MENTION) -> RequestContext:
    return make_ctx(PersonaKey.PRIYA, AppRole.JUNIOR, entry)


class _FakeCursor:
    def __init__(self, rows: Sequence[object]) -> None:
        self._rows = list(rows)
        self.statements: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = None) -> None:
        self.statements.append((query, params))

    def fetchall(self) -> list[object]:
        return self._rows

    def fetchone(self) -> object:
        return None


class FakeTopicDb:
    """``topic_access_rules`` rows as ``(id, topic, max_tier)`` tuples."""

    def __init__(
        self, rules: Sequence[tuple[str, str, str]] = (), fail: Exception | None = None
    ) -> None:
        self._rules = list(rules)
        self._fail = fail
        self.threads: list[int] = []
        self.cursors: list[_FakeCursor] = []

    @contextmanager
    def tx(self) -> Iterator[_FakeCursor]:
        self.threads.append(threading.get_ident())
        if self._fail is not None:
            raise self._fail
        cursor = _FakeCursor(self._rules)
        self.cursors.append(cursor)
        yield cursor


def conf_rule_db(topic: str = "atlas budget", rule_id: str = "rule-7") -> FakeTopicDb:
    return FakeTopicDb([(rule_id, topic, "confidential")])
