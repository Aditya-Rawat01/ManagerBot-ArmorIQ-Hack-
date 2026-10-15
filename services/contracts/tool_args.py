"""Strict per-tool argument and result contracts (P2-02, PRD v2 §6-§7, Arch §6).

One argument model and one result model per :class:`ToolName`. The planner
(P3-02) and the MCP dispatcher (P2-06) validate with :func:`validate_arguments`;
the responder (P3-03) parses with the result models. Search results reuse
:data:`KnowledgeSearchResult`; source results compose the existing
:data:`SourceCitation`.

Conventions (same as :mod:`services.contracts.models`): every model is
immutable and forbids unknown keys. No argument model carries tier, identity,
workspace, recipient, permission, or security-context fields, and no field is
``_``-prefixed or named in :data:`FORBIDDEN_ARGUMENT_KEYS` — those are chosen
by trusted code only.

PROPOSED values recorded here for later ICRs (no live evidence yet):

- ``limit`` cap 50 (search and read tools); ``excerpt``/``snippet`` 2000 chars;
  note ``content`` 2000 chars; titles 200 (note) / 120 (task); ``description``
  1000 chars; supersession ``reason`` / honeypot ``message`` 500 chars;
  record/task ``id`` and ``idempotency_key`` 128 chars.
- ``source_ref``: opaque source reference, shape provisional pending D-11.
- ``view`` names (``approved_company_data_<tier>``): placeholders pending
  P2-08, which fixes them by ICR.
- ``create_followup_task`` ``owner`` (:class:`PersonaKey`) and ``priority``
  (``low``/``medium``/``high``): PROPOSED pending D-17/ADR-024.
- ``send_external_message`` follows resolved ADR-025 (D-29): fixed
  ``synthetic_sink`` destination plus a bounded message. Never real egress.

Validation errors never echo caller-supplied values: :func:`validate_arguments`
re-raises sanitized :class:`ValidationError`s that name the failing field
(``loc``) and constraint but redact ``input``.
"""

from typing import Literal, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

from .enums import DataTier, PersonaKey, ReviewStatus, ToolName
from .models import ContractModel, KnowledgeSearchResult, SourceCitation
from .tools import TOOL_CATALOG

MAX_QUERY_CHARS = 500
MAX_RESULT_LIMIT = 50
MAX_EXCERPT_CHARS = 2000
MAX_NOTE_CHARS = 2000
MAX_TITLE_CHARS = 200
MAX_TASK_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 1000
MAX_REASON_CHARS = 500
MAX_MESSAGE_CHARS = 500
MAX_REF_CHARS = 128
MAX_VIEW_ROWS = 50


def _result_must_match(tool: ToolName, actual_tool: ToolName, tier: DataTier) -> None:
    """Result envelope guard: tool echo must match, tier must equal the catalog tier."""
    if actual_tool is not tool:
        raise ValueError(f"result tool must be {tool.value}")
    if TOOL_CATALOG[tool].tier is not tier:
        raise ValueError("result tier must equal the tier bound to the tool name")


# ---------------------------------------------------------------------------
# Search tools (tier fixed by the tool name; query + limit only)
# ---------------------------------------------------------------------------


class _SearchArgsBase(ContractModel):
    """Shared shape; one concrete subclass per tool (see below)."""

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    limit: int = Field(ge=1, le=MAX_RESULT_LIMIT)


class KnowledgeSearchTeamArgs(_SearchArgsBase):
    """Arguments for ``knowledge_search_team``."""


class KnowledgeSearchConfidentialArgs(_SearchArgsBase):
    """Arguments for ``knowledge_search_confidential``."""


class KnowledgeSearchRestrictedArgs(_SearchArgsBase):
    """Arguments for ``knowledge_search_restricted``."""


# ---------------------------------------------------------------------------
# Get-source tools (opaque reference in, bounded record out; D-11 provisional)
# ---------------------------------------------------------------------------


