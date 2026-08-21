/**
 * Types mirroring the backend contract.
 *
 * Kept hand-written and narrow on purpose: the console should fail to compile
 * when the backend contract changes, not discover it at 03:00 on a fireground.
 * `npm run typecheck` is the check; `docs/openapi.json` is the source.
 */

export type AssertionStatus = 'CONFIRMED' | 'DISPUTED' | 'UNKNOWN';

export type CapabilityStatus = 'AVAILABLE' | 'PLANNED';

export interface CapabilityInfo {
  id: string;
  label: string;
  status: CapabilityStatus;
  phase: number;
}

export interface SystemStatus {
  app: string;
  version: string;
  environment: string;
  /** "fake" or "live" -- shown prominently; a hidden simulation is worse. */
  mode: string;
  /** "memory" or "firestore" -- where durable memory actually lives. */
  storage_backend: string;
  /** "memory" or "pubsub" -- how events actually move. */
  event_backend: string;
  /**
   * "fake" or "google" -- whether the survey calendar and crew mail reach
   * Google Workspace. A live deployment can legitimately be "fake" here:
   * Calendar and Gmail act as a *user*, which needs delegated authority the
   * other integrations do not. Rendered so a recorded-but-not-sent crew
   * notification is stated rather than implied.
   */
  workspace_writes: string;
  municipality_id: string;
  districts: string[];
  instant_brief_budget_ms: number;
  seeded_profiles: number;
  /** Agent descriptors published in the registry at startup. */
  published_agents: number;
  capabilities: CapabilityInfo[];
  disclosure: string;
}

export interface ComponentCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface Readiness {
  status: string;
  ready: boolean;
  mode: string;
  /** Optional in the type as well as in practice: a malformed payload must
   * degrade the badge, not blank the console. */
  checks?: ComponentCheck[];
}

export interface Liveness {
  status: string;
  app: string;
  version: string;
}

/** The single error envelope every backend failure uses. */
export interface ApiErrorBody {
  code: string;
  message: string;
  details: Record<string, unknown>;
  request_id: string | null;
  correlation_id: string | null;
}

export interface ApiErrorEnvelope {
  error: ApiErrorBody;
}

/**
 * Results are returned, not thrown.
 *
 * An unreachable backend is a state the console must render honestly, not an
 * exception that blanks the screen.
 */
export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: ApiErrorBody; unreachable: boolean };

// ---------------------------------------------------------------- console --

/** How a fact is known. Never rendered by colour alone. */
export interface FactView {
  canonical_key: string;
  value: string;
  status: AssertionStatus;
  known: boolean;
  source_type: string;
  source_ref: string;
  observed_at: string;
  confidence: number;
  decayed_confidence: number | null;
  human_verified: boolean;
  /** Every fact ever written for this attribute -- winners and losers alike. */
  all_fact_ids: string[];
}

export interface ConflictView {
  conflict_id: string;
  canonical_key: string;
  rule_id: string;
  severity: number;
  summary: string;
  fact_ids: string[];
  status: 'OPEN' | 'RESOLVED';
  detected_at: string;
  resolved_by: string | null;
}

export interface TimelineEventView {
  sequence: number;
  occurred_at: string;
  type: string;
  actor: string;
  actor_version: string | null;
  summary: string;
  fact_ids: string[];
  conflict_id: string | null;
}

export interface ReferralSummary {
  referral_id: string;
  status: string;
  case_number: string | null;
  conflict_id: string;
}

export interface BuildingProfileView {
  address_id: string;
  district_id: string;
  profile_version: number;
  facts: FactView[];
  conflicts: ConflictView[];
  unknown_keys: string[];
  hydrant_ids: string[];
  last_human_survey: string | null;
  open_referrals: ReferralSummary[];
  has_geometry: boolean;
}

export interface RankReasonView {
  rule_id: string;
  detail: string;
  weight: number;
  canonical_key: string | null;
  conflict_id: string | null;
}

export interface QueueEntryView {
  entry_id: string;
  address_id: string;
  rank: number;
  score: number;
  status: string;
  reasons: RankReasonView[];
  assigned_company: string | null;
  dispatched_at: string | null;
  calendar_event_ref: string | null;
  survey_id: string | null;
}

export interface QueueView {
  district_id: string;
  entries: QueueEntryView[];
  count: number;
}

/** Where a source's records actually came from. Rendered verbatim. */
export interface SourceHealthView {
  source_id: string;
  mode: 'LIVE' | 'FIXTURE' | 'UNCONFIGURED';
  circuit_state: string;
  available: boolean;
  cache_hits: number;
  upstream_calls: number;
  last_snapshot_id: string | null;
}

export interface DistrictStatsView {
  district_id: string;
  profiles: number;
  facts: number;
  open_conflicts: number;
  high_severity_conflicts: number;
  queued_for_survey: number;
  dispatched: number;
  surveyed: number;
  profiles_never_surveyed: number;
  open_referrals: number;
  sources: SourceHealthView[];
}

// --------------------------------------------------------------- registry --

export interface AgentDescriptorView {
  agent_id: string;
  version: string;
  ref: string;
  publisher_department: string;
  loop: 'SLOW' | 'INCIDENT';
  role_summary: string;
  capabilities: string[];
  required_scopes: string[];
  classifications_accessed: string[];
  write_targets: string[];
  approval_threshold: string;
  input_schema_ref: string;
  output_schema_ref: string;
  latency_target_ms: number;
  published_at: string;
  deprecated_at: string | null;
}

