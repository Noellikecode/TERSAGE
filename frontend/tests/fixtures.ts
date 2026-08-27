/**
 * Fixtures shaped exactly like the backend's responses.
 *
 * These mirror the seeded story: 450 Hayes, where the permit says two storeys
 * and the lidar measured three.
 */

import type {
  AgentDescriptorView,
  AuditEventView,
  BriefEmissionView,
  BuildingProfileView,
  DistrictStatsView,
  GeometryView,
  IncidentLogView,
  IntakeResponse,
  OpenIncidentResponse,
  PolicyDecisionView,
  QueueView,
  SubscriptionView,
  SystemStatus,
  TimelineEventView,
} from '@/lib/api/types';

export const ADDRESS = 'sf-0450-hayes';
export const DISTRICT = 'sffd-district-03';

export const STATUS: SystemStatus = {
  app: 'firstdue',
  version: '0.1.0',
  environment: 'local',
  mode: 'fake',
  storage_backend: 'memory',
  event_backend: 'memory',
  workspace_writes: 'fake',
  municipality_id: 'san-francisco-ca',
  districts: [DISTRICT, 'sffd-district-05'],
  instant_brief_budget_ms: 500,
  seeded_profiles: 8,
  published_agents: 9,
  capabilities: [
    { id: 'incident-loop', label: 'Streaming incident brief', status: 'AVAILABLE', phase: 5 },
  ],
  disclosure: 'Decision-support prototype, not a certified public-safety system.',
};

export const STATS: DistrictStatsView = {
  district_id: DISTRICT,
  profiles: 4,
  facts: 43,
  open_conflicts: 1,
  high_severity_conflicts: 1,
  queued_for_survey: 4,
  dispatched: 0,
  surveyed: 0,
  profiles_never_surveyed: 4,
  open_referrals: 0,
  sources: [
    {
      source_id: 'sf-permits',
      mode: 'FIXTURE',
      circuit_state: 'CLOSED',
      available: true,
      cache_hits: 2,
      upstream_calls: 1,
      last_snapshot_id: 'sf-permits:2026-08-20',
    },
    {
      source_id: 'tier-ii-confidential',
      mode: 'UNCONFIGURED',
      circuit_state: 'CLOSED',
      available: false,
      cache_hits: 0,
      upstream_calls: 0,
      last_snapshot_id: null,
    },
  ],
};

export const QUEUE: QueueView = {
  district_id: DISTRICT,
  count: 2,
  entries: [
    {
      entry_id: `queue_${DISTRICT}_${ADDRESS}`,
      address_id: ADDRESS,
      rank: 1,
      score: 0.871,
      status: 'RANKED',
      reasons: [
        {
          rule_id: 'rank.open-conflict-severity',
          detail: 'Severity 4 conflict open: Permit records 2 storeys; lidar DSM measures 3.',
          weight: 0.8,
          canonical_key: 'structure.stories',
          conflict_id: 'conflict_0c93',
        },
        {
          rule_id: 'rank.never-surveyed',
          detail: 'No company survey on record for this structure',
          weight: 1,
          canonical_key: null,
          conflict_id: null,
        },
      ],
      assigned_company: null,
      dispatched_at: null,
      calendar_event_ref: null,
      survey_id: null,
    },
    {
      entry_id: `queue_${DISTRICT}_sf-1215-fell`,
      address_id: 'sf-1215-fell',
      rank: 2,
      score: 0.42,
      status: 'RANKED',
      reasons: [
        {
          rule_id: 'rank.confidence-decay',
          detail: 'Confidence in structure.height_m has decayed to 0.20 of its filed value',
          weight: 0.8,
          canonical_key: 'structure.height_m',
          conflict_id: null,
        },
      ],
      assigned_company: null,
      dispatched_at: null,
      calendar_event_ref: null,
      survey_id: null,
    },
  ],
};