class _GetSourceArgsBase(ContractModel):
    """Shared shape; one concrete subclass per tool (see below).

    PROPOSED pending D-11: ``source_ref`` is an opaque server-issued
    reference. Its exact representation is revised by ICR once decided.
    """

    source_ref: str = Field(min_length=1, max_length=MAX_REF_CHARS)


class KnowledgeGetSourceTeamArgs(_GetSourceArgsBase):
    """Arguments for ``knowledge_get_source_team``."""


class KnowledgeGetSourceConfidentialArgs(_GetSourceArgsBase):
    """Arguments for ``knowledge_get_source_confidential``."""


class KnowledgeGetSourceRestrictedArgs(_GetSourceArgsBase):
    """Arguments for ``knowledge_get_source_restricted``."""


class _GetSourceResultBase(ContractModel):
    """Bounded source record. ``tier`` is set by the server, never the caller."""

    tool: ToolName
    tier: DataTier
    source: SourceCitation
    excerpt: str = Field(max_length=MAX_EXCERPT_CHARS)

    @model_validator(mode="after")
    def _check_source_tier(self) -> Self:
        # Same rule as KnowledgeSearchResult hits: a record from another tier
        # must never travel inside this tool's result envelope.
        if self.source.tier is not self.tier:
            raise ValueError("source tier must equal the result tier")
        return self


class KnowledgeGetSourceTeamResult(_GetSourceResultBase):
    """Result of ``knowledge_get_source_team``."""

    @model_validator(mode="after")
    def _check_envelope(self) -> Self:
        _result_must_match(ToolName.KNOWLEDGE_GET_SOURCE_TEAM, self.tool, self.tier)
        return self


class KnowledgeGetSourceConfidentialResult(_GetSourceResultBase):
    """Result of ``knowledge_get_source_confidential``."""

    @model_validator(mode="after")
    def _check_envelope(self) -> Self:
        _result_must_match(ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL, self.tool, self.tier)
        return self


class KnowledgeGetSourceRestrictedResult(_GetSourceResultBase):
    """Result of ``knowledge_get_source_restricted``."""

    @model_validator(mode="after")
    def _check_envelope(self) -> Self:
        _result_must_match(ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED, self.tool, self.tier)
        return self


# ---------------------------------------------------------------------------
# Read-source tools (allowlisted view + typed filters; views P2-08 provisional)
# ---------------------------------------------------------------------------

TeamView = Literal["approved_company_data_team"]
ConfidentialView = Literal["approved_company_data_confidential"]
RestrictedView = Literal["approved_company_data_restricted"]


class _ReadSourceArgsBase(ContractModel):
    """Shared shape; one concrete subclass per tool (see below).

    No table names, SQL, columns, sort expressions, or tier selectors: the
    view literal fixes the readable view and trusted code fixes the filter.
    """

    limit: int = Field(ge=1, le=MAX_RESULT_LIMIT)
    author: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)
    include_superseded: bool = False


class ReadSourceTeamArgs(_ReadSourceArgsBase):
    """Arguments for ``read_source_team``."""

    view: TeamView


class ReadSourceConfidentialArgs(_ReadSourceArgsBase):
    """Arguments for ``read_source_confidential``."""

    view: ConfidentialView


class ReadSourceRestrictedArgs(_ReadSourceArgsBase):
    """Arguments for ``read_source_restricted``."""

    view: RestrictedView


class SourceRow(ContractModel):
    """One bounded row returned from an approved view."""

    source_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    snippet: str = Field(max_length=MAX_EXCERPT_CHARS)


class _ReadSourceResultBase(ContractModel):
    """Rows plus total count and truncation flag. ``view`` echoes the query."""

    tool: ToolName
    tier: DataTier
    rows: list[SourceRow] = Field(max_length=MAX_VIEW_ROWS)
    row_count: int = Field(ge=0)
    truncated: bool = False

    @model_validator(mode="after")
    def _check_row_count(self) -> Self:
        if self.row_count < len(self.rows):
            raise ValueError("row_count must cover at least the returned rows")
        return self


class ReadSourceTeamResult(_ReadSourceResultBase):
    """Result of ``read_source_team``."""

    view: TeamView

    @model_validator(mode="after")
    def _check_envelope(self) -> Self:
        _result_must_match(ToolName.READ_SOURCE_TEAM, self.tool, self.tier)
        return self


