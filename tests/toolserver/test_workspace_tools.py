"""Workspace-tool tests (P2-10). The honeypot never touches the network:
a static import scan, offline handler runs with every socket entry point
patched to raise (plus an armed audit hook), and a live call under the same
guard. Every call carries its own request_id and jti, so assertions name
exactly the rows that call wrote. Offline tests use `FakeLedger` and
`TablePool` (an in-memory double that interprets the handlers' SQL). Live
tests need a LOCAL DATABASE_URL_TOOLSERVER plus PROXYLEAD_DB_WRITE_TESTS=1
(sink, task and ledger rows are permanent) and skip otherwise. DSNs captured
at collection time, never logged.
"""

import ast
import hashlib
import json
import re
import socket
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.contracts import ToolExecutionStatus, ToolName
from services.toolserver import main
from services.toolserver.main import create_app
from services.toolserver.tools_workspace import PRODUCT_TOOLS

from .conftest import make_settings
from .test_tool_dispatch import (
    CTX_SECRET,
    FAKE_DSN,
    NEEDS_DB,
    NOW,
    FakeLedger,
    call,
    dev_client,
    list_tools,
    live_client,
    make_ctx,
    result_meta,
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


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def workspace_ctx(
    tool: str = WORKSPACE_HONEYPOT, *, persona: str = "sam", **overrides: object
) -> str:
    """A `_ctx` for workspace-mcp; request_id/trace_id/jti unique per call."""
    member, role, email_ref = PERSONAS[persona]
    values: dict[str, object] = {
        "tool": tool,
        "aud": WORKSPACE_AUD,
        "requester_member_id": member,
        "app_role": role,
        "armoriq_member_email_ref": email_ref,
        "request_id": new_id("req-ws"),
        "trace_id": new_id("trace-ws"),
    }
    values.update(overrides)
    return make_ctx(**values)


def scoped_key(workspace_id: str, member_id: str, key: str) -> str:
    """Independent re-statement of the stored key (tools_workspace B1 fix)."""
    return hashlib.sha256(f"{workspace_id}\x1f{member_id}\x1f{key}".encode()).hexdigest()


def honeypot_args(**overrides: object) -> dict:
    arguments: dict[str, object] = {
        "destination": "synthetic_sink",
        "message": "test dispatch ping",
    }
    arguments.update(overrides)
    return arguments


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


def call_honeypot(
    client: TestClient, arguments: dict[str, object] | None = None, **ctx: object
) -> dict:
    """`ctx` overrides the `_ctx` fields; `_ctx=None` sends no `_ctx` at all."""
    body = dict(arguments or honeypot_args())
    if "_ctx" in ctx:
        token = ctx.pop("_ctx")
        if token is not None:
            body["_ctx"] = token
    else:
        body["_ctx"] = workspace_ctx(**ctx)  # type: ignore[arg-type]
    return call(client, body, token=WORKSPACE_TOKEN, path="/workspace/mcp", tool=WORKSPACE_HONEYPOT)


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


def live_rows(query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Read rows as the toolserver role (SELECT on sink, tasks and ledger)."""
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def assert_live_executed_once(message: dict, jti: str, tool: str, request_id: str) -> None:
    """`_meta.ledger_entry_id` names this call's single `executed` ledger row."""
    entry_id = result_meta(message)["ledger_entry_id"]
    assert live_rows(
        "SELECT id, execution_status, validation_outcome, tool_name, request_id"
        " FROM public.tool_execution_ledger WHERE jti = %s",
        (jti,),
    ) == [(int(entry_id), "executed", "accepted", tool, request_id)]


def assert_fake_executed_once(message: dict, ledger: FakeLedger, jti: str) -> None:
    [(entry_id, entry)] = ledger.rows(jti)
    assert result_meta(message) == {"ledger_entry_id": str(entry_id)}
    assert entry.execution_status is ToolExecutionStatus.EXECUTED
    assert entry.validation_outcome == "accepted"


# --- network guard ---------------------------------------------------------------

_NETWORK_AUDIT_EVENTS = frozenset(
    {
        "socket.__new__",
        "socket.connect",
        "socket.sendto",
        "socket.sendmsg",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    }
)


class NetworkUseAttempted(AssertionError):
    """A guarded code path tried to use the network."""


class _Guard:
    def __init__(self, allow_dns_host: str | None) -> None:
        self.allow_dns_host = allow_dns_host
        self.attempts: list[str] = []

    def refuse(self, what: str) -> None:
        self.attempts.append(what)
        raise NetworkUseAttempted(what)


_ARMED: list[_Guard] = []


def _network_audit_hook(event: str, args: tuple[Any, ...]) -> None:
    # Catches socket use that bypasses the module attributes patched below
    # (e.g. `_socket` directly). Inert unless a test armed a guard.
    if not _ARMED or event not in _NETWORK_AUDIT_EVENTS:
        return
    guard = _ARMED[-1]
    if event == "socket.getaddrinfo" and args and args[0] == guard.allow_dns_host:
        return
    guard.refuse(f"audit:{event}")


sys.addaudithook(_network_audit_hook)  # process-wide but inert when unarmed


@contextmanager
def network_forbidden(
    monkeypatch: pytest.MonkeyPatch, *, allow_dns_host: str | None = None
) -> Iterator[list[str]]:
    """Every socket entry point raises (and is recorded) inside the block:
    `socket.socket`, `create_connection`, `getaddrinfo`, and the `connect`,
    `connect_ex`, `sendto` methods of existing sockets. `allow_dns_host` lets
    only the live database's own name resolve (psycopg resolves host names in
    Python when the pool opens a connection); offline tests allow nothing.
    Yields the list of refused attempts — a swallowed refusal still counts."""
    guard = _Guard(allow_dns_host)
    real_getaddrinfo = socket.getaddrinfo
    real_socket = socket.socket

    def refusing(what: str) -> Any:
        def _refuse(*args: object, **kwargs: object) -> None:
            guard.refuse(what)

        return _refuse

    def getaddrinfo(host: object, *args: Any, **kwargs: Any) -> Any:
        if allow_dns_host is not None and host == allow_dns_host:
            return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]
        guard.refuse(f"socket.getaddrinfo({host!r})")
        return None

    with monkeypatch.context() as patch:
        for method in ("connect", "connect_ex", "sendto"):
            patch.setattr(real_socket, method, refusing(f"socket.socket.{method}"))
        patch.setattr(socket, "socket", refusing("socket.socket"))
        patch.setattr(socket, "create_connection", refusing("socket.create_connection"))
        patch.setattr(socket, "getaddrinfo", getaddrinfo)
        _ARMED.append(guard)
        try:
            yield guard.attempts
        finally:
            _ARMED.remove(guard)


def test_network_guard_refuses_every_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard itself: TCP connect, UDP sendto, connect_ex, DNS, raw `_socket`."""
    import _socket

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with network_forbidden(monkeypatch) as attempts:
            probes = (
                lambda: socket.create_connection(("192.0.2.1", 25), timeout=0.1),
                lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                lambda: socket.getaddrinfo("example.invalid", 443),
                lambda: udp.sendto(b"x", ("192.0.2.1", 53)),
                lambda: udp.connect(("192.0.2.1", 53)),
                lambda: udp.connect_ex(("192.0.2.1", 53)),
                lambda: _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM),
                lambda: __import__("socket").create_connection(("192.0.2.1", 80)),
            )
            for probe in probes:
                with pytest.raises(NetworkUseAttempted):
                    probe()
        assert len(attempts) == len(probes)
    finally:
        udp.close()


# --- offline database double -----------------------------------------------------

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


# --- registration and static checks ----------------------------------------------

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


# --- send_external_message: offline ----------------------------------------------


def test_honeypot_writes_sink_row_without_network_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With every socket entry point raising, the honeypot completes exactly
    one sink write (values from the verified `_ctx`), touches no task row,
    and the call has its own `executed` ledger row named in `_meta`."""
    ledger, pool = FakeLedger(), TablePool()
    workspace, request_id, jti = new_id("ws-offline"), new_id("req-sink"), new_id("jti-sink")
    # The app (lifespan) starts first; the guard covers only the call.
    with (
        table_pool_client(monkeypatch, ledger, pool) as client,
        network_forbidden(monkeypatch) as attempts,
    ):
        message = call_honeypot(
            client,
            honeypot_args(message="offline probe"),
            persona="priya",
            workspace_id=workspace,
            request_id=request_id,
            jti=jti,
        )
    assert attempts == [], f"network use attempted: {attempts}"
    body = ok_body(message)
    assert body["delivered"] is False and body["sink"] == "synthetic_sink"
    [row] = pool.rows("egress_sink")
    assert (row["workspace_id"], row["request_id"], row["message"]) == (
        workspace,
        request_id,
        "offline probe",
    )
    assert (row["destination"], row["tool_name"]) == ("synthetic_sink", WORKSPACE_HONEYPOT)
    assert pool.rows("tasks") == []
    assert [text.split(" (")[0] for text, _ in pool.statements] == [
        "INSERT INTO public.egress_sink"
    ]
    assert_fake_executed_once(message, ledger, jti)


@pytest.mark.parametrize(
    ("label", "ctx_overrides", "code"),
    [
        ("missing", {"_ctx": None}, "invalid_context"),
        ("forged", {"secret": "not-the-ctx-secret"}, "invalid_context"),
        ("wrong audience", {"aud": "memory-mcp"}, "invalid_context"),
        ("wrong tool", {"tool": FOLLOWUP}, "invalid_context"),
        ("lifetime over 900 s", {"iat": NOW - 60.0, "exp": NOW + 1000.0}, "invalid_context"),
        ("expired", {"iat": NOW - 700.0, "exp": NOW - 100.0}, "context_expired"),
    ],
)
def test_honeypot_requires_valid_ctx_offline(
    monkeypatch: pytest.MonkeyPatch, label: str, ctx_overrides: dict, code: str
) -> None:
    """Rejected before any SQL runs: no sink row, no statement, no ledger row."""
    ledger, pool = FakeLedger(), TablePool()
    with table_pool_client(monkeypatch, ledger, pool) as client:
        message = call_honeypot(client, **ctx_overrides)
    assert result_text(message)["code"] == code, label
    assert pool.statements == [], label
    assert ledger.entries == {}, label


# --- send_external_message: live -------------------------------------------------


@NEEDS_DB
def test_honeypot_requires_valid_ctx() -> None:
    """Invalid `_ctx` variants carrying a unique request_id: none writes a
    sink row or a ledger row; the sink row count is unchanged."""
    request_id = new_id("req-noctx")
    jtis = [new_id("jti-noctx") for _ in range(5)]
    variants = [
        ({"_ctx": None}, "invalid_context"),
        ({"secret": "not-the-ctx-secret", "jti": jtis[0]}, "invalid_context"),
        ({"aud": "memory-mcp", "jti": jtis[1]}, "invalid_context"),
        ({"tool": FOLLOWUP, "jti": jtis[2]}, "invalid_context"),
        ({"iat": NOW - 60.0, "exp": NOW + 1000.0, "jti": jtis[3]}, "invalid_context"),
        ({"iat": NOW - 700.0, "exp": NOW - 100.0, "jti": jtis[4]}, "context_expired"),
    ]
    [(before,)] = live_rows("SELECT count(*) FROM public.egress_sink")
    with live_client() as client:
        for overrides, code in variants:
            ctx = dict(overrides)
            if "_ctx" not in ctx:
                ctx["request_id"] = request_id
            assert result_text(call_honeypot(client, **ctx))["code"] == code, overrides
    assert live_rows("SELECT count(*) FROM public.egress_sink") == [(before,)]
    assert live_rows(
        "SELECT count(*) FROM public.egress_sink WHERE request_id = %s", (request_id,)
    ) == [(0,)]
    assert live_rows(
        "SELECT count(*) FROM public.tool_execution_ledger WHERE jti = ANY(%s)", (jtis,)
    ) == [(0,)]


@NEEDS_DB
def test_honeypot_writes_only_egress_sink() -> None:
    """Exactly one sink row for this call's request_id, no task row, and one
    `executed` ledger row named by `_meta.ledger_entry_id`."""
    request_id, jti = new_id("req-sink"), new_id("jti-sink")
    [(tasks_before,)] = live_rows("SELECT count(*) FROM public.tasks")
    with live_client() as client:
        message = call_honeypot(
            client, honeypot_args(message="synthetic probe"), request_id=request_id, jti=jti
        )
    body = ok_body(message)
    assert body["delivered"] is False and body["sink"] == "synthetic_sink"
    assert live_rows(
        "SELECT workspace_id, destination, message, tool_name FROM public.egress_sink"
        " WHERE request_id = %s",
        (request_id,),
    ) == [(DEFAULT_WORKSPACE, "synthetic_sink", "synthetic probe", WORKSPACE_HONEYPOT)]
    assert live_rows("SELECT count(*) FROM public.tasks") == [(tasks_before,)]
    assert_live_executed_once(message, jti, WORKSPACE_HONEYPOT, request_id)


@NEEDS_DB
def test_honeypot_sink_row_carries_jti_and_requester() -> None:
    """I4 (spec §9.1, migration 0012): the row names the verified jti and
    requester, here a non-default persona."""
    request_id, jti = new_id("req-sink"), new_id("jti-sink")
    with live_client() as client:
        ok_body(call_honeypot(client, persona="priya", request_id=request_id, jti=jti))
    assert live_rows(
        "SELECT workspace_id, jti, requester_member_id, created_at IS NOT NULL"
        " FROM public.egress_sink WHERE request_id = %s",
        (request_id,),
    ) == [(DEFAULT_WORKSPACE, jti, "m-priya", True)]


def test_honeypot_sink_row_from_verified_ctx_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """I4 without a database: every correlation value comes from the `_ctx`."""
    ledger, pool = FakeLedger(), TablePool()
    workspace, request_id, jti = new_id("ws-offline"), new_id("req-sink"), new_id("jti-sink")
    with table_pool_client(monkeypatch, ledger, pool) as client:
        message = call_honeypot(
            client,
            honeypot_args(message="offline probe"),
            persona="priya",
            workspace_id=workspace,
            request_id=request_id,
            jti=jti,
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
def test_honeypot_never_uses_network(monkeypatch: pytest.MonkeyPatch) -> None:
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
    with live_client() as client:
        message = call_honeypot(client)
    assert message["result"]["isError"] is False


# --- create_followup_task: offline -----------------------------------------------


def test_followup_uses_verified_ctx_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspace and requester come from the `_ctx` (non-default persona and
    workspace), never from constants or arguments; the call is ledgered."""
    ledger, pool = FakeLedger(), TablePool()
    workspace, jti, key = new_id("ws-offline"), new_id("jti-task"), new_id("key")
    with (
        table_pool_client(monkeypatch, ledger, pool) as client,
        network_forbidden(monkeypatch) as attempts,
    ):
        message = call_followup(
            client,
            task_args(key, owner="priya", priority="low"),
            persona="priya",
            workspace_id=workspace,
            jti=jti,
        )
    assert attempts == []
    body = ok_body(message)
    [row] = pool.rows("tasks")
    assert body["task_id"] == str(row["id"])
    assert (row["workspace_id"], row["created_by_member_id"], row["status"]) == (
        workspace,
        "m-priya",
        "open",
    )
    assert row["idempotency_key"] == scoped_key(workspace, "m-priya", key)
    assert (row["owner"], row["priority"]) == ("priya", "low")
    assert pool.rows("egress_sink") == []
    assert_fake_executed_once(message, ledger, jti)


def test_followup_replay_returns_stored_fields_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """I1 without a database: the replay result comes from the stored row."""
    ledger, pool = FakeLedger(), TablePool()
    key = new_id("key")
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
    key = new_id("key")
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


def test_followup_absent_description_is_null_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """An omitted description is stored as NULL, not as an empty string."""
    ledger, pool = FakeLedger(), TablePool()
    arguments = task_args(new_id("key"))
    del arguments["description"]
    with table_pool_client(monkeypatch, ledger, pool) as client:
        ok_body(call_followup(client, arguments))
    [row] = pool.rows("tasks")
    assert row["description"] is None


def test_honeypot_tools_list_description_is_neutral() -> None:
    """tools/list must not advertise the honeypot as a decoy (the ArmorIQ
    registry and the planner read these descriptions)."""
    with dev_client() as client:
        tools = {
            tool["name"]: tool for tool in list_tools(client, "/workspace/mcp", WORKSPACE_TOKEN)
        }
    description = tools[WORKSPACE_HONEYPOT]["description"].lower()
    for giveaway in ("honeypot", "decoy", "trap", "fake", "synthetic", "never", "sink"):
        assert giveaway not in description, giveaway


def test_task_enum_bounds() -> None:
    with dev_client() as client:
        for arguments in (
            {"title": "T", "owner": "mallory", "priority": "low", "idempotency_key": "k1"},
            {"title": "T", "owner": "sam", "priority": "urgent", "idempotency_key": "k2"},
            {"title": "", "owner": "sam", "priority": "low", "idempotency_key": "k3"},
        ):
            message = call_followup(client, arguments)
            assert result_text(message)["code"] == "invalid_arguments"


# --- create_followup_task: live --------------------------------------------------


@NEEDS_DB
def test_task_idempotency() -> None:
    key = new_id("key")
    jtis = [new_id("jti-task"), new_id("jti-task")]
    request_ids = [new_id("req-task"), new_id("req-task")]
    with live_client() as client:
        first = call_followup(client, task_args(key), jti=jtis[0], request_id=request_ids[0])
        second = call_followup(client, task_args(key), jti=jtis[1], request_id=request_ids[1])
    first_id = ok_body(first)["task_id"]
    assert ok_body(second)["task_id"] == first_id
    for message, jti, request_id in zip((first, second), jtis, request_ids, strict=True):
        assert_live_executed_once(message, jti, FOLLOWUP, request_id)
    # Rows are found by the returned task_id: the stored key is scoped (B1).
    assert live_rows(
        "SELECT count(*) FROM public.tasks WHERE idempotency_key ="
        " (SELECT idempotency_key FROM public.tasks WHERE id = %s)",
        (int(first_id),),
    ) == [(1,)]
    assert live_rows("SELECT count(*) FROM public.tasks WHERE idempotency_key = %s", (key,)) == [
        (0,)
    ], "the raw caller key is never stored"


@NEEDS_DB
def test_task_requester_from_ctx() -> None:
    # Local DB fixture: member rows are seeded by the operator (migration role)
    # alongside the corpus; live tool tests assume them like they assume sources.
    key = new_id("key")
    with live_client() as client:
        message = call_followup(
            client,
            {"title": "T", "owner": "priya", "priority": "low", "idempotency_key": key},
            persona="priya",
        )
    task_id = int(ok_body(message)["task_id"])
    assert live_rows(
        "SELECT created_by_member_id, workspace_id, idempotency_key FROM public.tasks"
        " WHERE id = %s",
        (task_id,),
    ) == [("m-priya", DEFAULT_WORKSPACE, scoped_key(DEFAULT_WORKSPACE, "m-priya", key))]


@NEEDS_DB
def test_task_absent_description_is_null() -> None:
    arguments = task_args(new_id("key"))
    del arguments["description"]
    with live_client() as client:
        task_id = int(ok_body(call_followup(client, arguments))["task_id"])
    rows = live_rows("SELECT description IS NULL FROM public.tasks WHERE id = %s", (task_id,))
    assert rows == [(True,)]


@NEEDS_DB
def test_task_idempotency_is_scoped_per_requester() -> None:
    """B1: the same caller key from two requesters creates two tasks; neither
    requester ever receives the other's task id or title."""
    key = new_id("key")
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
    rows = live_rows(
        "SELECT id, created_by_member_id, title, idempotency_key FROM public.tasks"
        " WHERE id = ANY(%s) ORDER BY id",
        ([int(sam["task_id"]), int(priya_body["task_id"])],),
    )
    assert [(row[1], row[2]) for row in rows] == [("m-sam", sam_title), ("m-priya", priya_title)]
    assert [row[3] for row in rows] == [
        scoped_key(DEFAULT_WORKSPACE, "m-sam", key),
        scoped_key(DEFAULT_WORKSPACE, "m-priya", key),
    ]


@NEEDS_DB
def test_task_replay_returns_stored_fields() -> None:
    """I1: a replay with different owner/priority/title reports the stored task."""
    key = new_id("key")
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
