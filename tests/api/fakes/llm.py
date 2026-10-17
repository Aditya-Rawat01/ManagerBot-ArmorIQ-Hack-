"""Planner-model and responder-generator doubles (offline; no API key, no network).

The planner double is P3-02's ``FakeModelClient`` (canned plan JSON, prompts
recorded); :class:`GatedModelClient` additionally blocks until released, for
background-task and concurrency tests. Responder generators return a fixed
``ModelDraft`` (P3-03 shape) and record their inputs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from apps.api.planner import ModelClient
from apps.api.responder import ModelDraft, ToolRequest
from tests.fakes.planner import FakeModelClient, conf_search, restricted_search, team_search, wire

__all__ = [
    "DraftRecorder",
    "FakeModelClient",
    "GatedModelClient",
    "conf_search",
    "draft",
    "restricted_search",
    "team_search",
    "wire",
]


class GatedModelClient(ModelClient):
    """Returns canned plan JSON only after :attr:`release` is set."""

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self.release = asyncio.Event()
        self.started = 0
        self.finished = 0

    async def propose_plan(self, *, system: str, user: str) -> str:
        self.started += 1
        await self.release.wait()
        self.finished += 1
        return self._responses.pop(0)


def draft(
    answer: str,
    citations: Sequence[str] = (),
    requested: Sequence[tuple[str, dict[str, object]]] = (),
    malformed: Sequence[str] = (),
) -> ModelDraft:
    """A structured responder draft (inline ``[id]`` markers are the caller's)."""
    return ModelDraft(
        answer_text=answer,
        citations=list(citations),
        requested_tools=[ToolRequest(tool=tool, arguments=dict(args)) for tool, args in requested],
        malformed_tool_requests=list(malformed),
    )


class DraftRecorder:
    """Sync responder generator: one fixed draft, every call recorded."""

    def __init__(self, result: ModelDraft | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, system: str, evidence_block: str, question_block: str) -> ModelDraft:
        self.calls.append((system, evidence_block, question_block))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
