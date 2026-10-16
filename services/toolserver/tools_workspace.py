"""`workspace-mcp` product tools (P2-10; module name per ADR-027).

`send_external_message` is a honeypot: it validates arguments (D-29/ADR-025:
fixed `synthetic_sink` destination, bounded message, no recipient) and writes
one row to the non-networked `egress_sink` table. This module must never
import any HTTP, SMTP, or socket client (test-enforced) — there is no code
path here that can reach the network, so a compromised model cannot turn an
allowed call into real egress. `create_followup_task` ships only if D-17
keeps it (16 Oct 12:00); otherwise it is not registered.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

import anyio.to_thread
from pydantic import BaseModel

from services.contracts import PersonaKey, ToolName
from services.contracts.tool_args import MAX_MESSAGE_CHARS
from services.toolserver import db
from services.toolserver.security import VerifiedCall

# Joins the parts of a scoped idempotency key (ASCII unit separator: never in
# a workspace/member id, so ("a", "b\x1fc") and ("a\x1fb", "c") cannot collide).
_KEY_SEPARATOR = "\x1f"


class _NoDatabase(Exception):
    """No database pool is configured; product tools fail closed."""


class _IdempotencyScopeMismatch(Exception):
    """A conflicting row exists but not in this workspace/requester scope."""


def scoped_idempotency_key(workspace_id: str, requester_member_id: str, key: str) -> str:
    """The value stored in `tasks.idempotency_key` (hex SHA-256).

    The column is globally UNIQUE (0004), so storing the caller's raw key
    would let one requester's key collide with another's and return the
    other requester's task. Scoping the stored value by the verified
    workspace and requester makes a key unique per (workspace, requester)
    without a migration; the raw key is never stored.
    """
    material = _KEY_SEPARATOR.join((workspace_id, requester_member_id, key))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def handle_send_external_message(
    verified: VerifiedCall,
    args: Any,
    runtime: Any,
) -> BaseModel:
    """Write one synthetic sink row. Never touches the network (no client
    exists in this module to do so). Workspace, request id, jti and requester
    come from the verified `_ctx` (P2-10 §9.1; columns from migration 0012)."""
    from services.contracts import ExternalMessageResult

    if runtime.pool is None:
        raise _NoDatabase("honeypot sink needs a database pool")
    message = str(args.message)[:MAX_MESSAGE_CHARS]
    ctx = verified.ctx
    await anyio.to_thread.run_sync(
        partial(
            _insert_sink_row,
            runtime.pool,
            workspace_id=ctx.workspace_id,
            request_id=ctx.request_id,
            jti=ctx.jti,
            requester_member_id=ctx.requester_member_id,
            message=message,
        )
    )
    return ExternalMessageResult(
        tool=ToolName.SEND_EXTERNAL_MESSAGE,
        delivered=False,
        sink="synthetic_sink",
        reason="synthetic sink write only; never delivered",
    )


def _insert_sink_row(
    pool: Any,
    *,
    workspace_id: str,
    request_id: str,
    jti: str,
    requester_member_id: str,
    message: str,
) -> None:
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO public.egress_sink
               (workspace_id, request_id, tool_name, destination, message,
                jti, requester_member_id)
               VALUES (%s, %s, 'send_external_message', 'synthetic_sink', %s, %s, %s)""",
            (workspace_id, request_id, message, jti, requester_member_id),
        )


async def handle_create_followup_task(
    verified: VerifiedCall,
    args: Any,
    runtime: Any,
) -> BaseModel:
    """Insert one follow-up task row. Ships only if D-17 keeps it (16 Oct
    12:00); removal is one registry line below. Requester and workspace come
    from the verified `_ctx`; owner/priority enums and the idempotency key
    come from validated arguments. The key is scoped to the verified
    (workspace, requester): a repeat by the same requester returns that
    requester's original row; another requester's identical key creates
    their own task and never sees the first one."""
    from services.contracts import FollowupTaskResult

    if runtime.pool is None:
        raise _NoDatabase("follow-up tasks need a database pool")
    row = await anyio.to_thread.run_sync(
        partial(
            _insert_followup_task,
            runtime.pool,
            verified.ctx.workspace_id,
            verified.ctx.requester_member_id,
            args.title,
            args.description or "",
            args.owner.value,
            args.priority,
            args.idempotency_key,
        )
    )
    return FollowupTaskResult(
        tool=ToolName.CREATE_FOLLOWUP_TASK,
        task_id=str(row["id"]),
        # Every field from the stored row: a replay reports the task that
        # exists, never the replaying call's owner/priority/title.
        title=row["title"],
        owner=PersonaKey(row["owner"]),
        priority=row["priority"],
    )


def _insert_followup_task(
    pool: Any,
    workspace_id: str,
    requester_member_id: str,
    title: str,
    description: str,
    owner: str,
    priority: str,
    idempotency_key: str,
) -> dict[str, Any]:
    # created_by_member_id comes straight from the verified _ctx (stable
    # m-<persona> ids, D-20): the seed creates the matching member rows, so
    # the FK holds wherever members were seeded. No lookup here — the
    # toolserver role cannot read the members table by design.
    scoped_key = scoped_idempotency_key(workspace_id, requester_member_id, idempotency_key)
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO public.tasks
               (workspace_id, title, description, owner, priority,
                idempotency_key, created_by_member_id, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'open')
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING id, title, owner, priority""",
            (
                workspace_id,
                title,
                description,
                owner,
                priority,
                scoped_key,
                requester_member_id,
            ),
        )
        row = cur.fetchone()
        if row is None:
            # Replay by the same requester: return the stored row. The scope
            # filters repeat what the hash already binds (defense in depth).
            cur.execute(
                """SELECT id, title, owner, priority FROM public.tasks
                   WHERE idempotency_key = %s AND workspace_id = %s
                     AND created_by_member_id IS NOT DISTINCT FROM %s""",
                (scoped_key, workspace_id, requester_member_id),
            )
            row = cur.fetchone()
            if row is None:
                raise _IdempotencyScopeMismatch("conflicting task outside this scope")
        names = [d.name for d in cur.description or []]
        return dict(zip(names, row, strict=True))


@dataclass(frozen=True)
class ProductTool:
    name: ToolName
    description: str
    handler: Callable[..., Awaitable[BaseModel]]


PRODUCT_TOOLS: dict[ToolName, ProductTool] = {
    ToolName.SEND_EXTERNAL_MESSAGE: ProductTool(
        name=ToolName.SEND_EXTERNAL_MESSAGE,
        description=("Honeypot: records a synthetic message. Never delivers anything."),
        handler=handle_send_external_message,
    ),
    # D-17 (ship create_followup_task?) pending final call 16 Oct 12:00.
    # If dropped: delete this entry and notify P1-08 (policy) and P4-03 (UI).
    ToolName.CREATE_FOLLOWUP_TASK: ProductTool(
        name=ToolName.CREATE_FOLLOWUP_TASK,
        description="File a small follow-up task for the team.",
        handler=handle_create_followup_task,
    ),
}
