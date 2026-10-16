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


def fake_web_client(
    *,
    errors: dict[str, str] | None = None,
    bot_user_id: str = BOT_USER_ID,
    team_id: str = TEAM_ID,
) -> Any:
    """A real ``slack_sdk.WebClient`` whose transport is replaced (no network).

    Every Web API call returns a genuine ``SlackResponse`` (``.validate()``
    raises a genuine ``SlackApiError`` for methods listed in ``errors``, e.g.
    ``{"chat.postMessage": "not_in_channel"}``), so Bolt's ``auth.test``,
    the sender and error-code extraction run their production code paths.
    Calls are recorded in ``client.calls`` as ``(api_method, args)``.
    """
    from slack_sdk.web import SlackResponse, WebClient

    class _FakeWebClient(WebClient):
        def __init__(self) -> None:
            super().__init__(token="xoxb-offline-fake", base_url="http://127.0.0.1:9/api/")
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.errors = dict(errors or {})

        def api_call(  # type: ignore[override]
            self,
            api_method: str,
            *,
            http_verb: str = "POST",
            files: dict[str, Any] | None = None,
            data: dict[str, Any] | None = None,
            params: dict[str, Any] | None = None,
            json: dict[str, Any] | None = None,
            headers: dict[str, Any] | None = None,
            auth: dict[str, Any] | None = None,
        ) -> SlackResponse:
            merged = {**(params or {}), **(json or {}), **(data or {})}
            # slack_sdk drops None-valued arguments before sending; so do we.
            args = {key: value for key, value in merged.items() if value is not None}
            self.calls.append((api_method, args))
            error = self.errors.get(api_method)
            if error is not None:
                payload: dict[str, Any] = {"ok": False, "error": error}
            elif api_method == "auth.test":
                payload = {
                    "ok": True,
                    "user_id": bot_user_id,
                    "bot_id": "B-bot",
                    "team_id": team_id,
                    "team": "Fake Team",
                    "url": "https://fake.invalid/",
                }
            elif api_method == "conversations.open":
                payload = {"ok": True, "channel": {"id": f"D-{args.get('users', 'x')}"}}
            else:
                payload = {"ok": True, "ts": "1700000009.0001"}
            return SlackResponse(
                client=self,
                http_verb=http_verb,
                api_url=f"{self.base_url}{api_method}",
                req_args={"params": params, "json": json},
                data=payload,
                headers={},
                status_code=200,
            ).validate()

    return _FakeWebClient()


def offline_bolt_app(client: Any | None = None) -> Any:
    """A real Bolt ``App`` wired to :func:`fake_web_client` (auth.test is faked)."""
    from slack_bolt import App

    return App(client=client if client is not None else fake_web_client())


__all__ = [
    "BOT_USER_ID",
    "TEAM_ID",
    "dm_body",
    "envelope",
    "fake_web_client",
    "mention_body",
    "offline_bolt_app",
]
