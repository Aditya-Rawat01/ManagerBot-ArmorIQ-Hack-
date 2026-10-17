/* Generated from services/contracts/schemas/proxylead-contracts.schema.json by json-schema-to-typescript. DO NOT EDIT. Run: npm run gen:types */

/**
 * Application role label. ArmorIQ policy, not this label, authorizes tools.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "AppRole".
 */
export type AppRole = "manager" | "senior" | "junior";
/**
 * HOLD lifecycle state.
 *
 * `pending`, `approved`, `rejected`, `expired` mirror statuses the pinned SDK
 * reports for a delegation. `timeout`, `interrupted`, `unverified` are
 * application states (never treated as approval).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ApprovalState".
 */
export type ApprovalState = "pending" | "approved" | "rejected" | "expired" | "timeout" | "interrupted" | "unverified";
export type DelegationId = string | null;
export type PlanId = string;
export type RequestId = string;
export type RequestedAt = string;
export type ResolvedAt = string | null;
export type StepIndex = number;
/**
 * Logical tool/action names from PRD v2 §6.
 *
 * Exact registry representation must be confirmed by experiment E5.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ToolName".
 */
export type ToolName =
  | "knowledge_search_team"
  | "knowledge_search_confidential"
  | "knowledge_search_restricted"
  | "knowledge_get_source_team"
  | "knowledge_get_source_confidential"
  | "knowledge_get_source_restricted"
  | "read_source_team"
  | "read_source_confidential"
  | "read_source_restricted"
  | "knowledge_save_team_note"
  | "knowledge_confirm_maya_decision"
  | "knowledge_mark_superseded"
  | "create_followup_task"
  | "send_external_message";
export type ArmoriqPlanId = string | null;
export type DecidedAt = string;
export type DelegationId1 = string | null;
export type EnforcementAction = string | null;
/**
 * Authorization outcome for one planned tool call.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "DecisionOutcome".
 */
export type DecisionOutcome = "allow" | "block" | "hold" | "unverified";
export type Reason = string | null;
/**
 * Which layer produced a decision.
 *
 * Union of the labels in PRD v2 §10.1 and Architecture v2.1 §5.3 (see ADR-009).
 * Never label an SDK client-side mismatch as a hosted-proxy decision.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "DecisionSource".
 */
export type DecisionSource =
  | "armoriq_issuance"
  | "armoriq_policy_decision"
  | "armoriq_sdk_plan_check"
  | "armoriq_proxy_decision"
  | "tool_server_rule"
  | "application_rule"
  | "unknown";
export type EventId = string;
/**
 * Provisional audit event types (PRD v2 §7 step 14). Extend in Phase 2.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "AuditEventType".
 */
export type AuditEventType =
  | "request_received"
  | "plan_proposed"
  | "plan_rejected"
  | "plan_minted"
  | "authorization_decided"
  | "hold_created"
  | "approval_resolved"
  | "escalation_created"
  | "confirmation_resolved"
  | "tool_executed"
  | "tool_rejected"
  | "answer_generated"
  | "answer_abstained"
  | "delivery_attempted"
  | "delivery_succeeded"
  | "delivery_failed"
  | "slack_event_rejected"
  | "simulation_activated";
export type Jti = string | null;
export type OccurredAt = string;
/**
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "JsonValue".
 */
export type JsonValue = unknown;
export type PlanId1 = string | null;
export type RequestId1 = string;
export type StepIndex1 = number | null;
export type TraceId = string;
export type DisplayName = string;
/**
 * e.g. m***@example.com; never the raw address
 */
export type MaskedEmail = string;
export type MemberId = string;
/**
 * Fictional demo members (PRD v2 §1). Labels only — never identity proof.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "PersonaKey".
 */
export type PersonaKey = "maya" | "sam" | "priya";
export type SlackUserId = string | null;
export type SourceId = string;
/**
 * Knowledge sensitivity tiers (PRD v2 §5.1). Encoded in tool names, never in arguments.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "DataTier".
 */
