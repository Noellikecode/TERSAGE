/**
 * The console's types, checked against the backend's OpenAPI document.
 *
 * The types in `lib/api/types.ts` are hand-written so the console fails to
 * compile when the contract changes. This test is the other half of that: it
 * asserts the paths the console actually calls exist in `docs/openapi.json`,
 * and that the response fields the console reads are declared.
 *
 * A renamed backend field is then a failing test here rather than an `undefined`
 * on a fireground.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

interface OpenApiDocument {
  paths: Record<string, Record<string, unknown>>;
  components?: { schemas?: Record<string, { properties?: Record<string, unknown> }> };
}

const OPENAPI: OpenApiDocument = JSON.parse(
  readFileSync(join(process.cwd(), '..', 'docs', 'openapi.json'), 'utf-8'),
);

/** Every path the console calls, with the method it uses. */
const CALLED: Array<[string, string]> = [
  ['/api/v1/system/status', 'get'],
  ['/readyz', 'get'],
  ['/healthz', 'get'],
  ['/api/v1/districts/{district_id}/stats', 'get'],
  ['/api/v1/districts/{district_id}/queue', 'get'],
  ['/api/v1/buildings/{address_id}', 'get'],
  ['/api/v1/buildings/{address_id}/timeline', 'get'],
  ['/api/v1/buildings/{address_id}/geometry', 'get'],
  ['/api/v1/registry/agents', 'get'],
  ['/api/v1/registry/subscriptions', 'get'],
  ['/api/v1/internal/audit/events', 'get'],
  ['/api/v1/internal/audit/decisions', 'get'],
  ['/api/v1/incidents', 'post'],
  ['/api/v1/incidents/{incident_id}/brief/enrich', 'post'],
  ['/api/v1/incidents/{incident_id}/stream', 'get'],
  ['/api/v1/incidents/{incident_id}/log', 'get'],
  ['/api/v1/incidents/{incident_id}/resolutions', 'post'],
  ['/api/v1/incidents/{incident_id}/thermal', 'post'],
  ['/api/v1/incidents/{incident_id}/resources', 'post'],
  ['/api/v1/incidents/{incident_id}/approvals/{approval_id}', 'post'],
  ['/api/v1/incidents/{incident_id}/close', 'post'],
  ['/api/v1/conflicts/{conflict_id}/referral', 'post'],
  ['/api/v1/referrals/{referral_id}/approve', 'post'],
  ['/api/v1/internal/audit/incidents/{incident_id}/replay', 'get'],
];

describe('the console calls paths the backend actually serves', () => {
  it.each(CALLED)('%s %s exists in the OpenAPI document', (path, method) => {
    expect(OPENAPI.paths[path], `missing path ${path}`).toBeDefined();
    expect(OPENAPI.paths[path]?.[method], `missing ${method} on ${path}`).toBeDefined();
  });
});

describe('the fields the console reads are declared', () => {
  function properties(schema: string): string[] {
    const found = OPENAPI.components?.schemas?.[schema]?.properties;
    expect(found, `schema ${schema} is missing`).toBeDefined();
    return Object.keys(found ?? {});
  }

  it('SystemStatus carries the mode and backend fields the header shows', () => {
    const fields = properties('SystemStatus');
    for (const field of [
      'mode',
      'storage_backend',
      'event_backend',
      'municipality_id',
      'districts',
      'published_agents',
      'disclosure',
    ]) {
      expect(fields).toContain(field);
    }
  });

  it('DistrictStatsView carries every metric on the strip', () => {
    const fields = properties('DistrictStatsView');
    for (const field of [
      'profiles',
      'facts',
      'open_conflicts',
      'high_severity_conflicts',
      'queued_for_survey',
      'dispatched',
      'profiles_never_surveyed',
      'sources',
    ]) {
      expect(fields).toContain(field);
    }
  });

  it('a queue row carries the reasons the console renders inline', () => {
    expect(properties('QueueEntryView')).toContain('reasons');
    const reason = properties('RankReasonView');
    expect(reason).toContain('rule_id');
    expect(reason).toContain('detail');
    expect(reason).toContain('weight');
  });

  it('a fact carries its provenance and assertion state', () => {
    const fields = properties('FactView');
    for (const field of [
      'canonical_key',
      'value',
      'status',
      'source_type',
      'observed_at',
      'decayed_confidence',
      'all_fact_ids',
    ]) {
      expect(fields).toContain(field);
    }
  });

  it('geometry carries the SVG fallback and the disputed-mass flag', () => {
    const fields = properties('GeometryView');
    expect(fields).toContain('spec');
    expect(fields).toContain('svg');
    expect(fields).toContain('has_disputed_mass');
  });

  it('an incident response carries the persisted instant brief', () => {
    const fields = properties('OpenIncidentResponse');
    for (const field of [
      'incident_id',
      'profile_snapshot_id',
      'cold_start',
      'brief',
      'instant_brief_ms',
    ]) {
      expect(fields).toContain(field);
    }
  });
  it('IntakeResponse carries everything the intake panel renders', () => {
    const fields = properties('IntakeResponse');
    for (const field of [
      'accepted',
      'rejection_reason',
      'channel',
      'model_ref',
      'screen_findings',
      'reported',
      'unknowns',
      'woken',
      'fired_rule_ids',
      'withheld',
    ]) {
      expect(fields).toContain(field);
    }
  });

  it('ReportedLine carries the offsets that make a quote checkable', () => {
    const fields = properties('ReportedLine');
    for (const field of ['intake_key', 'reported_value', 'quoted_text', 'start_offset', 'end_offset']) {
      expect(fields).toContain(field);
    }
  });

  it('IncidentReplayView carries what an investigator has to be able to see', () => {
    const fields = properties('IncidentReplayView');
    for (const field of [
      'intact',
      'tampered_sequences',
      'digest',
      'agent_versions',
      'policy_versions',
      'profile_snapshot_id',
      'snapshot_available',
      'sealed_at',
    ]) {
      expect(fields).toContain(field);
    }
  });
});
