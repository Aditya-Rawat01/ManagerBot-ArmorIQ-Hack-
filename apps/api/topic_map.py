"""Team-first tier selection over manager-approved topic rules (P3-02, PRD §5.2).

The topic map proposes the allowed planning scope; it NEVER grants access.
ArmorIQ policy and the tool-server tier ceiling still decide every call
(R-26). A confidential step additionally needs a matching rule or a trusted
escalation confirmation (checked in :mod:`apps.api.planner`).

Rules live in ``topic_access_rules`` (backend role: SELECT/INSERT/UPDATE).
``match`` degrades to team-only when the database is absent or unreadable —
fail closed for tier selection, never fail open. ``list_rules`` and
``upsert_rule`` are Maya-only (P4-04 editor); the manager role is enforced
inside, so a missing caller check cannot leak or mutate rules.

Interface note: SHARED_INTERFACES §12 sketches ``match(message)``. Rules are
stored per workspace in the database, so the implemented signature is
``match(db, workspace_id, message)`` — same core, explicit dependencies.
"""

import re
from dataclasses import dataclass

from apps.api.core.db import AuditDatabase
from services.audit.errors import AuditNotAuthorizedError, AuditStoreError
from services.contracts import AppRole, AuthenticatedUser, DataTier

MAX_TOPIC_CHARS = 200


@dataclass(frozen=True)
class TopicMatch:
    """The most specific rule whose topic the message covers."""

    rule_id: str
    topic: str
    max_tier: DataTier


@dataclass(frozen=True)
class TopicRule:
    """One manager-approved row (P4-04 editor view)."""

    id: str
    workspace_id: str
    topic: str
    max_tier: DataTier


def normalize_topic(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace (deterministic)."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _covers(normalized_message: str, rule_topic: str) -> bool:
    if not rule_topic:
        return False
    if rule_topic in normalized_message:
        return True
    message_tokens = set(normalized_message.split())
    significant = [token for token in rule_topic.split() if len(token) >= 4]
    return bool(significant) and all(token in message_tokens for token in significant)


def _row_to_match(row: object) -> TopicMatch:
    rule_id, topic, max_tier = row  # type: ignore[misc]
    try:
        tier = DataTier(str(max_tier))
    except ValueError:
        raise AuditStoreError(f"topic rule {rule_id!r} carries unknown tier") from None
    return TopicMatch(rule_id=str(rule_id), topic=str(topic), max_tier=tier)


def match(db: AuditDatabase | None, workspace_id: str, message: str) -> TopicMatch | None:
    """Most specific matching rule, or None (team-only planning).

    ``db=None`` and any database error both yield None: without readable
    rules the planner stays at team tier. Callers must not treat None as a
    denial — it only means "no elevated scope proposed".
    """
    normalized = normalize_topic(message)
    if db is None or not normalized:
        return None
    try:
        with db.tx() as cursor:
            cursor.execute(
                "SELECT id, topic, max_tier FROM topic_access_rules "
                "WHERE workspace_id = %s ORDER BY id",
                [workspace_id],
            )
            rows = cursor.fetchall()
    except Exception:
        return None
    candidates = []
    for row in rows:
        try:
            candidate = _row_to_match(row)
        except AuditStoreError:
            continue
        if _covers(normalized, normalize_topic(candidate.topic)):
            candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item.topic))


def _require_manager(actor: AuthenticatedUser) -> None:
    if actor.app_role is not AppRole.MANAGER:
        raise AuditNotAuthorizedError("topic-rule reads and edits require the Maya role")


def list_rules(db: AuditDatabase, workspace_id: str, actor: AuthenticatedUser) -> list[TopicRule]:
    """All rules for a workspace, oldest first (Maya-only)."""
    _require_manager(actor)
    with db.tx() as cursor:
        cursor.execute(
            "SELECT id, workspace_id, topic, max_tier FROM topic_access_rules "
            "WHERE workspace_id = %s ORDER BY id",
            [workspace_id],
        )
        rules = []
        for row in cursor.fetchall():
            rule_id, workspace, topic, max_tier = row  # type: ignore[misc]
            try:
                tier = DataTier(str(max_tier))
            except ValueError:
                raise AuditStoreError(f"topic rule {rule_id!r} carries unknown tier") from None
            rules.append(
                TopicRule(
                    id=str(rule_id),
                    workspace_id=str(workspace),
                    topic=str(topic),
                    max_tier=tier,
                )
            )
        return rules


def upsert_rule(
    db: AuditDatabase,
    *,
    actor: AuthenticatedUser,
    workspace_id: str,
    topic: str,
    max_tier: DataTier,
) -> str:
    """Create or replace a workspace rule by topic (Maya-only). Returns the rule id."""
    _require_manager(actor)
    normalized = normalize_topic(topic)
    if not normalized or len(normalized) > MAX_TOPIC_CHARS:
        raise AuditStoreError("topic must normalize to 1-200 characters")
    with db.tx() as cursor:
        cursor.execute(
            "INSERT INTO topic_access_rules (workspace_id, topic, max_tier) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (workspace_id, topic) DO UPDATE SET "
            "max_tier = EXCLUDED.max_tier, updated_at = now() RETURNING id",
            [workspace_id, normalized, max_tier.value],
        )
        row = cursor.fetchone()
        if row is None:
            raise AuditStoreError("topic rule upsert returned no id")
        return str(row[0])


__all__ = [
    "TopicMatch",
    "TopicRule",
    "list_rules",
    "match",
    "normalize_topic",
    "upsert_rule",
]