export type DataTier = "team" | "confidential" | "restricted";
export type Title = string | null;
export type EscalationId = string;
export type ExpiresAt = string;
export type Token = string;
export type ConfirmationToken = string | null;
export type Message = string;
export type SimulateHijackedModel = boolean;
export type RequestId2 = string;
/**
 * Where an answer was delivered (Architecture v2.1 §7.2).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "DeliveryChannel".
 */
export type DeliveryChannel = "slack_thread" | "slack_dm" | "web_private";
/**
 * Where a request entered ProxyLead.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "EntryPoint".
 */
export type EntryPoint = "slack_mention" | "slack_dm" | "web";
export type Code = string;
export type Message1 = string;
export type RequestId3 = string | null;
export type Environment = string;
export type Service = string;
export type Status = "ok";
export type Version = string;
export type ChunkId = string;
export type Rank = number;
export type Score = number;
export type Snippet = string;
export type Author = string;
export type CreatedAt = string;
/**
 * e.g. Slack permalink
 */
export type ReferenceUrl = string | null;
/**
 * Knowledge authority (PRD v2 §8.4). Supersession is tracked separately.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ReviewStatus".
 */
export type ReviewStatus = "draft" | "authoritative";
export type SourceId1 = string;
export type SupersededBy = string | null;
export type Title1 = string;
export type Version1 = number;
export type Hits = KnowledgeSearchHit[];
/**
 * Calibrated threshold; None until E17
 */
export type MinScore = number | null;
export type Query = string;
export type Label = string;
export type Name = string;
export type Status1 = "connected" | "unavailable" | "planned";
export type CheckedAt = string;
export type Connectors = ManagerConnectorStatus[];
export type Environment1 = string;
export type Service1 = string;
export type Version2 = string;
export type ArmoriqConsoleUrl = string | null;
export type DelegationId2 = string | null;
export type Note = string;
export type PlanId2 = string;
export type RequestId4 = string;
export type RequestedAt1 = string;
export type StepIndex2 = number;
export type LastEventAt = string;
export type RequestId5 = string;
export type DelegationId3 = string | null;
export type DeliveryStatus = string | null;
export type EnforcementLayer = string | null;
export type ErrorClass = string | null;
export type EventType = string;
/**
 * What actually happened at the tool server, as evidenced by its ledger.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ToolExecutionStatus".
 */
export type ToolExecutionStatus = "executed" | "rejected_by_tool_server" | "not_reached" | "error" | "unknown";
export type IsArmoriqPolicy = boolean;
export type Jti1 = string | null;
export type Kind = "request" | "decision" | "delivery";
export type LedgerEntryId = string | null;
export type OccurredAt1 = string;
export type PlanId3 = string | null;
export type Reason1 = string | null;
export type Simulated = boolean;
export type StepIndex3 = number | null;
export type ToolServerReached = boolean | null;
export type ValidationOutcome = string | null;
export type Topic = string;
export type RuleId = string;
export type Topic1 = string;
export type Note1 = string;
export type Rules = ManagerTopicRuleView[];
export type CreatedAt1 = string;
export type Question = string;
export type QuestionId = string;
export type Reason2 = string | null;
export type RequestId6 = string;
export type Status2 = string;
/**
 * Registered MCP endpoint names (PRD v2 §2). Must match the ArmorIQ console exactly.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "McpEndpoint".
 */
export type McpEndpoint = "memory-mcp" | "workspace-mcp";
export type EnforcementLayer1 = string;
export type Executed = false;
export type IsArmoriqPolicy1 = boolean;
export type Rejection = string | null;
export type Simulated1 = boolean;
export type EscalationUserConfirmed = boolean;
export type MatchedTopicRuleId = string | null;
export type Purpose = string;
export type StepIndex4 = number;
export type Steps = PlannedToolCall[];
export type Summary = string;
export type ReceivedAt = string;
export type RequestId7 = string;
export type SlackChannelId = string | null;
/**
 * Used for event deduplication
 */
