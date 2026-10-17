"""Recording Slack sender (the P3-05 ``SlackSender`` shape; offline only)."""

from __future__ import annotations

from services.slack_socket.sender import SlackError


class RecordingSlackSender:
    """Records thread posts and DMs; can fail like Slack (raises ``SlackError``)."""

    def __init__(self, *, fail_dm: bool = False, fail_thread: bool = False) -> None:
        self.fail_dm = fail_dm
        self.fail_thread = fail_thread
        self.thread_posts: list[tuple[str, str | None, str]] = []
        self.dms: list[tuple[str, str]] = []

    def post_in_thread(self, channel_id: str, thread_ts: str | None, text: str) -> None:
        if self.fail_thread:
            raise SlackError("chat.postMessage failed: not_in_channel")
        self.thread_posts.append((channel_id, thread_ts, text))

    def send_dm(self, slack_user_id: str, text: str) -> None:
        if self.fail_dm:
            raise SlackError("DM send failed: channel_not_found")
        self.dms.append((slack_user_id, text))

    def all_text(self) -> str:
        return "\n".join(
            [text for _, _, text in self.thread_posts] + [text for _, text in self.dms]
        )


__all__ = ["RecordingSlackSender"]
