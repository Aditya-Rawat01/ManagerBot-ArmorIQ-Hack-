/* Generated from contracts/schemas/proxylead-contracts.schema.json by json-schema-to-typescript. DO NOT EDIT. Run: npm run gen:types */

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
  | "tool_executed"
  | "tool_rejected"
  | "answer_generated"
  | "answer_abstained"
  | "delivery_attempted"
  | "delivery_succeeded"
  | "delivery_failed";
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
/**
 * Knowledge sensitivity tiers (PRD v2 §5.1). Encoded in tool names, never in arguments.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "DataTier".
 */
export type DataTier = "team" | "confidential" | "restricted";
/**
 * Where a request entered ProxyLead.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "EntryPoint".
 */
export type EntryPoint = "slack_mention" | "slack_dm" | "web";
export type Code = string;
export type Message = string;
export type RequestId2 = string | null;
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
export type SourceId = string;
export type SupersededBy = string | null;
export type Title = string;
export type Version1 = number;
export type Hits = KnowledgeSearchHit[];
/**
 * Calibrated threshold; None until E17
 */
export type MinScore = number | null;
export type Query = string;
/**
 * Registered MCP endpoint names (PRD v2 §2). Must match the ArmorIQ console exactly.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "McpEndpoint".
 */
export type McpEndpoint = "memory-mcp" | "workspace-mcp";
export type EscalationUserConfirmed = boolean;
export type MatchedTopicRuleId = string | null;
export type Purpose = string;
export type StepIndex2 = number;
export type Steps = PlannedToolCall[];
export type Summary = string;
export type ReceivedAt = string;
export type RequestId3 = string;
export type SlackChannelId = string | null;
/**
 * Used for event deduplication
 */
export type SlackEventId = string | null;
export type SlackTeamId = string | null;
export type SlackThreadTs = string | null;
export type TraceId1 = string;
export type WorkspaceId = string;
/**
 * What actually happened at the tool server, as evidenced by its ledger.
 *
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ToolExecutionStatus".
 */
export type ToolExecutionStatus = "executed" | "rejected_by_tool_server" | "not_reached" | "error" | "unknown";
export type ErrorCode = string | null;
export type ErrorMessage = string | null;
/**
 * Application-generated invocation ID (not an ArmorIQ claim)
 */
export type Jti1 = string;
export type LedgerEntryId = string | null;
export type PlanId2 = string;
export type RequestId4 = string;
export type StepIndex3 = number;
/**
 * None when it cannot be determined
 */
export type ToolServerReached = boolean | null;

/**
 * Generated from proxylead_contracts. Do not edit by hand.
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
 * This interface was referenced by `ProxyLeadContracts`'s JSON-Schema
 * via the `definition` "ErrorBody".
 */
export interface ErrorBody {
  code: Code;
  details: unknown;
  message: Message;
  request_id: RequestId2;
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
  source_id: SourceId;
  superseded_by: SupersededBy;
  tier: DataTier;
  title: Title;
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
  step_index: StepIndex2;
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
  request_id: RequestId3;
  requester: AuthenticatedUser;
  slack_channel_id: SlackChannelId;
  slack_event_id: SlackEventId;
  slack_team_id: SlackTeamId;
  slack_thread_ts: SlackThreadTs;
  trace_id: TraceId1;
  workspace_id: WorkspaceId;
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
  jti: Jti1;
  ledger_entry_id: LedgerEntryId;
  plan_id: PlanId2;
  request_id: RequestId4;
  result: unknown;
  status: ToolExecutionStatus;
  step_index: StepIndex3;
  tool: ToolName;
  tool_server_reached: ToolServerReached;
}