export type SlackEventId = string | null;
export type SlackTeamId = string | null;
export type SlackThreadTs = string | null;
export type TraceId1 = string;
export type WorkspaceId = string;
export type AbstainReason = string | null;
export type Abstained = boolean;
export type AnswerText = string | null;
export type Citations = CitationView[];
export type DeliveryStatus1 = string | null;
/**
 * Executed send_external_message steps of this request, counted from the persisted ledger-backed execution references (the only way the honeypot writes its synthetic sink). Guarded mode: always 0. The backend cannot read the sink table itself (grant PROPOSED, SHARED_INTERFACES §7).
 */
export type EgressSinkExecutions = number;
export type LastEventAt1 = string | null;
export type Note2 = string | null;
export type OffPlanAttempts = OffPlanAttemptView[];
export type Outcome = ("allow" | "block" | "hold" | "abstain" | "error" | "unverified" | "clarification") | null;
export type RequestId8 = string;
export type SimulationActive = boolean;
export type State = "processing" | "pending_approval" | "awaiting_confirmation" | "completed";
export type IsArmoriqPolicy2 = boolean;
export type StepIndex5 = number;
export type Steps1 = StepStatusView[];
export type Passcode = string;
export type DisplayName1 = string;
export type MaskedEmail1 = string;
export type ErrorCode = string | null;
export type ErrorMessage = string | null;
/**
 * Application-generated invocation ID (not an ArmorIQ claim)
 */
export type Jti2 = string;
export type LedgerEntryId1 = string | null;
export type PlanId4 = string;
export type RequestId9 = string;
export type StepIndex6 = number;
/**
 * None when it cannot be determined
 */
export type ToolServerReached1 = boolean | null;

/**
 * Generated from services.contracts. Do not edit by hand.
 */
export interface ProxyLeadContracts {
  [k: string]: unknown;
}
/**
 * State of a real ArmorIQ HOLD for one plan step.
 *
 * `delegation_id` may be absent briefly: the SDK reports the hold before the
 * delegation request exists. UNRESOLVED: whether approver identity is
 * observable through the pinned SDK.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ApprovalStatus".
 */
export interface ApprovalStatus {
  delegation_id: DelegationId;
  plan_id: PlanId;
  request_id: RequestId;
  requested_at: RequestedAt;
  resolved_at: ResolvedAt;
  state: ApprovalState;
  step_index: StepIndex;
  tool: ToolName;
}
/**
 * Authorization result for one planned step, labeled by the layer that decided.
 *
 * Mapped from real ArmorIQ SDK responses/exceptions by the backend gateway.
 * `enforcement_action` keeps ArmorIQ's raw value (e.g. "allow_log") verbatim.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ArmorIQDecision".
 */
export interface ArmorIQDecision {
  armoriq_plan_id: ArmoriqPlanId;
  decided_at: DecidedAt;
  delegation_id: DelegationId1;
  enforcement_action: EnforcementAction;
  outcome: DecisionOutcome;
  reason: Reason;
  source: DecisionSource;
}
/**
 * Append-only audit event. `payload` must already be redacted (no secrets/tokens).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "AuditEvent".
 */
export interface AuditEvent {
  decision_source: DecisionSource | null;
  event_id: EventId;
  event_type: AuditEventType;
  jti: Jti;
  occurred_at: OccurredAt;
  payload: Payload;
  plan_id: PlanId1;
  request_id: RequestId1;
  step_index: StepIndex1;
  trace_id: TraceId;
}
export interface Payload {
  [k: string]: JsonValue;
}
/**
 * A requester resolved by trusted backend code (PRD v2 §4.1).
 *
 * Deliberately excludes the raw ArmorIQ member email: it stays inside the
 * backend (masked in UI/logs). Identity is never taken from chat text.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "AuthenticatedUser".
 */
export interface AuthenticatedUser {
  app_role: AppRole;
  display_name: DisplayName;
  masked_email: MaskedEmail;
  member_id: MemberId;
  persona: PersonaKey;
  slack_user_id: SlackUserId;
}
/**
 * A validated citation of the delivered answer (source ID + tier persisted;
 * the title only from the owner's web inbox).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "CitationView".
 */