class ReadSourceConfidentialResult(_ReadSourceResultBase):
    """Result of ``read_source_confidential``."""

    view: ConfidentialView

    @model_validator(mode="after")
    def _check_envelope(self) -> Self:
        _result_must_match(ToolName.READ_SOURCE_CONFIDENTIAL, self.tool, self.tier)
        return self


class ReadSourceRestrictedResult(_ReadSourceResultBase):
    """Result of ``read_source_restricted``."""

    view: RestrictedView

    @model_validator(mode="after")
    def _check_envelope(self) -> Self:
        _result_must_match(ToolName.READ_SOURCE_RESTRICTED, self.tool, self.tier)
        return self


# ---------------------------------------------------------------------------
# Knowledge maintenance (provenance comes from trusted code, never arguments)
# ---------------------------------------------------------------------------


class KnowledgeSaveTeamNoteArgs(ContractModel):
    """Arguments for ``knowledge_save_team_note`` (PRD v2 §6.1, §8.4)."""

    content: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)
    content_kind: Literal["note", "summary"]
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)


class SaveTeamNoteResult(ContractModel):
    """A saved note is always a draft until Maya confirms it (PRD v2 §8.4)."""

    tool: ToolName
    record_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    version: int = Field(ge=1)
    review_status: Literal["draft"] = "draft"


class KnowledgeConfirmMayaDecisionArgs(ContractModel):
    """Arguments for ``knowledge_confirm_maya_decision``.

    Binds to the exact record version (``version`` ≥ 1, PRD v2 §8.4).
    """

    record_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    version: int = Field(ge=1)


class ConfirmMayaDecisionResult(ContractModel):
    """Maya's confirmation makes the exact version authoritative."""

    tool: ToolName
    record_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    version: int = Field(ge=1)
    review_status: Literal["authoritative"] = "authoritative"


class KnowledgeMarkSupersededArgs(ContractModel):
    """Arguments for ``knowledge_mark_superseded``."""

    record_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    version: int = Field(ge=1)
    superseded_by_record_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


class MarkSupersededResult(ContractModel):
    """Supersession record with provenance for the replacing record."""

    tool: ToolName
    record_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    version: int = Field(ge=1)
    review_status: ReviewStatus
    superseded_by_record_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)


# ---------------------------------------------------------------------------
# Workspace actions (follow-up task + honeypot; ADR-025, ADR-024)
# ---------------------------------------------------------------------------