export const PROFILE: BuildingProfileView = {
  address_id: ADDRESS,
  district_id: DISTRICT,
  profile_version: 16,
  facts: [
    {
      canonical_key: 'structure.stories',
      value: '2',
      status: 'DISPUTED',
      known: true,
      source_type: 'PERMIT',
      source_ref: 'permit/2018-04871',
      observed_at: '2018-10-14T08:00:00+00:00',
      confidence: 0.92,
      decayed_confidence: 0.31,
      human_verified: false,
      all_fact_ids: ['fact_a', 'fact_b', 'fact_c'],
    },
    {
      canonical_key: 'structure.construction_type',
      value: 'wood-frame',
      status: 'CONFIRMED',
      known: true,
      source_type: 'ASSESSOR',
      source_ref: 'assessor/0808-021',
      observed_at: '2022-08-20T08:00:00+00:00',
      confidence: 0.88,
      decayed_confidence: 0.71,
      human_verified: false,
      all_fact_ids: ['fact_d'],
    },
  ],
  conflicts: [
    {
      conflict_id: 'conflict_0c93',
      canonical_key: 'structure.stories',
      rule_id: 'permit-vs-lidar-story-count',
      severity: 4,
      summary: 'Permit records 2 storeys; lidar DSM measures 3.',
      fact_ids: ['fact_a', 'fact_b'],
      status: 'OPEN',
      detected_at: '2026-08-20T08:00:00+00:00',
      resolved_by: null,
    },
  ],
  unknown_keys: ['suppression.sprinklered'],
  hydrant_ids: ['HYD-A', 'HYD-B'],
  last_human_survey: null,
  open_referrals: [
    {
      referral_id: 'ref_0c93',
      status: 'FILED',
      case_number: 'REF-00001',
      conflict_id: 'conflict_0c93',
    },
  ],
  has_geometry: true,
};

export const TIMELINE: TimelineEventView[] = [
  {
    sequence: 0,
    occurred_at: '2018-10-15T08:00:00+00:00',
    type: 'FACT_WRITTEN',
    actor: 'records-watcher',
    actor_version: '1.0.0',
    summary: 'PERMIT recorded structure.stories',
    fact_ids: ['fact_a'],
    conflict_id: null,
  },
  {
    sequence: 1,
    occurred_at: '2026-08-20T08:00:00+00:00',
    type: 'CONFLICT_DETECTED',
    actor: 'conflict-detector',
    actor_version: '1.0.0',
    summary: 'Permit records 2 storeys; lidar DSM measures 3.',
    fact_ids: ['fact_a', 'fact_b'],
    conflict_id: 'conflict_0c93',
  },
];

export const GEOMETRY: GeometryView = {
  spec: {
    spec_version: 1,
    address_id: ADDRESS,
    generated_at: '2026-08-20T08:00:00+00:00',
    footprint: [
      [0, 0],
      [11.5, 0],
      [11.5, 22],
      [0, 22],
    ],
    levels: [
      { height_m: 3.17, provenance: 'PERMIT', status: 'CONFIRMED', fact_id: 'fact_a' },
      { height_m: 3.17, provenance: 'PERMIT', status: 'CONFIRMED', fact_id: 'fact_a' },
      { height_m: 3.17, provenance: 'LIDAR_DSM', status: 'DISPUTED', fact_id: 'fact_b' },
    ],
    roof_segments: [
      { pitch_deg: 18, azimuth_deg: 210, area_m2: 126.5, provenance: 'SOLAR_API', status: 'CONFIRMED' },
    ],
    obstructions: [{ type: 'SOLAR_ARRAY', segment_index: 0, provenance: 'SOLAR_API', status: 'CONFIRMED' }],
    faces: [
      {
        label: 'ALPHA',
        thermal: { kind: 'UNSCANNED', surface: null },
        observed_at: null,
        thermal_cells: [],
      },
      {
        label: 'BRAVO',
        thermal: { kind: 'UNSCANNED', surface: null },
        observed_at: null,
        thermal_cells: [],
      },
      {
        label: 'CHARLIE',
        thermal: { kind: 'UNSCANNED', surface: null },
        observed_at: null,
        thermal_cells: [],
      },
      {
        label: 'DELTA',
        thermal: { kind: 'UNSCANNED', surface: null },
        observed_at: null,
        thermal_cells: [],
      },
    ],
    collapse_zone_radius_m: 14.27,
  },
  svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 120"><rect x="20" y="10" width="160" height="24" stroke-dasharray="5 3"/><text x="184" y="26">3.17 m DISPUTED</text></svg>',
  has_disputed_mass: true,
  total_height_m: 9.51,
  latitude: 37.7749,
  longitude: -122.4194,
};