export interface CitationView {
  source_id: SourceId;
  tier: DataTier;
  title: Title;
}
/**
 * A pending D-14 confirmation for the owner's confirm control.
 *
 * ``token`` is the single-use value the web confirm button posts back as
 * ``confirmation_token``; it is shown only to the requesting member's
 * session, only until it expires, and is never logged or audited.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ConfirmationOfferView".
 */
export interface ConfirmationOfferView {
  escalation_id: EscalationId;
  expires_at: ExpiresAt;
  requested_tier: DataTier;
  token: Token;
}
/**
 * Web request body. No identity, tier, or recipient field exists (R-10).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "CreateRequestBody".
 */
export interface CreateRequestBody {
  confirmation_token: ConfirmationToken;
  message: Message;
  simulate_hijacked_model: SimulateHijackedModel;
}
/**
 * ``202`` body: the orchestrator-minted request ID (``req-<32 hex>``).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "CreateRequestResponse".
 */
export interface CreateRequestResponse {
  request_id: RequestId2;
}
/**
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ErrorBody".
 */
export interface ErrorBody {
  code: Code;
  details: unknown;
  message: Message1;
  request_id: RequestId3;
}
/**
 * Every non-2xx JSON response from ProxyLead services uses this envelope.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ErrorResponse".
 */
export interface ErrorResponse {
  error: ErrorBody;
}
/**
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "HealthResponse".
 */
export interface HealthResponse {
  environment: Environment;
  service: Service;
  status: Status;
  version: Version;
}
/**
 * UNRESOLVED: snippet length limit and score scale (depends on E17 / RRF design).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "KnowledgeSearchHit".
 */
export interface KnowledgeSearchHit {
  chunk_id: ChunkId;
  rank: Rank;
  score: Score;
  snippet: Snippet;
  source: SourceCitation;
}
/**
 * Provenance for a knowledge record (PRD v2 §8.1).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "SourceCitation".
 */
export interface SourceCitation {
  author: Author;
  created_at: CreatedAt;
  reference_url: ReferenceUrl;
  review_status: ReviewStatus;
  source_id: SourceId1;
  superseded_by: SupersededBy;
  tier: DataTier;
  title: Title1;
  version: Version1;
}
/**
 * Output of a `knowledge_search_*` tool. `tier` is set by the server from the tool name.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "KnowledgeSearchResult".
 */
export interface KnowledgeSearchResult {
  hits: Hits;
  min_score: MinScore;
  query: Query;
  tier: DataTier;
  tool: ToolName;
}
/**
 * One connector tile of ``GET /api/v1/manager/health``.
 *
 * ``status`` is the machine value; ``label`` is the exact wording the
 * console shows (P4-04 §9.6). ``planned`` is never a fake health result:
 * it marks a connector that does not exist yet (Gmail, Drive, Meet).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ManagerConnectorStatus".
 */
export interface ManagerConnectorStatus {
  label: Label;
  name: Name;
  status: Status1;
}
/**
 * ``GET /api/v1/manager/health``: real checks, one row per connector.
 *
 * Every ``connected`` / ``unavailable`` verdict comes from a live probe
 * (tool-server ``/health``, a backend ``SELECT 1``, the Slack socket
 * thread); an unconfigured or failed probe reports ``unavailable``, never
 * a fake ``connected``.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ManagerHealthView".
 */
export interface ManagerHealthView {
  checked_at: CheckedAt;
  connectors: Connectors;
  environment: Environment1;
  service: Service1;
  version: Version2;
}
/**
 * One unresolved HOLD of ``GET /api/v1/manager/holds``.
 *
 * ``armoriq_console_url`` is the D-22 target where Maya resolves it;
 * ``note`` repeats that approval happens only there. There is no approve
 * or reject field by design (R-40, Arch §5.6).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ManagerHoldView".
 */
