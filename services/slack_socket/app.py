"""Slack Socket Mode lifecycle (P3-05, D-27 pending).

Nothing connects at import time. :func:`start_slack_socket` returns ``None``
whenever the bot/app tokens are unset (normal for local runs without Slack)
or no dispatch ``handle`` is available yet (P3-04 owns the orchestrator) —
`/health` stays green either way.

Process placement (D-27, unresolved): the handler currently runs on a daemon
thread inside the API process. That placement is PROPOSED, not approved —
do not treat it as the deployment answer, and do not start this in
production without the D-27 decision.
"""

import asyncio
import functools
import logging
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from apps.api.core.db import AuditDatabase
from apps.api.identity import MemberRecord
from services.contracts import PersonaKey
from services.slack_socket import events as slack_events
from services.slack_socket.sender import SlackSender

logger = logging.getLogger(__name__)


@dataclass
class SlackRuntime:
    """A running Socket Mode handler plus its worker thread (D-27 interim)."""

    handler: Any
    thread: threading.Thread

    def stop(self) -> None:
        try:
            self.handler.disconnect()
        except Exception as exc:
            logger.warning("slack handler disconnect failed error=%s", type(exc).__name__)
        try:
            self.handler.close()
        except Exception as exc:
            logger.warning("slack handler close failed error=%s", type(exc).__name__)


def build_app(
    *,
    bot_token: str,
    members: dict[PersonaKey, MemberRecord],
    expected_team_id: str | None,
    allowed_channels: set[str],
    workspace_id: str | None,
    db: AuditDatabase | None,
    handle: slack_events.OrchestratorHandle | None,
    loop: asyncio.AbstractEventLoop | None = None,
    app: Any | None = None,
    sender: slack_events.RefusalSender | None = None,
) -> Any:
    """Create the Bolt app with mention/DM listeners (no connection yet).

    ``loop`` is the API's (uvicorn's) running event loop, captured in the
    FastAPI lifespan. Bolt runs listeners on its own worker threads; each
    accepted event is handed to ``handle`` on ``loop`` with
    :func:`asyncio.run_coroutine_threadsafe`, so background tasks ``handle``
    creates keep running after it returns (P3-04: ``handle`` returns at once,
    the work continues asynchronously). A per-event ``asyncio.run`` would
    cancel those tasks the moment ``handle`` returned.

    ``sender`` posts the fixed polite refusal to unmapped senders; it
    defaults to a :class:`SlackSender` over the Bolt app's own Web API client.
    Pass ``app`` in tests to avoid Bolt's startup ``auth.test`` network call.
    """
    from slack_bolt import App

    if app is None:
        app = App(token=bot_token)
    if sender is None:
        sender = SlackSender(app.client)
    # Each listener gets exactly the keyword arguments it accepts: DMs have no
    # channel allowlist (passing one raised TypeError on every DM).
    dm_deps: dict[str, Any] = {
        "members": members,
        "expected_team_id": expected_team_id,
        "workspace_id": workspace_id,
        "db": db,
        "sender": sender,
    }
    mention_deps: dict[str, Any] = {**dm_deps, "allowed_channels": allowed_channels}

    def _submit(incoming: slack_events.IncomingSlackEvent | slack_events.SlackRejection) -> None:
        if not isinstance(incoming, slack_events.IncomingSlackEvent):
            return
        if handle is None or db is None or loop is None:
            logger.warning(
                "slack event not dispatched (no handle, database or loop) event_id=%s",
                incoming.event_id,
            )
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                slack_events.dispatch(handle, incoming, db), loop
            )
        except RuntimeError as exc:  # loop closed during shutdown
            logger.warning(
                "slack dispatch not scheduled event_id=%s request_id=%s error=%s",
                incoming.event_id,
                incoming.request_id,
                type(exc).__name__,
            )
            return
        future.add_done_callback(
            functools.partial(_log_dispatch_outcome, incoming.event_id, incoming.request_id)
        )

    # Bolt injects listener arguments by name; `context.bot_user_id` comes
    # from auth.test and is the only mention the adapter strips.
    @app.event("app_mention")
    def _on_mention(body: dict[str, Any], ack: Any, context: Any) -> None:
        bot_user_id = getattr(context, "bot_user_id", None)
        _submit(slack_events.on_app_mention(body, ack, **mention_deps, bot_user_id=bot_user_id))

    @app.event("message")
    def _on_message(body: dict[str, Any], ack: Any, context: Any) -> None:
        event = body.get("event") if isinstance(body, dict) else None
        if not isinstance(event, dict) or event.get("channel_type") != "im":
            ack()
            return
        bot_user_id = getattr(context, "bot_user_id", None)
        _submit(slack_events.on_direct_message(body, ack, **dm_deps, bot_user_id=bot_user_id))

    return app


def _log_dispatch_outcome(event_id: str, request_id: str, future: Future[str]) -> None:
    """Log a failed/cancelled dispatch by exception class only (never text)."""
    if future.cancelled():
        logger.warning("slack dispatch cancelled event_id=%s request_id=%s", event_id, request_id)
        return
    exc = future.exception()
    if exc is not None:
        logger.warning(
            "slack dispatch failed event_id=%s request_id=%s error=%s",
            event_id,
            request_id,
            type(exc).__name__,
        )


def start_slack_socket(
    *,
    bot_token: str | None,
    app_token: str | None,
    members: dict[PersonaKey, MemberRecord],
    expected_team_id: str | None,
    allowed_channels: set[str],
    workspace_id: str | None,
    db: AuditDatabase | None,
    handle: slack_events.OrchestratorHandle | None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> SlackRuntime | None:
    """Start Socket Mode in a daemon thread, or return None when disabled.

    Disabled (normal, not an error) when tokens are unset, no orchestrator
    ``handle`` exists yet, or no API event ``loop`` was given — a running bot
    that cannot dispatch would be wrong.
    """
    if not bot_token or not app_token:
        logger.info("slack socket mode disabled: tokens unset")
        return None
    if handle is None:
        logger.warning("slack socket mode disabled: no orchestrator handle (P3-04 pending)")
        return None
    if loop is None:
        logger.warning("slack socket mode disabled: no API event loop to dispatch on")
        return None
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = build_app(
        bot_token=bot_token,
        members=members,
        expected_team_id=expected_team_id,
        allowed_channels=allowed_channels,
        workspace_id=workspace_id,
        db=db,
        handle=handle,
        loop=loop,
    )
    handler = SocketModeHandler(app, app_token)
    thread = threading.Thread(
        target=_connect_quietly, args=(handler,), name="slack-socket-mode", daemon=True
    )
    thread.start()
    logger.info("slack socket mode started")
    return SlackRuntime(handler=handler, thread=thread)


def _connect_quietly(handler: Any) -> None:
    """Open the websocket; a failure is logged by class only (never a token)."""
    try:
        handler.connect()
    except Exception as exc:
        logger.error("slack socket mode connect failed error=%s", type(exc).__name__)


def stop_slack_socket(runtime: SlackRuntime | None) -> None:
    """Stop a runtime started above. No-op for None (never leaks)."""
    if runtime is None:
        return
    runtime.stop()
    logger.info("slack socket mode stopped")


__all__ = ["SlackRuntime", "build_app", "start_slack_socket", "stop_slack_socket"]
