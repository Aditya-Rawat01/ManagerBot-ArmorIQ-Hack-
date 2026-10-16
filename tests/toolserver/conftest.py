import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from services.toolserver.config import Settings
from services.toolserver.main import create_app

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


# --- embedding model: never downloaded by tests --------------------------------------

# Tests that may touch the real loader opt out of `_no_model_download` by
# requesting one of these fixtures (directly or through another fixture).
_REAL_LOADER_FIXTURES = frozenset({"real_model", "fake_text_embedding"})


def model_cached() -> bool:
    """True when the FastEmbed model is in FastEmbed's cache directory:
    FASTEMBED_CACHE_PATH, else `<tempdir>/fastembed_cache` (FastEmbed's rule)."""
    base = os.environ.get("FASTEMBED_CACHE_PATH") or os.path.join(
        tempfile.gettempdir(), "fastembed_cache"
    )
    return any(Path(base).glob("models--*bge-small-en-v1.5*"))


@pytest.fixture(autouse=True)
def _no_model_download(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI never downloads the FastEmbed model (P2-05 review).

    Every app lifespan calls `embeddings.warm()`: here it is a no-op, so the
    app reports ready (`/health` 200) without a model. Any other attempt to
    load the model fails loudly instead of downloading it. Tests that need
    the real model request `real_model` (skipped unless the model is cached).
    """
    if _REAL_LOADER_FIXTURES & set(request.fixturenames):
        return
    from services.knowledge import embeddings

    def refuse_load() -> None:
        raise AssertionError("tests never load the embedding model; request `real_model`")

    monkeypatch.setattr(embeddings, "warm", lambda: None)
    monkeypatch.setattr(embeddings, "_load", refuse_load)


@pytest.fixture
def real_model(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real, process-wide model, loaded from the local cache only.

    Skips unless the model is cached; HF_HUB_OFFLINE makes FastEmbed refuse
    the network even if the cache is incomplete.
    """
    if not model_cached():
        pytest.skip("FastEmbed model not cached (no download in tests)")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from services.knowledge import embeddings

    embeddings.warm()
    return embeddings


class OneShotPool:
    """Minimal `ConnectionPool` stand-in for live tests: a fresh connection per
    checkout, closed afterwards (enough for `db.transaction`)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextmanager
    def connection(self) -> Iterator[Any]:
        import psycopg

        with psycopg.connect(self._dsn) as conn:
            yield conn


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