export interface ManagerHoldView {
  approval_state: ApprovalState;
  armoriq_console_url: ArmoriqConsoleUrl;
  delegation_id: DelegationId2;
  note: Note;
  plan_id: PlanId2;
  request_id: RequestId4;
  requested_at: RequestedAt1;
  requester_persona: PersonaKey | null;
  step_index: StepIndex2;
  tool: ToolName | null;
}
/**
 * One row of ``GET /api/v1/manager/requests`` (newest first).
 *
 * Mirrors the P3-01 ``RequestSummary``; only IDs that have a
 * ``request_received`` event are listed. Identity fields are empty when
 * the stored origin payload is unreadable.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ManagerRequestSummary".
 */
export interface ManagerRequestSummary {
  entry_point: EntryPoint | null;
  last_event_at: LastEventAt;
  request_id: RequestId5;
  requester_persona: PersonaKey | null;
}
/**
 * One event of ``GET /api/v1/manager/requests/{request_id}/timeline``.
 *
 * Merged request / decision / delivery events, oldest first, with the
 * console's correlation fields: ``jti`` and ``ledger_entry_id`` line up
 * with the execution ledger (SHARED_INTERFACES §13). Every optional field
 * is a whitelisted extraction from the reader's redacted payload; missing
 * or unknown values stay ``None`` rather than failing the page.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ManagerTimelineEntry".
 */
export interface ManagerTimelineEntry {
  approval_state: ApprovalState | null;
  decision_source: DecisionSource | null;
  delegation_id: DelegationId3;
  delivered_to_persona: PersonaKey | null;
  delivery_channel: DeliveryChannel | null;
  delivery_status: DeliveryStatus;
  enforcement_layer: EnforcementLayer;
  error_class: ErrorClass;
  event_type: EventType;
  execution_status: ToolExecutionStatus | null;
  is_armoriq_policy: IsArmoriqPolicy;
  jti: Jti1;
  kind: Kind;
  ledger_entry_id: LedgerEntryId;
  occurred_at: OccurredAt1;
  outcome: DecisionOutcome | null;
  plan_id: PlanId3;
  reason: Reason1;
  simulated: Simulated;
  step_index: StepIndex3;
  tier_delivered: DataTier | null;
  tool: ToolName | null;
  tool_server_reached: ToolServerReached;
  validation_outcome: ValidationOutcome;
}
/**
 * ``PUT /api/v1/manager/topic-rules`` body: topic + ceiling only.
 *
 * A request body, so it can never carry identity or authority: unknown
 * keys (``member_id``, ``app_role``, ``actor``, ...) are refused with a
 * ``422`` envelope. ``topic`` validity is enforced by
 * ``apps.api.topic_map.validate_topic`` (normalization, length,
 * specificity), mapped to the same ``422``; the stored
 * ``created_by_member_id`` always comes from the session.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ManagerTopicRuleUpdate".
 */
export interface ManagerTopicRuleUpdate {
  max_tier: DataTier;
  topic: Topic;
}
/**
 * One topic-map rule (P3-02): retrieval configuration, not authority.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ManagerTopicRuleView".
 */
export interface ManagerTopicRuleView {
  max_tier: DataTier;
  rule_id: RuleId;
  topic: Topic1;
}
/**
 * ``GET /api/v1/manager/topic-rules``: the rules plus the access note.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ManagerTopicRulesView".
 */
export interface ManagerTopicRulesView {
  note: Note1;
  rules: Rules;
}
/**
 * One queue row of ``GET /api/v1/manager/unanswered``.
 *
 * The question is stored redacted at write time and passed through
 * unchanged; no protected snippet is ever part of this view.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ManagerUnansweredView".
 */
export interface ManagerUnansweredView {
  created_at: CreatedAt1;
  question: Question;
  question_id: QuestionId;
  reason: Reason2;
  request_id: RequestId6;
  requester_persona: PersonaKey | null;
  status: Status2;
}
/**
 * A model-requested tool call routed off-plan (P3-03/P3-07). Never executed.
 *
 * ``enforcement_layer`` is the server's wording for ``decision_source``;
 * ``is_armoriq_policy`` is true only for ArmorIQ policy sources (ADR-026),
 * never for the client-side SDK plan check or application rules.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "OffPlanAttemptView".
 */
