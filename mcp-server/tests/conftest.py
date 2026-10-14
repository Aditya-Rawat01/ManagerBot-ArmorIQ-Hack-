import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

MEMORY_TOKEN = "test-memory-token"
WORKSPACE_TOKEN = "test-workspace-token"

# Header set used by ArmorIQ's documented MCP smoke test.
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        # Protocol version shown in ArmorIQ's MCP format documentation.
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "armoriq-agent", "version": "1.0.0"},
    },
}


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never depend on a developer's shell environment or `.env` file."""
    field_names = {name.upper() for name in Settings.model_fields}
    for key in list(os.environ):
        if key.upper() in field_names:
            monkeypatch.delenv(key, raising=False)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "mcp_allowed_hosts": "testserver",
        "toolserver_bearer_memory": MEMORY_TOKEN,
        "toolserver_bearer_workspace": WORKSPACE_TOKEN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(make_settings())) as test_client:
        yield test_client


def auth(token: str) -> dict[str, str]:
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


def sse_messages(response: httpx.Response) -> list[dict[str, Any]]:
    """Parse `event: message` / `data: {...}` SSE frames into JSON-RPC messages."""
    assert response.headers["content-type"].startswith("text/event-stream")
    messages: list[dict[str, Any]] = []
    event = None
    for raw_line in response.text.splitlines():
        line = raw_line.strip()
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            assert event == "message"
            messages.append(json.loads(line.removeprefix("data:").strip()))
    return messages


def rpc(client: TestClient, path: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=payload, headers=auth(token))
    assert response.status_code == 200, response.text
    messages = sse_messages(response)
    assert len(messages) == 1
    return messages[0]
