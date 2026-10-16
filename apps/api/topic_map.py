"""Team-first tier selection over manager-approved topic rules (P3-02, PRD §5.2).

The topic map proposes the allowed planning scope; it NEVER grants access.
ArmorIQ policy and the tool-server tier ceiling still decide every call
(R-26). A confidential step additionally needs a matching rule or a trusted
escalation confirmation (checked in :mod:`apps.api.planner`).

Matching is on whole tokens: text is lowercased, split on anything that is
not ``a-z0-9``, and a rule covers a text only when EVERY token of the rule's
topic appears as a whole token of the text. There is no substring match and
no length filter, so ``ai`` never matches ``maintenance`` and ``q4 pricing``
never matches ``q3 pricing``. The planner applies :func:`covers` twice: to
the user's message (here, :func:`match`) and to the elevated search step's
``query`` (so stuffing a topic's words into the message does not widen an
unrelated search).

Rules live in ``topic_access_rules`` (backend role). ``match`` needs the
backend database (no ``None`` fallback); a database error yields ``None``
(team-only planning, never a grant) and is logged with the exception class.
``list_rules`` and ``upsert_rule`` are Maya-only (P4-04 editor; the manager
role is enforced inside, so a missing caller check cannot leak or mutate
rules). ``upsert_rule`` refuses topics too generic to scope anything (no
word of 3+ characters that is not a stopword) and records the acting member
in ``created_by_member_id``.

Interface (SHARED_INTERFACES §12, accepted on issue #20): rules are stored
per workspace, so the signature is ``match(db, workspace_id, message)``.
"""

import logging
import re
from dataclasses import dataclass

from apps.api.core.db import AuditDatabase
from services.audit.errors import AuditNotAuthorizedError, AuditStoreError
from services.contracts import AppRole, AuthenticatedUser, DataTier

logger = logging.getLogger(__name__)

MAX_TOPIC_CHARS = 200
MAX_TOPIC_TOKENS = 12
MIN_SIGNIFICANT_TOKEN_CHARS = 3

# Words that never make a topic specific on their own. A rule needs at least
# one token outside this set with MIN_SIGNIFICANT_TOKEN_CHARS or more.
STOPWORDS: frozenset[str] = frozenset(
    {
        "about",
        "all",
        "and",
        "any",
        "are",
        "but",
        "can",
        "did",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "into",
        "its",
        "not",
        "our",
        "out",
        "the",
        "their",
        "them",
        "then",
        "they",
        "this",
        "that",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    }
)

_TOKEN = re.compile(r"[a-z0-9]+")


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


def topic_tokens(text: str) -> tuple[str, ...]:
    """Lowercase ``a-z0-9`` tokens of ``text``, in order (deterministic)."""
    return tuple(_TOKEN.findall(text.lower()))


def normalize_topic(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace (deterministic)."""
    return " ".join(topic_tokens(text))


def covers(text: str, topic: str) -> bool:
    """True when every token of ``topic`` appears as a whole token of ``text``."""
    wanted = topic_tokens(topic)
    return bool(wanted) and set(wanted) <= set(topic_tokens(text))


def _is_specific(tokens: tuple[str, ...]) -> bool:
    return any(
        len(token) >= MIN_SIGNIFICANT_TOKEN_CHARS and token not in STOPWORDS for token in tokens
    )


def validate_topic(topic: str) -> str:
    """Normalized topic, or :class:`AuditStoreError` when unusable as a rule.

    Refused: empty, longer than ``MAX_TOPIC_CHARS`` or ``MAX_TOPIC_TOKENS``,
    or with no token of 3+ characters that is not a stopword (``ai``,
    ``the``, ``q4``). Short tokens are kept next to a specific one
    (``q4 pricing``): every token must still match.
    """
    tokens = topic_tokens(topic)
    normalized = " ".join(tokens)
    if not normalized or len(normalized) > MAX_TOPIC_CHARS:
        raise AuditStoreError(f"topic must normalize to 1-{MAX_TOPIC_CHARS} characters")
    if len(tokens) > MAX_TOPIC_TOKENS:
        raise AuditStoreError(f"topic must have at most {MAX_TOPIC_TOKENS} words")
    if not _is_specific(tokens):
        raise AuditStoreError(
            "topic needs at least one specific word of 3+ characters that is not a stopword"
        )
    return normalized


def _row_to_match(row: object) -> TopicMatch:
    rule_id, topic, max_tier = row  # type: ignore[misc]
    try:
        tier = DataTier(str(max_tier))
    except ValueError:
        raise AuditStoreError(f"topic rule {rule_id!r} carries unknown tier") from None
    return TopicMatch(rule_id=str(rule_id), topic=str(topic), max_tier=tier)


def match(db: AuditDatabase, workspace_id: str, message: str) -> TopicMatch | None:
    """Most specific matching rule, or None (team-only planning).

    Blocking (psycopg): async callers run it via ``asyncio.to_thread``. A
    database error yields None and a warning naming the exception class only.
    Callers must not treat None as a denial; it only means "no elevated scope
    proposed". Stored rules that fail :func:`validate_topic` (for example a
    row written before that check existed) are skipped, never matched.
    ``db=None`` is a wiring error and raises (no silent team-only fallback).
    """
    if db is None:
        raise TypeError("topic_map.match needs the backend database")
    message_tokens = set(topic_tokens(message))
    if not message_tokens:
        return None
    try:
        with db.tx() as cursor:
            cursor.execute(
                "SELECT id, topic, max_tier FROM topic_access_rules "
                "WHERE workspace_id = %s ORDER BY id",
                [workspace_id],
            )
            rows = cursor.fetchall()
    except Exception as exc:
        logger.warning(
            "topic rule lookup failed; planning stays team-only error=%s",
            type(exc).__name__,
        )
        return None
    best: TopicMatch | None = None
    best_key: tuple[int, int] = (0, 0)
    for row in rows:
        try:
            candidate = _row_to_match(row)
        except AuditStoreError:
            logger.warning("topic rule skipped: unknown tier")
            continue
        tokens = topic_tokens(candidate.topic)
        if not tokens or not _is_specific(tokens) or not set(tokens) <= message_tokens:
            continue
        key = (len(set(tokens)), len(candidate.topic))
        if best is None or key > best_key:
            best, best_key = candidate, key
    return best


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
    """Create or replace a workspace rule by topic (Maya-only). Returns the rule id.

    ``created_by_member_id`` is the actor on insert and is kept on update.
    The caller (P4-04) passes the actor's own workspace.
    """
    _require_manager(actor)
    normalized = validate_topic(topic)
    with db.tx() as cursor:
        cursor.execute(
            "INSERT INTO topic_access_rules (workspace_id, topic, max_tier, created_by_member_id) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (workspace_id, topic) DO UPDATE SET "
            "max_tier = EXCLUDED.max_tier, updated_at = now() RETURNING id",
            [workspace_id, normalized, max_tier.value, actor.member_id],
        )
        row = cursor.fetchone()
        if row is None:
            raise AuditStoreError("topic rule upsert returned no id")
        return str(row[0])


__all__ = [
    "MAX_TOPIC_CHARS",
    "MAX_TOPIC_TOKENS",
    "STOPWORDS",
    "TopicMatch",
    "TopicRule",
    "covers",
    "list_rules",
    "match",
    "normalize_topic",
    "topic_tokens",
    "upsert_rule",
    "validate_topic",
]