export interface OffPlanAttemptView {
  decision_source: DecisionSource;
  enforcement_layer: EnforcementLayer1;
  executed: Executed;
  is_armoriq_policy: IsArmoriqPolicy1;
  outcome: DecisionOutcome;
  rejection: Rejection;
  simulated: Simulated1;
  tool: ToolName | null;
}
/**
 * Planner output, built from the user's message only (PRD v2 §7).
 *
 * UNRESOLVED (Phase 2): maximum step count, an `intent` field, and a
 * clarifying-question field. Only the PRD's elevated-tier rule is enforced.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "PlanProposal".
 */
export interface PlanProposal {
  escalation_user_confirmed: EscalationUserConfirmed;
  matched_topic_rule_id: MatchedTopicRuleId;
  steps: Steps;
  summary: Summary;
}
/**
 * One validated step of a plan. Tier is implied by `tool`, never an argument.
 *
 * UNRESOLVED (Phase 2): per-tool argument schemas. Until they exist,
 * `arguments` is a JSON object screened for forbidden/underscore keys.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "PlannedToolCall".
 */
export interface PlannedToolCall {
  arguments: Arguments;
  mcp: McpEndpoint;
  purpose: Purpose;
  step_index: StepIndex4;
  tool: ToolName;
}
export interface Arguments {
  [k: string]: JsonValue;
}
/**
 * Trusted context created by the backend for every inbound request.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "RequestContext".
 */
export interface RequestContext {
  entry_point: EntryPoint;
  received_at: ReceivedAt;
  request_id: RequestId7;
  requester: AuthenticatedUser;
  slack_channel_id: SlackChannelId;
  slack_event_id: SlackEventId;
  slack_team_id: SlackTeamId;
  slack_thread_ts: SlackThreadTs;
  trace_id: TraceId1;
  workspace_id: WorkspaceId;
}
/**
 * Owner-only status of one request (P4-01, polled by P4-02/P4-03).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "RequestStatusView".
 */
export interface RequestStatusView {
  abstain_reason: AbstainReason;
  abstained: Abstained;
  answer_text: AnswerText;
  citations: Citations;
  confirmation: ConfirmationOfferView | null;
  delivery_channel: DeliveryChannel | null;
  delivery_status: DeliveryStatus1;
  egress_sink_executions: EgressSinkExecutions;
  last_event_at: LastEventAt1;
  note: Note2;
  off_plan_attempts: OffPlanAttempts;
  outcome: Outcome;
  request_id: RequestId8;
  simulation_active: SimulationActive;
  state: State;
  steps: Steps1;
}
/**
 * One planned step as persisted (decision, approval, execution reference).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "StepStatusView".
 */
export interface StepStatusView {
  approval_state: ApprovalState | null;
  decision_source: DecisionSource | null;
  execution_status: ToolExecutionStatus | null;
  is_armoriq_policy: IsArmoriqPolicy2;
  outcome: DecisionOutcome | null;
  step_index: StepIndex5;
  tool: ToolName | null;
}
/**
 * Sign-in body. The only request body that may carry a persona (PRD §4.1).
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "SessionCreateRequest".
 */
export interface SessionCreateRequest {
  passcode: Passcode;
  persona: PersonaKey;
}
/**
 * Current session. Carries the masked email, never the raw address.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "SessionInfo".
 */
export interface SessionInfo {
  app_role: AppRole;
  display_name: DisplayName1;
  masked_email: MaskedEmail1;
  persona: PersonaKey;
}
/**
 * What the tool server actually did. A plan or ALLOW is not proof of execution.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ToolInvocationResult".
 */
export interface ToolInvocationResult {
  error_code: ErrorCode;
  error_message: ErrorMessage;
  jti: Jti2;
  ledger_entry_id: LedgerEntryId1;
  plan_id: PlanId4;
  request_id: RequestId9;
  result: unknown;
  status: ToolExecutionStatus;
  step_index: StepIndex6;
  tool: ToolName;
  tool_server_reached: ToolServerReached1;
}
