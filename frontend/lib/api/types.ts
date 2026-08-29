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
  /** Filed referrals. Absent on a profile the backend has not written one to,
      so every reader must treat it as optional rather than spreading it. */
  open_referrals?: ReferralSummary[];
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
  /**
   * Where the structure actually is, so a renderer can put a real view of the
   * world behind the derived one. Reference data from the city adapter, and
   * always present: the endpoint 404s on an address it cannot place rather
   * than returning geometry with nowhere to put it.
   */
  latitude: number;
  longitude: number;
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
  /**
   * Present when this line is what a 911 caller said, not what a record holds.
   * The backend type refuses to let such a line be CONFIRMED, carry a fact id,
   * or carry a provenance source type -- so this field arriving is the whole
   * reported-versus-observed distinction, and it has to be visible.
   */
  reported_note: string | null;
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
  /**
   * The street address a dispatcher would read aloud. Reference data from the
   * city adapter, not a fact -- nothing derives or merges it.
   *
   * Empty when the city cannot place the id. The banner falls back to the id
   * rather than printing a slug as though it were a street address.
   */
  address_display: string;
  profile_snapshot_id: string;
  grant_id: string;
  cold_start: boolean;
  dispatched_at: string;
  elapsed_seconds: number;
  brief: BriefEmissionView;
  instant_brief_ms: number;
  event_id: string;
  /** Present only when a 911 transcript or CAD narrative came with the call. */
  intake: IntakeResponse | null;
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
  /** Which fire this belongs to, where it belongs to one. */
  incident_id?: string | null;
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

// ---------------------------------------------------------------- 911 intake

/** What a caller said about one attribute, bound to where they said it.
 *
 * The offsets are not decoration. A reported value nobody can trace back to
 * the transcript is a claim, so the console renders the quote and the console
 * can therefore be checked against the call.
 */
export interface ReportedLine {
  intake_key: string;
  reported_value: string;
  quoted_text: string;
  start_offset: number;
  end_offset: number;
}

export type IntakeChannel = 'CALL_911' | 'CAD_NARRATIVE';

/** The result of reading a 911 transcript or a CAD narrative.
 *
 * `accepted: false` is the normal degraded case, not an error: the screen was
 * down, the model was down, or the answer was malformed. The instant brief is
 * already on screen by the time any of this runs, so the worst outcome is that
 * it stays as it was — which is why nothing here throws.
 */
export interface IntakeResponse {
  incident_id: string;
  channel: IntakeChannel;
  source_ref: string;
  accepted: boolean;
  rejection_reason: string | null;
  model_ref: string;
  /** Screened before the model saw it. Same screen ingested permits get. */
  screened: boolean;
  screen: string;
  screen_findings: string[];
  reported: ReportedLine[];
  /** Attributes the call did not settle. Required, may be empty. */
  unknowns: string[];
  brief_version: number | null;
  /** Agents this narrative woke, and the routing rules that fired.
   *
   * `agent_ref` is `agent-id@version`, not a bare id: which *version* woke is
   * what a replay two years later has to be able to state.
   */
  woken: HandoffLine[];
  fired_rule_ids: string[];
  unmatched_rule_ids: string[];
  /** Wakes the incident grant could not cover, naming the missing scopes.
   *
   * Withheld rather than fired-and-denied: a denial an officer cannot
   * distinguish from a real one is worse than an honest absence.
   */
  withheld: WithheldLine[];
}

/** One agent the narrative woke. */
export interface HandoffLine {
  agent_ref: string;
  intake_keys: string[];
  rule_ids: string[];
  started: boolean;
}

/** One agent the narrative would have woken, had the grant carried the scope. */
export interface WithheldLine {
  agent_ref: string;
  missing_scopes: string[];
  rule_ids: string[];
}

// --------------------------------------------------------------------- replay

/** One log entry as the replay reconstructs it.
 *
 * `intact` is per entry: a replay that could only say "something changed"
 * would not tell an investigator which record to distrust.
 */
