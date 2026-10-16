"""Slack Web API sender for delivery (P3-05, SHARED_INTERFACES §11).

Thin wrappers: thread replies via ``chat.postMessage``; DMs via
``conversations.open`` + ``chat.postMessage``. Any Slack failure raises
:exc:`SlackError` (delivery never retries to a different destination —
P3-06 decides). The interface matches the ``SlackSender`` Protocol P3-06
expects (parameter names and failure semantics; asserted by
``tests/api/test_slack_events.py``).

No connection happens at import or construction time — only on method calls.
"""

from typing import Any


class SlackError(Exception):
    """A Slack send failed. Carries the API error code, never message text."""


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, str) and error:
            return error
    return type(exc).__name__


class SlackSender:
    """Concrete sender used by delivery (P3-06). Inject any compatible client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_token(cls, bot_token: str) -> "SlackSender":
        """Build from a bot token (``xoxb-...``). Still connects lazily."""
        if not bot_token:
            raise SlackError("refusing to build a sender with an empty token")
        from slack_sdk.web import WebClient

        return cls(WebClient(token=bot_token))

    def post_in_thread(self, channel_id: str, thread_ts: str | None, text: str) -> None:
        """Reply in a channel (or thread when ``thread_ts`` is set)."""
        try:
            kwargs: dict[str, object] = {"channel": channel_id, "text": text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            self._client.chat_postMessage(**kwargs)
        except Exception as exc:
            raise SlackError(f"chat.postMessage failed: {_error_code(exc)}") from exc

    def send_dm(self, slack_user_id: str, text: str) -> None:
        """Open (or reuse) the user's DM channel, then post. No fallback."""
        try:
            opened = self._client.conversations_open(users=[slack_user_id])
            channel_id = opened["channel"]["id"]
            self._client.chat_postMessage(channel=channel_id, text=text)
        except Exception as exc:
            raise SlackError(f"DM send failed: {_error_code(exc)}") from exc


__all__ = ["SlackError", "SlackSender"]
