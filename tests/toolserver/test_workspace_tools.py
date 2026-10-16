"""Workspace-tool tests (P2-10). The honeypot never touches the network:
a static import scan plus a live call with `socket` patched to raise. Live
tests need a LOCAL DATABASE_URL_TOOLSERVER plus PROXYLEAD_DB_WRITE_TESTS=1
(sink rows are permanent) and skip otherwise. DSNs captured at collection
time, never logged.
"""

import ast
import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.contracts import ToolName
from services.toolserver import main
from services.toolserver.main import create_app
from services.toolserver.tools_workspace import PRODUCT_TOOLS

from .conftest import make_settings
from .test_tool_dispatch import (
    CTX_SECRET,
    FAKE_DSN,
    NEEDS_DB,
    FakeLedger,
    call,
    dev_client,
    live_client,
    make_ctx,
    result_text,
)

WORKSPACE_HONEYPOT = ToolName.SEND_EXTERNAL_MESSAGE.value
FOLLOWUP = ToolName.CREATE_FOLLOWUP_TASK.value
WORKSPACE_AUD = "workspace-mcp"
WORKSPACE_TOKEN = "test-workspace-token"
DEFAULT_WORKSPACE = "workspace-primary"

# persona -> (member id, app role, email env-var NAME) as the seed creates them.
PERSONAS: dict[str, tuple[str, str, str]] = {
    "maya": ("m-maya", "manager", "PERSONA_MAYA_EMAIL"),
    "sam": ("m-sam", "senior", "PERSONA_SAM_EMAIL"),
    "priya": ("m-priya", "junior", "PERSONA_PRIYA_EMAIL"),
}


def workspace_ctx(
    tool: str = WORKSPACE_HONEYPOT, *, persona: str = "sam", **overrides: object
) -> str:
    member, role, email_ref = PERSONAS[persona]
    values: dict[str, object] = {
        "tool": tool,
        "aud": WORKSPACE_AUD,
        "requester_member_id": member,
        "app_role": role,
        "armoriq_member_email_ref": email_ref,
    }
    values.update(overrides)
    return make_ctx(**values)


def scoped_key(workspace_id: str, member_id: str, key: str) -> str:
    """Independent re-statement of the stored key (tools_workspace B1 fix)."""
    return hashlib.sha256(f"{workspace_id}\x1f{member_id}\x1f{key}".encode()).hexdigest()


def task_args(key: str, **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "title": "Follow up on freeze",
        "description": "Confirm the freeze lifted",
        "owner": "sam",
        "priority": "high",
        "idempotency_key": key,
    }
    arguments.update(overrides)
    return arguments


def call_followup(
    client: TestClient, arguments: dict[str, object], *, persona: str = "sam", **ctx: object
) -> dict:
    return call(
        client,
        {**arguments, "_ctx": workspace_ctx(FOLLOWUP, persona=persona, **ctx)},
        token=WORKSPACE_TOKEN,
        path="/workspace/mcp",
        tool=FOLLOWUP,
    )


def ok_body(message: dict) -> dict:
    assert message["result"]["isError"] is False, message
    return json.loads(message["result"]["content"][0]["text"])


# --- offline database double ---------------------------------------------------

_INSERT_SQL = re.compile(
    r"INSERT INTO public\.(\w+)\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)(.*)", re.S | re.I
)
_SELECT_SQL = re.compile(r"SELECT (.+?) FROM public\.(\w+) WHERE (.+)", re.S | re.I)
_FILTER = re.compile(r"(\w+)\s*(?:=|IS NOT DISTINCT FROM)\s*%s", re.I)
_RETURNING = re.compile(r"RETURNING (.+)", re.S | re.I)
# The UNIQUE column per table, as in 0004 (`tasks`) and 0012 (`egress_sink`).
_UNIQUE_COLUMN = {"tasks": "idempotency_key", "egress_sink": "jti"}


class FakeUniqueViolation(Exception):
    """Stands in for psycopg's UniqueViolation (INSERT without ON CONFLICT)."""


