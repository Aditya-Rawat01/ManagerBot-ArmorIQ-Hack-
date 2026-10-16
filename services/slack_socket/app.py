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

import logging
import threading
from dataclasses import dataclass
from typing import Any

from apps.api.core.db import AuditDatabase
from apps.api.identity import MemberRecord
from services.contracts import PersonaKey
from services.slack_socket import events as slack_events

logger = logging.getLogger(__name__)


@dataclass
class SlackRuntime:
    """A running Socket Mode handler plus its worker thread (D-27 interim)."""

    handler: Any
    thread: threading.Thread

    def stop(self) -> None:
        try:
            self.handler.disconnect()
        except Exception:
            logger.warning("slack handler disconnect failed")
        try:
            self.handler.close()
        except Exception:
            logger.warning("slack handler close failed")


def build_app(
    *,
    bot_token: str,
    members: dict[PersonaKey, MemberRecord],
    expected_team_id: str | None,
    allowed_channels: set[str],
    workspace_id: str | None,
    db: AuditDatabase | None,
    handle: slack_events.OrchestratorHandle | None,
    app: Any | None = None,
) -> Any:
    """Create the Bolt app with mention/DM listeners (no connection yet).

    Pass ``app`` in tests to avoid Bolt's startup ``auth.test`` network call.
    """
    import asyncio

    from slack_bolt import App

    if app is None:
        app = App(token=bot_token)
    deps = {
        "members": members,
        "expected_team_id": expected_team_id,
        "allowed_channels": allowed_channels,
        "workspace_id": workspace_id,
        "db": db,
    }

    @app.event("app_mention")
    def _on_mention(body: dict[str, Any], ack: Any) -> None:
        incoming = slack_events.on_app_mention(body, ack, **deps)
        if (
            isinstance(incoming, slack_events.IncomingSlackEvent)
            and handle is not None
            and db is not None
        ):
            asyncio.run(slack_events.dispatch(handle, incoming, db))

    @app.event("message")
    def _on_message(body: dict[str, Any], ack: Any) -> None:
        event = body.get("event") if isinstance(body, dict) else None
        if not isinstance(event, dict) or event.get("channel_type") != "im":
            ack()
            return
        incoming = slack_events.on_direct_message(body, ack, **deps)
        if (
            isinstance(incoming, slack_events.IncomingSlackEvent)
            and handle is not None
            and db is not None
        ):
            asyncio.run(slack_events.dispatch(handle, incoming, db))

    return app


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
) -> SlackRuntime | None:
    """Start Socket Mode in a daemon thread, or return None when disabled.

    Disabled (normal, not an error) when tokens are unset or no orchestrator
    ``handle`` exists yet — a running bot that cannot dispatch would be wrong.
    """
    if not bot_token or not app_token:
        logger.info("slack socket mode disabled: tokens unset")
        return None
    if handle is None:
        logger.warning("slack socket mode disabled: no orchestrator handle (P3-04 pending)")
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
    )
    handler = SocketModeHandler(app, app_token)
    thread = threading.Thread(target=handler.connect, name="slack-socket-mode", daemon=True)
    thread.start()
    logger.info("slack socket mode started")
    return SlackRuntime(handler=handler, thread=thread)


def stop_slack_socket(runtime: SlackRuntime | None) -> None:
    """Stop a runtime started above. No-op for None (never leaks)."""
    if runtime is None:
        return
    runtime.stop()
    logger.info("slack socket mode stopped")


__all__ = ["SlackRuntime", "build_app", "start_slack_socket", "stop_slack_socket"]