export interface ReplayedEntryView {
  sequence: number;
  entry_id: string;
  entry_type: string;
  occurred_at: string;
  content_hash: string;
  content: Record<string, unknown>;
  intact: boolean;
  agent_versions: Record<string, string>;
  profile_snapshot_id: string;
}

/** An incident reconstructed from its own record.
 *
 * This is the NIOSH view: two years after a fatal fire, what was the commander
 * told, by which agent version, under which policy version. `intact` is the
 * whole point — a replay that cannot detect tampering is not evidence.
 */
export interface IncidentReplayView {
  incident_id: string;
  entries: ReplayedEntryView[];
  digest: string;
  intact: boolean;
  tampered_sequences: number[];
  agent_versions: Record<string, string>;
  policy_versions: string[];
  profile_snapshot_id: string;
  snapshot_available: boolean;
  sealed_at: string | null;
}


/**
 * The ground plane under the regional fire map.
 *
 * **`bounds` is not the region that was asked for.** A tile zoom is an integer,
 * so the smallest image covering a region almost always covers more, and the
 * excess is not symmetric. This is the ground the returned pixels actually
 * span, computed by the backend from the centre, zoom and pixel size it
 * requested. Draw the image against *this* box. Drawing it against the region
 * stretches it, and every detection on top lands a few kilometres from where
 * the satellite saw it -- an error with no visible symptom.
 *
 * `data_url` carries the bytes inline. The server holds the Maps key and the
 * browser is handed pixels: a signed Static Maps URL reaching a client is the
 * key reaching a client.
 */
export interface RegionBasemapView {
  available: boolean;
  /** "static-map", "synthetic", or "" on a refusal. */
  provider: string;
  content_type: string;
  data_url: string;
  bounds: { west: number; south: number; east: number; north: number } | null;
  /** The Web Mercator zoom the image was rendered at. */
  zoom: number;
  style: string;
  /** Required on screen wherever it is non-empty: Google's Terms, and a
      synthetic ground plane says in this line that it is one. */
  attribution: string;
  unavailable_reason: string;
}

// ---------------------------------------------------------- entry package --

/**
 * One readiness criterion, as the backend evaluated it.
 *
 * `passed` is the whole verdict for this line and there is no third state: a
 * check that could not run fails and says why in `reason`. The console must
 * never render a failure as anything other than a failure -- that inversion is
 * the single reading this document exists to prevent.
 */
export interface CriterionView {
  criterion_id: string;
  title: string;
  passed: boolean;
  reason: string;
  /** Fact ids, canonical keys, face labels, conflict ids. Never a value. */
  refs: string[];
}

/**
 * The six criteria and the verdict over them.
 *
 * `ready`, `failed_ids` and `summary` are computed by the backend and sent on
 * the wire deliberately -- a console that re-derived "is this ready" from six
 * booleans is a console that can disagree with the document it is rendering.
 * So nothing here recomputes them.
 */
export interface ReadinessAssessmentView {
  incident_id: string;
  address_id: string;
  assessed_at: string;
  assessed_by: string;
  assessed_by_version: string;
  profile_snapshot_id: string;
  criteria: CriterionView[];
  ready: boolean;
  failed_ids: string[];
  summary: string;
}

/** One priced term on a leg: what it cost and what measurement said so. */
export interface CostTermView {
  term_id: string;
  weight: number;
  detail: string;
  refs: string[];
}

/** A leg the cost model refused to build, and why. Never a large number. */
export interface BarrierView {
  from_id: string;
  to_id: string;
  reason: string;
  refs: string[];
}

/**
 * One point on the route, in every frame a renderer might want it in.
 *
 * `x_m`/`y_m`/`z_m` are footprint-local metres -- the same frame the geometry
 * spec's footprint is in, so the two can be drawn together without a transform
 * either side invented. `longitude`/`latitude` are **null** whenever the city
 * could not place the parcel; a renderer that filled them in from anywhere
 * else would be putting a crew's route at coordinates nobody surveyed.
 */