export const AGENTS: AgentDescriptorView[] = [
  {
    agent_id: 'records-watcher',
    version: '1.0.0',
    ref: 'records-watcher@1.0.0',
    publisher_department: 'building',
    loop: 'SLOW',
    role_summary: 'Polls permit, assessor, inspection, and violation records into facts.',
    capabilities: ['READ'],
    required_scopes: ['read:public-records', 'write:profile'],
    classifications_accessed: ['PUBLIC'],
    write_targets: [],
    approval_threshold: 'NONE',
    input_schema_ref: 'firstdue.schemas.SourcePollRequest',
    output_schema_ref: 'firstdue.schemas.FactBatch',
    latency_target_ms: 120000,
    published_at: '2026-08-20T08:00:00+00:00',
    deprecated_at: null,
  },
  {
    agent_id: 'incident-controller',
    version: '1.0.0',
    ref: 'incident-controller@1.0.0',
    publisher_department: 'fire',
    loop: 'INCIDENT',
    role_summary: 'Opens the incident, loads one profile snapshot, and streams the brief.',
    capabilities: ['READ'],
    required_scopes: ['read:profile'],
    classifications_accessed: ['PUBLIC', 'PHI'],
    write_targets: [],
    approval_threshold: 'NONE',
    input_schema_ref: 'firstdue.schemas.DispatchEvent',
    output_schema_ref: 'firstdue.schemas.BriefEmission',
    latency_target_ms: 500,
    published_at: '2026-08-20T08:00:00+00:00',
    deprecated_at: null,
  },
];

export const SUBSCRIPTIONS: SubscriptionView[] = [
  {
    subscription_id: 'sub_fire_records-watcher',
    subscriber_department: 'fire',
    agent_id: 'records-watcher',
    pinned_version: '1.0.0',
    ref: 'records-watcher@1.0.0',
    subscribed_at: '2026-08-20T08:00:00+00:00',
    unsubscribed_at: null,
  },
];

export const EVENTS: AuditEventView[] = [
  {
    audit_id: 'audit-1',
    kind: 'injection_blocked',
    occurred_at: '2026-08-20T08:00:01+00:00',
    actor: 'records-watcher',
    target: 'sf-fire-inspections',
    correlation_id: 'corr-1',
    detail: { patterns: 'instruction-override', screen: 'local-injection-detector/1' },
  },
  {
    audit_id: 'audit-2',
    kind: 'write_executed',
    occurred_at: '2026-08-20T08:00:02+00:00',
    actor: 'referral-clerk',
    target: 'building-referral-intake',
    correlation_id: 'corr-1',
    detail: { external_ref: 'REF-00001' },
  },
];

export const DECISIONS: PolicyDecisionView[] = [
  {
    decision_id: 'decision-1',
    agent_id: 'agency-notifier',
    target: 'agency-notifications',
    operation: 'NOTIFY',
    classification: 'PUBLIC',
    action: 'REQUIRE_APPROVAL',
    rule_id: 'approval.required',
    justification: 'write:utility-shutoff commits resources outside this agent’s authority.',
    policy_version: '1.0.0',
    decided_at: '2026-08-20T08:00:03+00:00',
    decided_by: 'deterministic-policy-engine',
  },
  {
    decision_id: 'decision-2',
    agent_id: 'incident-controller',
    target: 'profile',
    operation: 'READ',
    classification: 'PUBLIC',
    action: 'ALLOW',
    rule_id: 'read.scope-held',
    justification: 'the grant carries read:profile for this classification',
    policy_version: '1.0.0',
    decided_at: '2026-08-20T08:00:04+00:00',
    decided_by: 'deterministic-policy-engine',
  },
];

export function emission(overrides: Partial<BriefEmissionView> = {}): BriefEmissionView {
  return {
    emission_id: 'emission-1',
    incident_id: 'inc-1',
    version: 1,
    stage: 'INSTANT',
    sections: [
      {
        key: 'CONSTRUCTION',
        items: [
          {
            label: 'structure.stories',
            value_render: '2',
            status: 'DISPUTED',
            canonical_key: 'structure.stories',
            fact_id: 'fact_a',
            provenance: 'PERMIT',
            derivation_note: null,
            withheld_note: null,
            reported_note: null,
          },
        ],
      },
      {
        key: 'AUXILIARY_APPLIANCES',
        items: [
          {
            label: 'suppression.sprinklered',
            value_render: 'UNKNOWN - no record found',
            status: 'UNKNOWN',
            canonical_key: 'suppression.sprinklered',
            fact_id: null,
            provenance: null,
            derivation_note: null,
            withheld_note: null,
            reported_note: null,
          },
        ],
      },
    ],
    unknowns: ['suppression.sprinklered'],
    unavailable: [],
    withheld: [],
    conflict_ids: ['conflict_0c93'],
    narrative: null,
    narrative_available: false,
    model_invoked: false,
    profile_snapshot_id: 'snap_abc',
    agent_versions: { 'incident-controller': '1.0.0' },
    produced_at: '2026-08-20T08:00:00+00:00',
    persisted_at: '2026-08-20T08:00:00.100+00:00',
    content_hash: 'abcdef1234567890',
    ...overrides,
  };
}

