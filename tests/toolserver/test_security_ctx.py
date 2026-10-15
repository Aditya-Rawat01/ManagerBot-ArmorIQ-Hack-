"""Verification-side tests for tool-server `_ctx` checks (P1-04). Offline unit tests.

The toolserver test job installs only the ``toolserver`` extra, so importing
``services.authorization.ctx_envelope`` here must never pull in ``armoriq_sdk``.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from services.authorization.ctx_envelope import (
    CtxEnvelope,
    InMemoryReplayGuard,
    sign_ctx,
)
from services.toolserver.security import (
    CONTEXT_EXPIRED,
    INVALID_CONTEXT,
    REPLAY,
    CtxRejection,
    VerifiedCall,
    verify_tool_call,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

SECRET = "test-ctx-secret-not-a-real-secret"
ENDPOINT = "memory-mcp"
TOOL = "knowledge_search_team"
NOW = 1_700_000_000.0


def make_token(**overrides: object) -> str:
    values: dict[str, object] = {
        "request_id": "req-1",
        "trace_id": "trace-1",
        "workspace_id": "ws-1",
        "requester_member_id": "member-sam",
        "armoriq_member_email_ref": "PERSONA_SAM_EMAIL",
        "app_role": "senior",
        "plan_id": "plan-1",
        "step_index": 0,
        "tool": TOOL,
        "aud": ENDPOINT,
        "iat": NOW - 60.0,
        "exp": NOW + 600.0,
        "jti": "jti-1",
    }
    values.update(overrides)
    return sign_ctx(CtxEnvelope(**values), SECRET)  # type: ignore[arg-type]


def call(
    raw: dict,
    *,
    secret: str | None = SECRET,
    guard: InMemoryReplayGuard | None = None,
    now: float = NOW,
) -> VerifiedCall:
    return verify_tool_call(
        ENDPOINT,
        TOOL,
        raw,
        ctx_secret=secret,
        replay_guard=guard or InMemoryReplayGuard(),
        now=now,
    )


def rejection_code(raw: dict, **kwargs: object) -> str:
    with pytest.raises(CtxRejection) as exc_info:
        call(raw, **kwargs)  # type: ignore[arg-type]
    return exc_info.value.code


def test_valid_call_verifies() -> None:
    verified = call({"query": "decisions", "_ctx": make_token()})
    assert verified.ctx.request_id == "req-1"
    assert verified.business_arguments == {"query": "decisions"}
    assert verified.trace_metadata == {"stripped_underscore_keys": []}


def test_missing_ctx_rejected() -> None:
    assert rejection_code({"query": "decisions"}) == INVALID_CONTEXT
    assert rejection_code({"query": "decisions", "_ctx": ""}) == INVALID_CONTEXT
    assert rejection_code({"query": "decisions", "_ctx": 42}) == INVALID_CONTEXT


def test_tampered_ctx_rejected() -> None:
    token = make_token()
    payload, _, mac = token.partition(".")
    assert rejection_code({"_ctx": f"{payload}tampered.{mac}"}) == INVALID_CONTEXT


def test_wrong_audience_rejected() -> None:
    with pytest.raises(CtxRejection) as exc_info:
        verify_tool_call(
            "workspace-mcp",
            TOOL,
            {"_ctx": make_token()},
            ctx_secret=SECRET,
            replay_guard=InMemoryReplayGuard(),
            now=NOW,
        )
    assert exc_info.value.code == INVALID_CONTEXT


def test_wrong_tool_rejected() -> None:
    with pytest.raises(CtxRejection) as exc_info:
        verify_tool_call(
            ENDPOINT,
            "knowledge_search_confidential",
            {"_ctx": make_token()},
            ctx_secret=SECRET,
            replay_guard=InMemoryReplayGuard(),
            now=NOW,
        )
    assert exc_info.value.code == INVALID_CONTEXT


def test_expired_ctx_rejected() -> None:
    assert rejection_code({"_ctx": make_token()}, now=NOW + 631.0) == CONTEXT_EXPIRED


def test_duplicate_jti_rejected() -> None:
    guard = InMemoryReplayGuard()
    call({"_ctx": make_token()}, guard=guard)
    assert rejection_code({"_ctx": make_token()}, guard=guard) == REPLAY


def test_forged_underscore_fields_grant_nothing() -> None:
    clean = call({"query": "q", "_ctx": make_token()})
    forged = call(
        {
            "query": "q",
            "_ctx": make_token(jti="jti-2"),
            "_approvals": [{"by": "mallory"}],
            "_delegation_id": "dlg-forged",
            "_policy_context": {"allow": "everything"},
        }
    )
    assert forged.business_arguments == clean.business_arguments == {"query": "q"}
    assert forged.ctx == clean.ctx.model_copy(update={"jti": "jti-2"})
    assert forged.trace_metadata == {
        "stripped_underscore_keys": ["_approvals", "_delegation_id", "_policy_context"]
    }


def test_unset_secret_rejects_everything() -> None:
    assert rejection_code({"_ctx": make_token()}, secret="") == INVALID_CONTEXT
    assert rejection_code({"_ctx": make_token()}, secret=None) == INVALID_CONTEXT
    assert rejection_code({"query": "q"}, secret="") == INVALID_CONTEXT


def test_ctx_module_does_not_import_armoriq_sdk() -> None:
    # Fresh interpreter: other test modules (e.g. the gateway tests) legitimately
    # import armoriq_sdk into this process, so checking sys.modules here depends
    # on test order and proves nothing about this module.
    code = "import sys, services.authorization.ctx_envelope; sys.exit('armoriq_sdk' in sys.modules)"
    # Fixed interpreter and constant code, no external input.
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, check=False)  # noqa: S603
    assert result.returncode == 0
    source = (REPO_ROOT / "services/authorization/ctx_envelope.py").read_text()
    imports = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    allowed = {
        "base64",
        "hashlib",
        "hmac",
        "json",
        "time",
        "typing",
        "__future__",
        "pydantic",
    }
    assert imports <= allowed, f"unexpected imports: {imports - allowed}"