export interface WaypointView {
  node_id: string;
  kind: string;
  face: string;
  level: number | null;
  x_m: number;
  y_m: number;
  z_m: number;
  longitude: number | null;
  latitude: number | null;
}

export interface RouteLegView {
  from_id: string;
  to_id: string;
  distance_m: number;
  cost: number;
  multiplier: number;
  terms: CostTermView[];
  /** What the search priced this against and rejected. */
  avoided: string[];
  /** One sentence joining the two. Composed from the terms, never authored. */
  chose_because: string;
}

export interface RouteView {
  waypoints: WaypointView[];
  legs: RouteLegView[];
  total_cost: number;
  total_distance_m: number;
  expanded_nodes: number;
}

/**
 * The route in, the route out, or a stated refusal.
 *
 * `refused` and `entry` are not redundant: a refusal carries no route at all
 * and the reason is the finding. There is no fallback route and no straight
 * line, so a console that drew something when `refused` is true would be
 * drawing a path the backend declined to compute.
 */
export interface EntryPathPlanView {
  incident_id: string;
  address_id: string;
  algorithm: string;
  heuristic: string;
  target_level: number;
  refused: boolean;
  refusal_reason: string;
  refusal_refs: string[];
  entry: RouteView | null;
  egress: RouteView | null;
  egress_note: string;
  barriers: BarrierView[];
  unscanned_faces: string[];
  node_count: number;
  edge_count: number;
  /** The face the route makes entry through, or an empty string. */
  entry_face: string;
}

export interface BriefClaimView {
  claim_id: string;
  /** One of READINESS, STRUCTURE, THERMAL, ROUTE, UNKNOWNS, CAVEATS. */
  section: string;
  text: string;
  refs: string[];
}

export interface CrewBriefView {
  brief_id: string;
  incident_id: string;
  address_id: string;
  composed_at: string;
  composed_by: string;
  composed_by_version: string;
  profile_snapshot_id: string;
  claims: BriefClaimView[];
  prose: string;
  /** "deterministic" or "model". Shown, because the two are not the same claim. */
  prose_source: string;
  /** Why a model composition was refused, when one was. A stable code. */
  prose_rejection: string;
  model_ref: string;
  unknowns: string[];
  readiness_summary: string;
  claim_refs: string[];
}

export type PackageStatus = 'AWAITING_APPROVAL' | 'READY_TO_SEND' | 'SENT';

/** The two halves, named the way the endpoint path spells them. */
export type PackageHalf = 'entry-path' | 'crew-brief';

/**
 * One assessment, one path, one brief, and what humans did about them.
 *
 * `status` and `outstanding_halves` are computed backend-side from the two
 * approval stamps; the console reflects them rather than deriving its own,
 * for the same reason it does not recompute `ready`.
 */
export interface EntryPackageView {
  package_id: string;
  incident_id: string;
  address_id: string;
  created_at: string;
  created_by: string;
  created_by_version: string;
  assessment: ReadinessAssessmentView;
  path: EntryPathPlanView;
  brief: CrewBriefView;
  path_approval_id: string;
  brief_approval_id: string;
  path_approved_by: string | null;
  path_approved_at: string | null;
  brief_approved_by: string | null;
  brief_approved_at: string | null;
  sent_at: string | null;
  sent_by: string | null;
  dispatch_decision_id: string;
  disclaimer: string;
  path_approved: boolean;
  brief_approved: boolean;
  status: PackageStatus;
  outstanding_halves: PackageHalf[];
}

/** One row of the package list. Ids, statuses and counts, never a claim. */
export interface PackageSummaryView {
  package_id: string;
  status: string;
  created_at: string;
  ready: boolean;
  path_refused: boolean;
  outstanding: string[];
  sent_at: string | null;
}

export interface PackageListView {
  incident_id: string;
  packages: PackageSummaryView[];
}
