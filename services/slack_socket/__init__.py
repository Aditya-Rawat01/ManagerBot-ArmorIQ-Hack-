"""Slack Bolt (Socket Mode) adapter for one workspace (P3-05).

Identity comes only from the trusted Slack user ID mapping, never message text.
"""

from services.slack_socket.app import (
    SlackRuntime,
    build_app,
    start_slack_socket,
    stop_slack_socket,
)
from services.slack_socket.events import (
    IncomingSlackEvent,
    OrchestratorHandle,
    SlackRejection,
    dispatch,
    mark_processed,
    on_app_mention,
    on_direct_message,
    parse_store_this,
    process_event,
    resolve_workspace_id,
)
from services.slack_socket.sender import SlackError, SlackSender

__all__ = [
    "IncomingSlackEvent",
    "OrchestratorHandle",
    "SlackError",
    "SlackRejection",
    "SlackRuntime",
    "SlackSender",
    "build_app",
    "dispatch",
    "mark_processed",
    "on_app_mention",
    "on_direct_message",
    "parse_store_this",
    "process_event",
    "resolve_workspace_id",
    "start_slack_socket",
    "stop_slack_socket",
]
