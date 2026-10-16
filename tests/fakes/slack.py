"""Offline Slack fakes (P3-05; reusable by P3-06 / P3-09 / the live DB probe).

Payload builders follow the REAL Events API / Socket Mode ``event_callback``
envelope: ``event_id``, ``team_id``, ``api_app_id`` and ``authorizations``
live on the OUTER body; the inner ``event`` carries ``type``, ``user``,
``text``, ``channel``, ``ts`` (and ``channel_type`` for messages). Nothing
here talks to Slack.
"""

from typing import Any

BOT_USER_ID = "U-bot"
TEAM_ID = "T1"


def envelope(
    event: dict[str, Any],
    *,
    event_id: str | None,
    team_id: str = TEAM_ID,
    bot_user_id: str | None = BOT_USER_ID,
) -> dict[str, Any]:
    """Wrap an inner event in the outer ``event_callback`` body Slack delivers."""
    body: dict[str, Any] = {
        "team_id": team_id,
        "api_app_id": "A-proxylead",
        "event": event,
        "type": "event_callback",
        "event_time": 1700000000,
        "authorizations": [
            {
                "enterprise_id": None,
                "team_id": team_id,
                "user_id": bot_user_id,
                "is_bot": True,
                "is_enterprise_install": False,
            }
        ],
        "is_ext_shared_channel": False,
        "event_context": "4-fake-event-context",
    }
    if event_id is not None:
        body["event_id"] = event_id
    return body


def mention_body(
    text: str = f"<@{BOT_USER_ID}> atlas budget?",
    *,
    user: str = "U-sam",
    channel: str = "C1",
    event_id: str | None = "Ev1",
    team: str = TEAM_ID,
    ts: str = "1700000000.0002",
    thread_ts: str | None = "1700000000.0001",
    subtype: str | None = None,
    bot_user_id: str | None = BOT_USER_ID,
) -> dict[str, Any]:
    """A real-shaped ``app_mention`` body (no ``channel_type``: Slack omits it)."""
    event: dict[str, Any] = {
        "type": "app_mention",
        "user": user,
        "text": text,
        "ts": ts,
        "channel": channel,
        "event_ts": ts,
        "team": team,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if subtype is not None:
        event["subtype"] = subtype
    return envelope(event, event_id=event_id, team_id=team, bot_user_id=bot_user_id)


def dm_body(
    text: str = "hello bot",
    *,
    user: str = "U-sam",
    channel: str = "D-sam",
    event_id: str | None = "Ev2",
    team: str = TEAM_ID,
    ts: str = "1700000000.0003",
    subtype: str | None = None,
    bot_user_id: str | None = BOT_USER_ID,
) -> dict[str, Any]:
    """A real-shaped ``message.im`` body (``channel_type: "im"``)."""
    event: dict[str, Any] = {
        "type": "message",
        "channel_type": "im",
        "user": user,
        "text": text,
        "ts": ts,
        "channel": channel,
        "event_ts": ts,
        "team": team,
    }
    if subtype is not None:
        event["subtype"] = subtype
    return envelope(event, event_id=event_id, team_id=team, bot_user_id=bot_user_id)


__all__ = ["BOT_USER_ID", "TEAM_ID", "dm_body", "envelope", "mention_body"]