export interface AgentListResponse {
  agents: AgentDescriptorView[];
  count: number;
}

export interface SubscriptionView {
  subscription_id: string;
  subscriber_department: string;
  agent_id: string;
  pinned_version: string;
  ref: string;
  subscribed_at: string;
  unsubscribed_at: string | null;
}

export interface SubscriptionListResponse {
  subscriptions: SubscriptionView[];
  count: number;
}

// ---------------------------------------------------------------- geometry --

export interface LevelView {
  height_m: number;
  provenance: string;
  status: AssertionStatus;
  fact_id: string | null;
}

export interface RoofSegmentView {
  pitch_deg: number;
  azimuth_deg: number;
  area_m2: number | null;
  provenance: string;
  status: AssertionStatus;
}

export interface ObstructionView {
  type: string;
  segment_index: number;
  provenance: string;
  status: AssertionStatus;
}

/**
 * One measured patch of a face, in face-plane coordinates.
 *
 * `u` runs across the face width, `v` runs UP from the ground. Every cell is a
 * rectangle a camera actually measured -- the gaps between cells are gaps on
 * purpose, and nothing here was interpolated or predicted.
 */
export interface ThermalCellView {
  u_from: number;
  u_to: number;
  v_from: number;
  v_to: number;
  temperature_c: number;
}

/** A face carries a measured temperature, no coverage, or an outage. */
export interface FaceView {
  label: 'ALPHA' | 'BRAVO' | 'CHARLIE' | 'DELTA' | 'ROOF';
  thermal:
    | { kind: 'QUANTITY'; magnitude: number; unit: string }
    | { kind: 'UNSCANNED'; surface: string | null }
    | { kind: 'UNAVAILABLE'; source_id: string; reason: string };
  observed_at: string | null;
  /** The registered heat map. Empty unless this face is actually scanned. */
  thermal_cells: ThermalCellView[];
}

export interface GeometrySpecView {
  spec_version: number;
  address_id: string;
  generated_at: string;
  footprint: [number, number][];
  levels: LevelView[];
  roof_segments: RoofSegmentView[];
  obstructions: ObstructionView[];
  faces: FaceView[];
  collapse_zone_radius_m: number;
}

export interface GeometryView {
  spec: GeometrySpecView;
  /** Static elevation for a renderer that cannot run the interactive spec. */
  svg: string;
  has_disputed_mass: boolean;
  total_height_m: number;
}

// ---------------------------------------------------------------- incident --

export type BriefStage = 'INSTANT' | 'ENRICHED' | 'AMENDMENT';

export interface BriefItemView {
  label: string;
  value_render: string;
  status: AssertionStatus;
  canonical_key: string | null;
  fact_id: string | null;
  provenance: string | null;
  derivation_note: string | null;
  withheld_note: string | null;
}

export interface BriefSectionView {
  key: string;
  items: BriefItemView[];
}

export interface BriefEmissionView {
  emission_id: string;
  incident_id: string;
  version: number;
  stage: BriefStage;
  sections: BriefSectionView[];
  unknowns: string[];
  unavailable: string[];
  withheld: string[];
  conflict_ids: string[];
  narrative: string | null;
  narrative_available: boolean;
  model_invoked: boolean;
  profile_snapshot_id: string;
  agent_versions: Record<string, string>;
  produced_at: string;
  /** Set by the log writer. Nothing is displayed without it. */
  persisted_at: string | null;
  content_hash: string;
}

export interface OpenIncidentResponse {
  incident_id: string;
  address_id: string;
  profile_snapshot_id: string;
  grant_id: string;
  cold_start: boolean;
  dispatched_at: string;
  elapsed_seconds: number;
  brief: BriefEmissionView;
  instant_brief_ms: number;
  event_id: string;
}

export interface IncidentLogEntryView {
  sequence: number;
  entry_type: string;
  occurred_at: string;
  profile_snapshot_id: string;
  content_hash: string;
  written_to_rms_at: string | null;
  content: Record<string, unknown>;
}

export interface IncidentLogView {
  incident_id: string;
  sealed_at: string | null;
  entries: IncidentLogEntryView[];
  unflushed: number;
}

export interface ResourceOutcomeView {
  kind_id: string;
  action: 'ALLOW' | 'DERIVE' | 'WITHHOLD_JURISDICTION' | 'REQUIRE_APPROVAL' | 'DENY';
  rule_id: string;
  decision_id: string;
  external_ref: string | null;
  approval_id: string | null;
  replayed: boolean;
}

export interface ResolutionResponse {
  conflict_id: string;
  fact_id: string;
  profile_version: number;
  brief_version: number;
  resolved_by: string;
}

export interface CloseIncidentResponse {
  incident: Record<string, unknown>;
  grant_revoked_at: string | null;
  log_sealed_at: string | null;
  log_entries: number;
  neris_draft: Record<string, unknown> | null;
  rms_still_buffered: number;
}

// ------------------------------------------------------------------- audit --

export interface AuditEventView {
  audit_id: string;
  kind: string;
  occurred_at: string;
  actor: string;
  target: string | null;
  correlation_id: string;
  detail: Record<string, string>;
}

export interface PolicyDecisionView {
  decision_id: string;
  agent_id: string;
  target: string;
  operation: string;
  classification: string;
  action: string;
  rule_id: string;
  justification: string;
  policy_version: string;
  decided_at: string;
  /** A constant. A model can explain a decision; it can never make one. */
  decided_by: string;
}