export const INCIDENT: OpenIncidentResponse = {
  incident_id: 'inc-1',
  address_id: ADDRESS,
  address_display: '450 Hayes St, San Francisco, CA 94102',
  profile_snapshot_id: 'snap_abc',
  grant_id: 'grant-1',
  cold_start: false,
  dispatched_at: new Date().toISOString(),
  elapsed_seconds: 0,
  brief: emission(),
  instant_brief_ms: 0.19,
  event_id: 'evt-1',
  intake: null,
};

/** A 911 call that reported two things and woke two agents.
 *
 * The offsets index into `SAMPLE_NARRATIVE` below, so a test can assert the
 * console actually checks a quote rather than trusting the backend's word.
 */
export const SAMPLE_NARRATIVE =
  'Caller reports heavy smoke on the third floor. Two people are still inside.';

export const INTAKE: IntakeResponse = {
  incident_id: 'inc-1',
  channel: 'CALL_911',
  source_ref: 'call/inc-1',
  accepted: true,
  rejection_reason: null,
  model_ref: 'vertex/gemini-3.5-flash',
  screened: true,
  screen: 'local+armor',
  screen_findings: [],
  reported: [
    {
      intake_key: 'intake.people_trapped',
      reported_value: '2',
      quoted_text: 'Two people are still inside.',
      start_offset: 47,
      end_offset: 75,
    },
  ],
  unknowns: ['intake.access_obstruction'],
  brief_version: 2,
  woken: [
    {
      agent_ref: 'sensor-fusion@1.0.0',
      intake_keys: ['intake.entrapment_reported'],
      rule_ids: ['route.people-reported-inside'],
      started: true,
    },
  ],
  fired_rule_ids: ['route.people-reported-inside'],
  unmatched_rule_ids: [],
  withheld: [],
};


export const LOG: IncidentLogView = {
  incident_id: 'inc-1',
  sealed_at: '2026-08-20T09:00:00+00:00',
  entries: [
    {
      sequence: 0,
      entry_type: 'BENCHMARK',
      occurred_at: '2026-08-20T08:00:00+00:00',
      profile_snapshot_id: 'snap_abc',
      content_hash: 'hash0000',
      written_to_rms_at: null,
      content: {},
    },
    {
      sequence: 1,
      entry_type: 'BRIEF_EMITTED',
      occurred_at: '2026-08-20T08:00:01+00:00',
      profile_snapshot_id: 'snap_abc',
      content_hash: 'abcdef1234567890',
      written_to_rms_at: null,
      content: { content_hash: 'abcdef1234567890', version: 1 },
    },
  ],
  unflushed: 2,
};


/**
 * The same geometry after a drone pass on Alpha: hot cockloft over a cool
 * ground floor. Kept separate from `GEOMETRY` on purpose -- the default
 * fixture is the standby state where nothing has been flown, and the
 * never-colour-alone test depends on all four faces being UNSCANNED there.
 */
export const GEOMETRY_SCANNED: GeometryView = {
  ...GEOMETRY,
  spec: {
    ...GEOMETRY.spec,
    faces: GEOMETRY.spec.faces.map((face) =>
      face.label === 'ALPHA'
        ? {
            ...face,
            thermal: { kind: 'QUANTITY' as const, magnitude: 340, unit: 'C' },
            observed_at: '2026-08-20T08:12:00+00:00',
            thermal_cells: [
              { u_from: 0.05, u_to: 0.95, v_from: 0.0, v_to: 0.33, temperature_c: 22 },
              { u_from: 0.05, u_to: 0.95, v_from: 0.33, v_to: 0.66, temperature_c: 120 },
              { u_from: 0.05, u_to: 0.95, v_from: 0.66, v_to: 0.95, temperature_c: 340 },
            ],
          }
        : face,
    ),
  },
};