class TablePool:
    """`ConnectionPool` double for the workspace handlers.

    Records every statement and keeps in-memory `tasks` / `egress_sink`
    tables: INSERT (unique column, `ON CONFLICT ... DO NOTHING`, RETURNING)
    and SELECT with `col = %s` / `col IS NOT DISTINCT FROM %s` filters. It
    interprets the SQL the handler actually sends, so a changed statement
    (dropped filter, dropped ON CONFLICT, hard-coded value) changes the
    outcome instead of passing silently. Never opens a socket.
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.tables: dict[str, list[dict[str, Any]]] = {"tasks": [], "egress_sink": []}
        self._next_id = 1000

    def close(self) -> None:
        pass

    @contextmanager
    def connection(self) -> Iterator["_FakeConnection"]:
        yield _FakeConnection(self)

    def run(self, query: str, params: tuple[Any, ...]) -> tuple[list[str], tuple | None]:
        text = " ".join(str(query).split())
        self.statements.append((text, params))
        if match := _INSERT_SQL.fullmatch(text):
            return self._insert(*match.groups(), params)
        if match := _SELECT_SQL.fullmatch(text):
            return self._select(*match.groups(), params)
        raise AssertionError(f"unexpected SQL: {text}")

    def _insert(
        self, table: str, columns: str, values: str, tail: str, params: tuple[Any, ...]
    ) -> tuple[list[str], tuple | None]:
        supplied = iter(params)
        row: dict[str, Any] = {}
        for name, value in zip(columns.split(","), values.split(","), strict=True):
            value = value.strip()
            row[name.strip()] = next(supplied) if value == "%s" else value.strip("'")
        assert next(supplied, None) is None, "unused SQL parameters"
        unique = _UNIQUE_COLUMN[table]
        if row.get(unique) is not None and any(
            existing.get(unique) == row[unique] for existing in self.tables[table]
        ):
            if "ON CONFLICT" in tail.upper():
                return [], None  # DO NOTHING: RETURNING yields no row
            raise FakeUniqueViolation(f"{table}.{unique}")
        self._next_id += 1
        row["id"] = self._next_id
        self.tables[table].append(row)
        returning = _RETURNING.search(tail)
        if returning is None:
            return [], None
        names = [name.strip() for name in returning.group(1).split(",")]
        return names, tuple(row.get(name) for name in names)

    def _select(
        self, columns: str, table: str, where: str, params: tuple[Any, ...]
    ) -> tuple[list[str], tuple | None]:
        names = [name.strip() for name in columns.split(",")]
        filters = _FILTER.findall(where)
        assert len(filters) == len(params), "every filter must be bound"
        for row in self.tables[table]:
            if all(row.get(column) == value for column, value in zip(filters, params, strict=True)):
                return names, tuple(row.get(name) for name in names)
        return names, None

    def rows(self, table: str) -> list[dict[str, Any]]:
        return list(self.tables[table])


class _FakeCursor:
    def __init__(self, pool: TablePool) -> None:
        self._pool = pool
        self._row: tuple | None = None
        self.description: list[SimpleNamespace] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        names, self._row = self._pool.run(query, tuple(params))
        self.description = [SimpleNamespace(name=name) for name in names] or None

    def fetchone(self) -> tuple | None:
        return self._row


class _FakeConnection:
    def __init__(self, pool: TablePool) -> None:
        self._pool = pool

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def execute(self, query: object, params: object = None) -> None:
        """`SET LOCAL statement_timeout` from `db.transaction` (no-op)."""

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._pool)


def table_pool_client(
    monkeypatch: pytest.MonkeyPatch, ledger: FakeLedger, pool: TablePool
) -> TestClient:
    """App whose lifespan gets `ledger` and `pool` instead of a database."""
    monkeypatch.setattr(main.db, "create_pool", lambda dsn, **kwargs: pool)
    monkeypatch.setattr(main, "Ledger", lambda pool: ledger)
    app = create_app(make_settings(ctx_secret=CTX_SECRET, database_url_toolserver=FAKE_DSN))
    return TestClient(app, raise_server_exceptions=False)


FORBIDDEN_NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "smtplib",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websockets",
    }
)


def honeypot_args(**overrides: object) -> dict:
    arguments: dict[str, object] = {
        "destination": "synthetic_sink",
        "message": "test dispatch ping",
    }
    arguments.update(overrides)
    return arguments


def test_honeypot_registered_on_workspace_only() -> None:
    assert set(PRODUCT_TOOLS) == {
        ToolName.SEND_EXTERNAL_MESSAGE,
        ToolName.CREATE_FOLLOWUP_TASK,
    }


def test_honeypot_module_imports_no_network_client() -> None:
    source = Path("services/toolserver/tools_workspace.py").read_text()
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module.split(".")[0])
    assert imports.isdisjoint(FORBIDDEN_NETWORK_MODULES), imports & FORBIDDEN_NETWORK_MODULES


@NEEDS_DB
def test_honeypot_requires_valid_ctx() -> None:
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    with live_client() as client:
        with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.egress_sink")
            before = cur.fetchone()[0]
        denied = call(
            client,
            {"destination": "synthetic_sink", "message": "x"},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=WORKSPACE_HONEYPOT,
        )
        assert result_text(denied)["code"] == "invalid_context"
        with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.egress_sink")
            assert cur.fetchone()[0] == before


@NEEDS_DB
def test_honeypot_writes_only_egress_sink() -> None:
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    jti = f"jti-honeypot-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(
            client,
            {**honeypot_args(message="synthetic probe"), "_ctx": workspace_ctx(jti=jti)},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=WORKSPACE_HONEYPOT,
        )
    assert message["result"]["isError"] is False
    body = json.loads(message["result"]["content"][0]["text"])
    assert body["delivered"] is False and body["sink"] == "synthetic_sink"
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT destination, message, tool_name FROM public.egress_sink WHERE request_id = %s",
            ("req-dispatch",),
        )
        rows = cur.fetchall()
    assert rows, "honeypot must write exactly its sink row"
    assert all(row[0] == "synthetic_sink" and row[2] == WORKSPACE_HONEYPOT for row in rows)
    assert any("synthetic probe" in row[1] for row in rows)


@NEEDS_DB
def test_honeypot_sink_row_carries_jti_and_requester() -> None:
    """I4 (spec §9.1, migration 0012): the row names the verified jti and
    requester, here a non-default persona."""
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    request_id = f"req-sink-{uuid.uuid4().hex}"
    jti = f"jti-sink-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(
            client,
            {
                **honeypot_args(),
                "_ctx": workspace_ctx(persona="priya", request_id=request_id, jti=jti),
            },
            token=WORKSPACE_TOKEN,
            path="/workspace/mcp",
            tool=WORKSPACE_HONEYPOT,
        )
    ok_body(message)
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workspace_id, jti, requester_member_id, created_at IS NOT NULL"
            " FROM public.egress_sink WHERE request_id = %s",
            (request_id,),
        )
        assert cur.fetchall() == [(DEFAULT_WORKSPACE, jti, "m-priya", True)]


def test_honeypot_sink_row_from_verified_ctx_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """I4 without a database: every correlation value comes from the `_ctx`."""
    ledger, pool = FakeLedger(), TablePool()
    workspace = f"ws-offline-{uuid.uuid4().hex[:8]}"
    request_id = f"req-sink-{uuid.uuid4().hex}"
    jti = f"jti-sink-{uuid.uuid4().hex}"
    with table_pool_client(monkeypatch, ledger, pool) as client:
        message = call(
            client,
            {
                **honeypot_args(message="offline probe"),
                "_ctx": workspace_ctx(
                    persona="priya", workspace_id=workspace, request_id=request_id, jti=jti
                ),
            },
            token=WORKSPACE_TOKEN,
            path="/workspace/mcp",
            tool=WORKSPACE_HONEYPOT,
        )
    ok_body(message)
    [row] = pool.rows("egress_sink")
    assert {key: value for key, value in row.items() if key != "id"} == {
        "workspace_id": workspace,
        "request_id": request_id,
        "tool_name": WORKSPACE_HONEYPOT,
        "destination": "synthetic_sink",
        "message": "offline probe",
        "jti": jti,
        "requester_member_id": "m-priya",
    }


@NEEDS_DB
def test_task_idempotency() -> None:
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    key = f"key-{uuid.uuid4().hex}"
    arguments = {
        "title": "Follow up on freeze",
        "description": "Confirm the freeze lifted",
        "owner": "sam",
        "priority": "high",
        "idempotency_key": key,
    }
    with live_client() as client:
        first = call(
            client,
            {**arguments, "_ctx": workspace_ctx(ToolName.CREATE_FOLLOWUP_TASK.value)},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=ToolName.CREATE_FOLLOWUP_TASK.value,
        )
        second = call(
            client,
            {**arguments, "_ctx": workspace_ctx(ToolName.CREATE_FOLLOWUP_TASK.value)},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=ToolName.CREATE_FOLLOWUP_TASK.value,
        )
    assert first["result"]["isError"] is False
    first_id = json.loads(first["result"]["content"][0]["text"])["task_id"]
    assert json.loads(second["result"]["content"][0]["text"])["task_id"] == first_id
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        # Rows are found by the returned task_id: the stored key is scoped (B1).
        cur.execute(
            "SELECT count(*) FROM public.tasks WHERE idempotency_key ="
            " (SELECT idempotency_key FROM public.tasks WHERE id = %s)",
            (int(first_id),),
        )
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM public.tasks WHERE idempotency_key = %s", (key,))
        assert cur.fetchone()[0] == 0, "the raw caller key is never stored"


@NEEDS_DB
def test_task_requester_from_ctx() -> None:
    # Local DB fixture: member rows are seeded by the operator (migration role)
    # alongside the corpus; live tool tests assume them like they assume sources.
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    key = f"key-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(
            client,
            {
                "title": "T",
                "owner": "priya",
                "priority": "low",
                "idempotency_key": key,
                "_ctx": workspace_ctx(ToolName.CREATE_FOLLOWUP_TASK.value),
            },
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=ToolName.CREATE_FOLLOWUP_TASK.value,
        )
    task_id = int(ok_body(message)["task_id"])
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT created_by_member_id, workspace_id, idempotency_key FROM public.tasks"
            " WHERE id = %s",
            (task_id,),
        )
        assert cur.fetchone() == (
            "m-sam",
            DEFAULT_WORKSPACE,
            scoped_key(DEFAULT_WORKSPACE, "m-sam", key),
        )


@NEEDS_DB
def test_task_idempotency_is_scoped_per_requester() -> None:
    """B1: the same caller key from two requesters creates two tasks; neither
    requester ever receives the other's task id or title."""
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    key = f"key-{uuid.uuid4().hex}"
    sam_title = f"Sam private {uuid.uuid4().hex[:8]}"
    priya_title = f"Priya own {uuid.uuid4().hex[:8]}"
    with live_client() as client:
        sam = ok_body(call_followup(client, task_args(key, title=sam_title), persona="sam"))
        priya = call_followup(client, task_args(key, title=priya_title), persona="priya")
        sam_again = ok_body(call_followup(client, task_args(key, title=sam_title), persona="sam"))
    priya_body = ok_body(priya)
    assert priya_body["task_id"] != sam["task_id"]
    assert priya_body["title"] == priya_title
    assert sam_title not in json.dumps(priya)
    assert sam_again["task_id"] == sam["task_id"]
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_by_member_id, title, idempotency_key FROM public.tasks"
            " WHERE id = ANY(%s) ORDER BY id",
            ([int(sam["task_id"]), int(priya_body["task_id"])],),
        )
        rows = cur.fetchall()
    assert [(row[1], row[2]) for row in rows] == [("m-sam", sam_title), ("m-priya", priya_title)]
    assert [row[3] for row in rows] == [
        scoped_key(DEFAULT_WORKSPACE, "m-sam", key),
        scoped_key(DEFAULT_WORKSPACE, "m-priya", key),
    ]


