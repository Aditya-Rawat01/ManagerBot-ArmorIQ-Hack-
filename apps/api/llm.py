"""Gemini calls for the planner and the responder (ADR-032).

The LLM is Google Gemini through the Gemini Developer API (a Google AI Studio
key in ``GEMINI_API_KEY``, model ``LLM_MODEL``, default ``gemini-2.5-flash``).
This is the only module that imports the ``google.genai`` SDK, and it does so
lazily inside each function, so importing the API never loads the SDK.

One JSON-mode call per request: the system prompt goes in
``system_instruction``, the nonce-delimited user turn is the only user
``Content``, and the caller's closed JSON Schema goes in
``response_json_schema`` with ``response_mime_type="application/json"``. No
tools and no function calling are sent. The callers keep their own strict
parsers, so this module returns raw text and never a parsed object.

Fail closed. Only a clean, complete answer returns text:

- SDK and HTTP errors (``google.genai.errors.APIError``: ``ClientError`` for
  4xx, ``ServerError`` for 5xx), timeouts and transport errors propagate
  unchanged;
- a blocked prompt (``prompt_feedback.block_reason``), no candidate, a finish
  reason other than ``STOP`` (``MAX_TOKENS`` truncation, ``SAFETY``,
  ``RECITATION``, ``OTHER``, a missing reason, ...), or no non-empty answer
  text raises :class:`LLMOutputError`.

Nothing here logs. Callers log the exception class only. Error messages carry
enum labels, never prompt or response text, and never the key.
"""

from __future__ import annotations

import re
from typing import Any

GEMINI_TIMEOUT_MS = 60_000  # per HTTP attempt (HttpOptions.timeout is in ms)
# The original request plus one retry. The SDK retries only 408/429/5xx and
# connect/timeout errors.
GEMINI_RETRY_ATTEMPTS = 2
# Gemini 2.5 counts thinking tokens against this limit, so it leaves room for
# THINKING_BUDGET plus the JSON answer.
MAX_OUTPUT_TOKENS = 16_000
THINKING_BUDGET = 2_048  # tokens; 0 disables thinking, -1 lets the model decide

_FINISH_STOP = "STOP"
_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


class LLMOutputError(ValueError):
    """The model answered, but not with complete, usable output."""


def gemini_client(api_key: str) -> Any:
    """A ``google.genai.Client`` for the Gemini Developer API.

    Construction makes no network call. ``vertexai=False`` is explicit so the
    ``GOOGLE_GENAI_USE_VERTEXAI`` environment variable can never redirect
    the key to Vertex AI. The key travels only in the SDK's
    ``x-goog-api-key`` header.
    """
    from google import genai
    from google.genai import types

    return genai.Client(
        vertexai=False,
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=GEMINI_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=GEMINI_RETRY_ATTEMPTS),
        ),
    )


def generation_config(system: str, schema: dict[str, Any]) -> Any:
    """The ``GenerateContentConfig`` for one JSON-mode call.

    No tools are sent, and the SDK's client-side automatic function calling
    is disabled outright. Temperature stays at the model default (Google's
    guidance for Gemini 3 models; the trusted validators are deterministic
    either way).
    """
    from google.genai import types

    return types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(thinking_budget=THINKING_BUDGET),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


async def generate_json(
    client: Any, *, model: str, system: str, user: str, schema: dict[str, Any]
) -> str:
    """One JSON-mode call. Returns the answer text or raises (fail closed)."""
    from google.genai import types

    response = await client.aio.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=user)])],
        config=generation_config(system, schema),
    )
    return usable_text(response)


def usable_text(response: Any) -> str:
    """The first candidate's answer text, only if it finished with ``STOP``.

    Thought parts are skipped (they are never requested). Raises
    :class:`LLMOutputError` on anything else, so a truncated or filtered
    answer can never reach a parser as a partial result.
    """
    feedback = getattr(response, "prompt_feedback", None)
    blocked = getattr(feedback, "block_reason", None)
    if blocked is not None:
        raise LLMOutputError(f"prompt blocked (block_reason={_label(blocked)})")
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise LLMOutputError("no candidate returned")
    candidate = candidates[0]
    reason = _label(getattr(candidate, "finish_reason", None))
    if reason != _FINISH_STOP:
        raise LLMOutputError(f"no usable output (finish_reason={reason})")
    parts = getattr(getattr(candidate, "content", None), "parts", None) or []
    text = "".join(part.text for part in parts if _is_answer_text(part))
    if not text.strip():
        raise LLMOutputError("model returned no answer text")
    return text


def _is_answer_text(part: Any) -> bool:
    return (
        isinstance(getattr(part, "text", None), str) and getattr(part, "thought", None) is not True
    )


def _label(value: object) -> str:
    """Enum label for an error message: identifier characters only, clipped."""
    if value is None:
        return "none"
    raw = getattr(value, "value", value)  # the SDK enums are str enums
    return _LABEL_UNSAFE.sub("?", str(raw))[:40] or "?"


__all__ = [
    "GEMINI_RETRY_ATTEMPTS",
    "GEMINI_TIMEOUT_MS",
    "MAX_OUTPUT_TOKENS",
    "THINKING_BUDGET",
    "LLMOutputError",
    "gemini_client",
    "generate_json",
    "generation_config",
    "usable_text",
]