class CreateFollowupTaskArgs(ContractModel):
    """Arguments for ``create_followup_task`` (Arch §6.2).

    PROPOSED pending D-17/ADR-024: ``owner``/``priority`` value sets. Kept
    even if the feature ships late or is dropped.
    """

    title: str = Field(min_length=1, max_length=MAX_TASK_TITLE_CHARS)
    description: str | None = Field(default=None, min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    owner: PersonaKey
    priority: Literal["low", "medium", "high"]
    idempotency_key: str = Field(min_length=1, max_length=MAX_REF_CHARS)


class FollowupTaskResult(ContractModel):
    """Created follow-up task reference."""

    tool: ToolName
    task_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    title: str = Field(min_length=1, max_length=MAX_TASK_TITLE_CHARS)
    owner: PersonaKey
    priority: Literal["low", "medium", "high"]


class SendExternalMessageArgs(ContractModel):
    """Arguments for the ``send_external_message`` honeypot (ADR-025).

    Fixed ``synthetic_sink`` destination plus a bounded synthetic message.
    No free-form recipient, email, URL, or destination exists, and no real
    external egress is ever performed.
    """

    destination: Literal["synthetic_sink"] = "synthetic_sink"
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ExternalMessageResult(ContractModel):
    """Honeypot outcome: never delivered, recorded only."""

    tool: ToolName
    delivered: Literal[False] = False
    sink: Literal["synthetic_sink"] = "synthetic_sink"
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


# ---------------------------------------------------------------------------
# Registry and validation entry point
# ---------------------------------------------------------------------------

TOOL_ARGUMENT_MODELS: dict[ToolName, type[BaseModel]] = {
    ToolName.KNOWLEDGE_SEARCH_TEAM: KnowledgeSearchTeamArgs,
    ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: KnowledgeSearchConfidentialArgs,
    ToolName.KNOWLEDGE_SEARCH_RESTRICTED: KnowledgeSearchRestrictedArgs,
    ToolName.KNOWLEDGE_GET_SOURCE_TEAM: KnowledgeGetSourceTeamArgs,
    ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL: KnowledgeGetSourceConfidentialArgs,
    ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED: KnowledgeGetSourceRestrictedArgs,
    ToolName.READ_SOURCE_TEAM: ReadSourceTeamArgs,
    ToolName.READ_SOURCE_CONFIDENTIAL: ReadSourceConfidentialArgs,
    ToolName.READ_SOURCE_RESTRICTED: ReadSourceRestrictedArgs,
    ToolName.KNOWLEDGE_SAVE_TEAM_NOTE: KnowledgeSaveTeamNoteArgs,
    ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION: KnowledgeConfirmMayaDecisionArgs,
    ToolName.KNOWLEDGE_MARK_SUPERSEDED: KnowledgeMarkSupersededArgs,
    ToolName.CREATE_FOLLOWUP_TASK: CreateFollowupTaskArgs,
    ToolName.SEND_EXTERNAL_MESSAGE: SendExternalMessageArgs,
}

TOOL_RESULT_MODELS: dict[ToolName, type[BaseModel]] = {
    ToolName.KNOWLEDGE_SEARCH_TEAM: KnowledgeSearchResult,
    ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: KnowledgeSearchResult,
    ToolName.KNOWLEDGE_SEARCH_RESTRICTED: KnowledgeSearchResult,
    ToolName.KNOWLEDGE_GET_SOURCE_TEAM: KnowledgeGetSourceTeamResult,
    ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL: KnowledgeGetSourceConfidentialResult,
    ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED: KnowledgeGetSourceRestrictedResult,
    ToolName.READ_SOURCE_TEAM: ReadSourceTeamResult,
    ToolName.READ_SOURCE_CONFIDENTIAL: ReadSourceConfidentialResult,
    ToolName.READ_SOURCE_RESTRICTED: ReadSourceRestrictedResult,
    ToolName.KNOWLEDGE_SAVE_TEAM_NOTE: SaveTeamNoteResult,
    ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION: ConfirmMayaDecisionResult,
    ToolName.KNOWLEDGE_MARK_SUPERSEDED: MarkSupersededResult,
    ToolName.CREATE_FOLLOWUP_TASK: FollowupTaskResult,
    ToolName.SEND_EXTERNAL_MESSAGE: ExternalMessageResult,
}

_REDACTED_INPUT = "[redacted]"


def _sanitized_error(exc: ValidationError, title: str) -> ValidationError:
    """Rebuild ``exc`` with caller-supplied values redacted.

    Standard Pydantic errors carry the rejected ``input`` (and a docs ``url``);
    both are dropped. Field locations (``loc``), error types, and schema
    constants (``ctx``: limits, allowed literals) are kept so callers can
    identify the failing field without seeing the value.
    """
    sanitized = []
    for error in exc.errors():
        item: dict[str, object] = {
            "type": error["type"],
            "loc": error["loc"],
            "input": _REDACTED_INPUT,
        }
        if "ctx" in error:
            item["ctx"] = error["ctx"]
        sanitized.append(item)
    return ValidationError.from_exception_data(title, sanitized)


def validate_arguments(tool: ToolName, raw: dict[str, object]) -> BaseModel:
    """Validate ``raw`` against ``tool``'s argument model.

    Returns the validated model. Raises a sanitized :class:`ValidationError`
    that names the failing field but never echoes caller-supplied values.
    Unknown ``tool`` names raise :class:`KeyError` (programmer error, not a
    caller error).
    """
    model = TOOL_ARGUMENT_MODELS[tool]
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise _sanitized_error(exc, model.__name__) from None