@NEEDS_DB
def test_task_replay_returns_stored_fields() -> None:
    """I1: a replay with different owner/priority/title reports the stored task."""
    key = f"key-{uuid.uuid4().hex}"
    with live_client() as client:
        first = ok_body(call_followup(client, task_args(key, owner="sam", priority="high")))
        replay = ok_body(
            call_followup(
                client, task_args(key, title="Changed title", owner="priya", priority="low")
            )
        )
    assert replay["task_id"] == first["task_id"]
    assert (replay["title"], replay["owner"], replay["priority"]) == (
        "Follow up on freeze",
        "sam",
        "high",
    )


def test_followup_replay_returns_stored_fields_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """I1 without a database: the replay result comes from the stored row."""
    ledger, pool = FakeLedger(), TablePool()
    key = f"key-{uuid.uuid4().hex}"
    with table_pool_client(monkeypatch, ledger, pool) as client:
        first = ok_body(call_followup(client, task_args(key, owner="sam", priority="high")))
        replay = ok_body(
            call_followup(
                client, task_args(key, title="Changed title", owner="priya", priority="low")
            )
        )
    assert replay == first
    assert (replay["title"], replay["owner"], replay["priority"]) == (
        "Follow up on freeze",
        "sam",
        "high",
    )
    assert len(pool.rows("tasks")) == 1


