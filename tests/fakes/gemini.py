"""Offline Gemini doubles (ADR-032): no network, no real key, no live model.

``FakeGeminiClient`` stands in for ``google.genai.Client``. It exposes only
``client.aio.models.generate_content(model=..., contents=..., config=...)``,
records every call, and returns canned responses or raises canned
exceptions in order. ``gemini_response`` builds a real
``google.genai.types.GenerateContentResponse`` (the SDK's own model, from the
same camelCase JSON the API returns), so the code under test reads exactly
the objects the SDK hands back. ``mock_transport_client`` builds a real
``genai.Client`` whose HTTP goes to an ``httpx.MockTransport`` handler, for
wire-level checks of what the SDK would send.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

FAKE_GEMINI_KEY = "gemini-offline-fake-key-0000"


class _FakeModels:
    def __init__(self, outcomes: Sequence[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        if not self._outcomes:
            raise AssertionError("FakeGeminiClient: unexpected model call")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeAio:
    def __init__(self, outcomes: Sequence[Any]) -> None:
        self.models = _FakeModels(outcomes)


class FakeGeminiClient:
    """``client.aio.models.generate_content`` replaying canned outcomes."""

    def __init__(self, *outcomes: Any) -> None:
        self.aio = _FakeAio(outcomes)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.aio.models.calls


def response_json(
    text: str | None = None,
    *,
    finish_reason: str | None = "STOP",
    block_reason: str | None = None,
    thought: str | None = None,
    no_candidates: bool = False,
) -> dict[str, Any]:
    """The Gemini API's ``generateContent`` response body (camelCase JSON)."""
    body: dict[str, Any] = {}
    if block_reason is not None:
        body["promptFeedback"] = {"blockReason": block_reason}
    if not no_candidates and block_reason is None:
        parts: list[dict[str, Any]] = []
        if thought is not None:
            parts.append({"text": thought, "thought": True})
        if text is not None:
            parts.append({"text": text})
        candidate: dict[str, Any] = {"content": {"role": "model", "parts": parts}, "index": 0}
        if finish_reason is not None:
            candidate["finishReason"] = finish_reason
        body["candidates"] = [candidate]
    return body


def gemini_response(text: str | None = None, **kwargs: Any) -> Any:
    """A real SDK ``GenerateContentResponse`` (see :func:`response_json`)."""
    from google.genai import types

    return types.GenerateContentResponse.model_validate(response_json(text, **kwargs))


def json_response(payload: object, **kwargs: Any) -> Any:
    """:func:`gemini_response` whose answer text is ``json.dumps(payload)``."""
    return gemini_response(json.dumps(payload), **kwargs)


def mock_transport_client(handler: Callable[[Any], Any], *, api_key: str = FAKE_GEMINI_KEY) -> Any:
    """A real ``genai.Client`` (Developer API) sending HTTP to ``handler`` only."""
    import httpx
    from google import genai
    from google.genai import types

    transport = httpx.MockTransport(handler)
    return genai.Client(
        vertexai=False,
        api_key=api_key,
        http_options=types.HttpOptions(
            httpx_async_client=httpx.AsyncClient(transport=transport),
            httpx_client=httpx.Client(transport=transport),
        ),
    )