def test_followup_key_scoped_per_requester_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """B1 without a database: two requesters, one caller key, two rows; the
    same requester's replay returns its own row; the raw key is never stored."""
    ledger, pool = FakeLedger(), TablePool()
    key = f"key-{uuid.uuid4().hex}"
    with table_pool_client(monkeypatch, ledger, pool) as client:
        sam = ok_body(call_followup(client, task_args(key, title="Sam private"), persona="sam"))
        priya = call_followup(client, task_args(key, title="Priya own"), persona="priya")
        sam_again = ok_body(call_followup(client, task_args(key, title="Sam private")))
    priya_body = ok_body(priya)
    assert priya_body["task_id"] != sam["task_id"]
    assert priya_body["title"] == "Priya own" and "Sam private" not in json.dumps(priya)
    assert sam_again["task_id"] == sam["task_id"]
    rows = pool.rows("tasks")
    assert [(row["created_by_member_id"], row["idempotency_key"]) for row in rows] == [
        ("m-sam", scoped_key(DEFAULT_WORKSPACE, "m-sam", key)),
        ("m-priya", scoped_key(DEFAULT_WORKSPACE, "m-priya", key)),
    ]
    assert all(key not in str(params) for _, params in pool.statements)


def test_task_enum_bounds() -> None:
    with dev_client() as client:
        for arguments in (
            {"title": "T", "owner": "mallory", "priority": "low", "idempotency_key": "k1"},
            {"title": "T", "owner": "sam", "priority": "urgent", "idempotency_key": "k2"},
            {"title": "", "owner": "sam", "priority": "low", "idempotency_key": "k3"},
        ):
            message = call(
                client,
                {**arguments, "_ctx": workspace_ctx(ToolName.CREATE_FOLLOWUP_TASK.value)},
                token="test-workspace-token",
                path="/workspace/mcp",
                tool=ToolName.CREATE_FOLLOWUP_TASK.value,
            )
            assert result_text(message)["code"] == "invalid_arguments"


@NEEDS_DB
def test_honeypot_never_uses_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    real_socket = socket.socket
    allowed_hosts = {"127.0.0.1", "::1", "localhost"}

    class GuardSocket(real_socket):  # type: ignore[valid-type, misc]
        def connect(self, address: object, *args: object) -> object:
            host = address[0] if isinstance(address, tuple) else address
            if host not in allowed_hosts:
                raise AssertionError(f"network use attempted: {host}")
            return super().connect(address, *args)  # type: ignore[arg-type]

    # Localhost DB traffic allowed; any other destination raises.
    monkeypatch.setattr(socket, "socket", GuardSocket)
    jti = f"jti-honeypot-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(
            client,
            {**honeypot_args(), "_ctx": workspace_ctx(jti=jti)},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=WORKSPACE_HONEYPOT,
        )
    assert message["result"]["isError"] is False
