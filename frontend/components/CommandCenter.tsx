'use client';

/**
 * The command center. One screen, two modes, no navigation between them.
 *
 * A dispatch does not take an officer somewhere else -- the standby view
 * compresses and the incident surfaces expand in place. That is deliberate:
 * losing the district context at the moment a fire starts is exactly when
 * losing it costs the most, and a page transition is a moment where a tablet on
 * a bad connection can show nothing at all.
 *
 * Everything on screen comes from the backend. Where the backend reports
 * nothing, the console says so rather than inventing a row.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AuditConsole } from '@/components/audit/AuditConsole';
import { BackendStatus } from '@/components/BackendStatus';
import { GeometryCanvas, type ViewAngle } from '@/components/GeometryCanvas';
import { BriefPanel, announcementFor } from '@/components/incident/BriefPanel';
import { IncidentBanner } from '@/components/incident/IncidentBanner';
import { ResourcePanel } from '@/components/incident/ResourcePanel';
import { ThermalPanel } from '@/components/incident/ThermalPanel';
import { AttributeGrid } from '@/components/profile/AttributeGrid';
import { ConflictPanel, type ResolutionSubmission } from '@/components/profile/ConflictPanel';
import { Timeline } from '@/components/profile/Timeline';
import { ActivityStream, toStreamItems } from '@/components/standby/ActivityStream';
import { AgentRail } from '@/components/standby/AgentRail';
import { DistrictStrip } from '@/components/standby/DistrictStrip';
import { SurveyQueue } from '@/components/standby/SurveyQueue';
import { StatusPill } from '@/components/StatusPill';
import { browserGet, browserPost } from '@/lib/api/client';
import { useBriefStream } from '@/lib/api/stream';
import type {
  AgentDescriptorView,
  AgentListResponse,
  AuditEventView,
  BriefEmissionView,
  BuildingProfileView,
  CloseIncidentResponse,
  DistrictStatsView,
  GeometryView,
  IncidentLogView,
  OpenIncidentResponse,
  PolicyDecisionView,
  QueueView,
  Readiness,
  ResolutionResponse,
  ResourceOutcomeView,
  SubscriptionListResponse,
  SubscriptionView,
  SystemStatus,
  TimelineEventView,
} from '@/lib/api/types';

const VIEWS: ViewAngle[] = ['ISO', 'ALPHA', 'BRAVO', 'CHARLIE', 'DELTA'];

export interface CommandCenterProps {
  status: SystemStatus | null;
  readiness: Readiness | null;
  error: string | null;
  /** Injected by tests and by the server render; fetched otherwise. */
  initialStats?: DistrictStatsView | null;
  initialQueue?: QueueView | null;
  initialAgents?: AgentDescriptorView[];
  initialSubscriptions?: SubscriptionView[];
  initialEvents?: AuditEventView[];
  initialDecisions?: PolicyDecisionView[];
  /** Set by the WebGL-disabled test path. */
  forceSvgGeometry?: boolean;
}

export function CommandCenter({
  status,
  readiness,
  error,
  initialStats = null,
  initialQueue = null,
  initialAgents = [],
  initialSubscriptions = [],
  initialEvents = [],
  initialDecisions = [],
  forceSvgGeometry = false,
}: CommandCenterProps) {
  const districtId = status?.districts[0] ?? 'sffd-district-03';

  const [stats, setStats] = useState<DistrictStatsView | null>(initialStats);
  const [queue, setQueue] = useState<QueueView | null>(initialQueue);
  const [agents] = useState<AgentDescriptorView[]>(initialAgents);
  const [subscriptions, setSubscriptions] = useState<SubscriptionView[]>(initialSubscriptions);
  const [events, setEvents] = useState<AuditEventView[]>(initialEvents);
  const [decisions, setDecisions] = useState<PolicyDecisionView[]>(initialDecisions);
  const [agentList, setAgentList] = useState<AgentDescriptorView[]>(initialAgents);

  const [selected, setSelected] = useState<string | null>(null);
  const [profile, setProfile] = useState<BuildingProfileView | null>(null);
  const [timeline, setTimeline] = useState<TimelineEventView[]>([]);
  const [geometry, setGeometry] = useState<GeometryView | null>(null);
  const [view, setView] = useState<ViewAngle>('ISO');

  const [incident, setIncident] = useState<OpenIncidentResponse | null>(null);
  const [outcomes, setOutcomes] = useState<ResourceOutcomeView[]>([]);
  const [log, setLog] = useState<IncidentLogView | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState('');

  const stream = useBriefStream(incident?.incident_id ?? null);
  const announcedRef = useRef<number>(0);

  // The brief the officer is looking at: whatever arrived last on the stream,
  // falling back to the instant brief the open call already returned.
  const emissions: BriefEmissionView[] = useMemo(() => {
    if (stream.emissions.length > 0) return stream.emissions;
    return incident ? [incident.brief] : [];
  }, [stream.emissions, incident]);
  const latest = emissions[emissions.length - 1] ?? null;

  // Announce each new version once, politely. An officer who cannot see the
  // version tick still hears that the brief changed.
  useEffect(() => {
    if (!latest || latest.version === announcedRef.current) return;
    announcedRef.current = latest.version;
    setAnnouncement(announcementFor(latest));
  }, [latest]);

  const refreshStandby = useCallback(async () => {
    const [statsResult, queueResult, eventsResult, decisionsResult] = await Promise.all([
      browserGet<DistrictStatsView>(`/api/v1/districts/${districtId}/stats`),
      browserGet<QueueView>(`/api/v1/districts/${districtId}/queue`),
      browserGet<AuditEventView[]>('/api/v1/internal/audit/events?limit=60'),
      browserGet<PolicyDecisionView[]>('/api/v1/internal/audit/decisions?limit=60'),
    ]);
    if (statsResult.ok) setStats(statsResult.data);
    if (queueResult.ok) setQueue(queueResult.data);
    if (eventsResult.ok) setEvents(eventsResult.data);
    if (decisionsResult.ok) setDecisions(decisionsResult.data);
  }, [districtId]);

  useEffect(() => {
    if (initialAgents.length > 0) return;
    void (async () => {
      const [agentsResult, subsResult] = await Promise.all([
        browserGet<AgentListResponse>('/api/v1/registry/agents'),
        browserGet<SubscriptionListResponse>(
          '/api/v1/registry/subscriptions?subscriber_department=fire',
        ),
      ]);
      if (agentsResult.ok) setAgentList(agentsResult.data.agents);
      if (subsResult.ok) setSubscriptions(subsResult.data.subscriptions);
      await refreshStandby();
    })();
  }, [initialAgents.length, refreshStandby]);

  const openProfile = useCallback(async (addressId: string) => {
    setSelected(addressId);
    const [profileResult, timelineResult, geometryResult] = await Promise.all([
      browserGet<BuildingProfileView>(`/api/v1/buildings/${addressId}`),
      browserGet<TimelineEventView[]>(`/api/v1/buildings/${addressId}/timeline`),
      browserGet<GeometryView>(`/api/v1/buildings/${addressId}/geometry`),
    ]);
    setProfile(profileResult.ok ? profileResult.data : null);
    setTimeline(timelineResult.ok ? timelineResult.data : []);
    setGeometry(geometryResult.ok ? geometryResult.data : null);
  }, []);

  const dispatch = useCallback(
    async (addressId: string) => {
      setBusy(true);
      setNotice(null);
      const result = await browserPost<OpenIncidentResponse>('/api/v1/incidents', {
        address: addressId,
        cad_ref: `CAD-${Date.now().toString().slice(-6)}`,
        alarm_level: 2,
      });
      setBusy(false);
      if (!result.ok) {
        setNotice(`Could not open an incident: ${result.error.message}`);
        return;
      }
      setIncident(result.data);
      setOutcomes([]);
      announcedRef.current = 0;
      await openProfile(result.data.address_id);
      // Prose is asked for only after the instant brief is on screen.
      void browserPost(`/api/v1/incidents/${result.data.incident_id}/brief/enrich`);
    },
    [openProfile],
  );

  const resolve = useCallback(
    async (submission: ResolutionSubmission) => {
      if (!incident) return;
      setBusy(true);
      const result = await browserPost<ResolutionResponse>(
        `/api/v1/incidents/${incident.incident_id}/resolutions`,
        {
          conflict_id: submission.conflictId,
          observed_value: submission.observedValue,
          resolved_by: submission.resolvedBy,
          note: submission.note,
        },
      );
      setBusy(false);
      if (!result.ok) {
        setNotice(`Could not record the observation: ${result.error.message}`);
        return;
      }
      setNotice(
        `Recorded. Profile is now version ${result.data.profile_version}; brief amended to version ${result.data.brief_version}.`,
      );
      await openProfile(incident.address_id);
    },
    [incident, openProfile],
  );

  const requestResource = useCallback(
    async (kindId: string) => {
      if (!incident) return;
      setBusy(true);
      const result = await browserPost<ResourceOutcomeView>(
        `/api/v1/incidents/${incident.incident_id}/resources`,
        { kind_id: kindId },
      );
      setBusy(false);
      if (!result.ok) {
        setNotice(`Request refused: ${result.error.message}`);
        return;
      }
      setOutcomes((current) => [...current.filter((o) => o.kind_id !== kindId), result.data]);
    },
    [incident],
  );

  const approve = useCallback(
    async (approvalId: string) => {
      if (!incident) return;
      setBusy(true);
      const result = await browserPost<Record<string, unknown>>(
        `/api/v1/incidents/${incident.incident_id}/approvals/${approvalId}`,
      );
      setBusy(false);
      if (!result.ok) {
        setNotice(`Approval failed: ${result.error.message}`);
        return;
      }
      setOutcomes((current) =>
        current.map((outcome) =>
          outcome.approval_id === approvalId
            ? {
                ...outcome,
                action: 'ALLOW',
                external_ref: String(result.data.external_ref ?? ''),
              }
            : outcome,
        ),
      );
    },
    [incident],
  );

  const registerThermal = useCallback(
    async (face: string) => {
      if (!incident) return;
      setBusy(true);
      await browserPost(`/api/v1/incidents/${incident.incident_id}/thermal`, {
        face,
        // Recorded footage, never presented as a live flight.
        region_temps_c: [21, 24, 96],
        coverage: 0.8,
        source: 'recorded',
      });
      setBusy(false);
      await openProfile(incident.address_id);
    },
    [incident, openProfile],
  );

  const closeIncident = useCallback(async () => {
    if (!incident) return;
    setBusy(true);
    const logResult = await browserGet<IncidentLogView>(
      `/api/v1/incidents/${incident.incident_id}/log`,
    );
    const result = await browserPost<CloseIncidentResponse>(
      `/api/v1/incidents/${incident.incident_id}/close`,
      { closed_by: 'bc-09' },
    );
    setBusy(false);
    if (logResult.ok) setLog(logResult.data);
    if (!result.ok) {
      setNotice(`Could not close the incident: ${result.error.message}`);
      return;
    }
    setNotice(
      `Incident closed. Grant revoked, log sealed with ${result.data.log_entries} entries.`,
    );
    setIncident(null);
    // Back to standby, updated: the resolution and the survey both landed.
    await refreshStandby();
    await openProfile(incident.address_id);
  }, [incident, openProfile, refreshStandby]);

  const streamItems = useMemo(() => toStreamItems(events, decisions), [events, decisions]);
  const railAgents = agents.length > 0 ? agents : agentList;

  return (
    <div className="flex min-h-screen flex-col bg-ground text-ink">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:border focus:border-live focus:bg-surface focus:px-3 focus:py-1"
      >
        Skip to main content
      </a>

      <header className="border-b border-line bg-surface px-4 py-3">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <div className="flex items-baseline gap-3">
            <h1 className="text-base font-semibold tracking-widest text-ink">FIRST DUE</h1>
            <span className="text-micro uppercase tracking-wide text-muted">Command Center</span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {status ? (
              <>
                <StatusPill
                  tone={status.mode === 'live' ? 'live' : 'muted'}
                  label={`${status.mode} mode`}
                  title={
                    status.mode === 'fake'
                      ? 'Deterministic adapters, no credentials required'
                      : 'Live Google-backed adapters'
                  }
                />
                <StatusPill tone="muted" label={`store: ${status.storage_backend}`} />
                <StatusPill tone="muted" label={`events: ${status.event_backend}`} />
                {status.mode === 'live' && status.workspace_writes === 'fake' ? (
                  <StatusPill
                    tone="disputed"
                    label="calendar + mail: simulated"
                    title={
                      'Calendar and Gmail act as a user, which needs delegated ' +
                      'Workspace authority this deployment does not hold. Both ' +
                      'actions are recorded and audited; neither is sent.'
                    }
                  />
                ) : null}
                <span className="text-micro text-muted">{status.municipality_id}</span>
                <span className="text-micro text-muted">v{status.version}</span>
              </>
            ) : (
              <StatusPill tone="alarm" label="No backend status" />
            )}
          </div>
        </div>
        <div className="mt-3">
          <BackendStatus initial={readiness ?? undefined} />
        </div>
      </header>

      {incident && (
        <IncidentBanner
          incidentId={incident.incident_id}
          addressId={incident.address_id}
          alarmLevel={2}
          dispatchedAt={incident.dispatched_at}
          coldStart={incident.cold_start}
          onClose={closeIncident}
          closing={busy}
        />
      )}

      {/* Stage and amendment announcements. Polite: it must not interrupt. */}
      <p aria-live="polite" aria-atomic="true" className="sr-only" data-testid="brief-announcer">
        {announcement}
      </p>
      {notice && (
        <p role="status" className="border-b border-line bg-raised px-4 py-2 text-micro text-ink">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="border-b border-alarm bg-raised px-4 py-2 text-micro text-alarm">
          {error}
        </p>
      )}
      {stream.state === 'reconnecting' && (
        <p role="status" className="border-b border-line bg-raised px-4 py-1 text-micro text-disputed">
          Stream reconnecting. Versions already received stay on screen; missed
          ones replay from the log.
        </p>
      )}

      <main
        id="main"
        className="grid flex-1 grid-cols-1 gap-px bg-line lg:grid-cols-[300px_1fr_340px]"
      >
        {/* Left: the fleet. Compresses during an incident, never disappears. */}
        <section aria-labelledby="fleet-heading" className="bg-ground p-4">
          <h2 id="fleet-heading" className="mb-3 text-micro uppercase tracking-widest text-muted">
            {incident ? 'Slow loop — still running' : 'Fleet'}
          </h2>
          <AgentRail
            agents={railAgents}
            subscriptions={subscriptions}
            compressed={Boolean(incident)}
            loop={incident ? 'SLOW' : undefined}
          />
          {incident && (
            <>
              <h2 className="mb-2 mt-4 text-micro uppercase tracking-widest text-muted">
                Incident agents
              </h2>
              <AgentRail agents={railAgents} subscriptions={subscriptions} loop="INCIDENT" />
            </>
          )}
        </section>

        {/* Centre: the queue in standby, the brief and structure at dispatch. */}
        <section aria-labelledby="centre-heading" className="min-w-0 bg-ground p-4">
          <h2 id="centre-heading" className="sr-only">
            {incident ? 'Incident brief' : 'District and survey queue'}
          </h2>

          {!incident && (
            <>
              <DistrictStrip stats={stats} />
              <div className="mt-4 flex flex-wrap items-baseline justify-between gap-2">
                <h3 className="text-micro uppercase tracking-widest text-muted">Survey queue</h3>
                {queue && <span className="text-micro text-muted">{queue.count} ranked</span>}
              </div>
              <div className="mt-2">
                <SurveyQueue
                  entries={queue?.entries ?? []}
                  onSelect={openProfile}
                  selectedAddressId={selected}
                />
              </div>
            </>
          )}

          {incident && <BriefPanel emission={latest} />}

          {(geometry || incident) && (
            <div className="mt-4">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <h3 className="text-micro uppercase tracking-widest text-muted">Structure</h3>
                <div className="flex flex-wrap gap-1" role="group" aria-label="Fixed camera views">
                  {VIEWS.map((angle) => (
                    <button
                      key={angle}
                      type="button"
                      aria-pressed={view === angle}
                      onClick={() => setView(angle)}
                      className={`border px-2 py-0.5 text-micro uppercase tracking-wide focus-visible:outline focus-visible:outline-2 focus-visible:outline-live ${
                        view === angle ? 'border-live text-live' : 'border-line text-muted'
                      }`}
                    >
                      {angle}
                    </button>
                  ))}
                </div>
              </div>
              <GeometryCanvas
                geometry={geometry}
                view={view}
                forceFallback={forceSvgGeometry}
              />
            </div>
          )}
        </section>

        {/* Right: the profile in standby, resources and thermal at dispatch. */}
        <section aria-labelledby="right-heading" className="min-w-0 bg-ground p-4">
          <h2 id="right-heading" className="mb-3 text-micro uppercase tracking-widest text-muted">
            {incident ? 'Resources and conditions' : 'Activity'}
          </h2>

          {incident ? (
            <div className="space-y-4">
              <ResourcePanel
                outcomes={outcomes}
                onRequest={requestResource}
                onApprove={approve}
                busy={busy}
              />
              <ThermalPanel
                faces={geometry?.spec.faces ?? []}
                onRegister={registerThermal}
                busy={busy}
              />
            </div>
          ) : (
            <ActivityStream items={streamItems} />
          )}
        </section>
      </main>

      {profile && (
        <section aria-labelledby="profile-heading" className="border-t border-line bg-ground p-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 id="profile-heading" className="font-mono text-ink">
              {profile.address_id}
              <span className="ml-2 text-micro text-muted">
                profile v{profile.profile_version}
              </span>
            </h2>
            {!incident && (
              <button
                type="button"
                disabled={busy}
                onClick={() => dispatch(profile.address_id)}
                className="border border-alarm px-3 py-1 text-micro uppercase tracking-wide text-alarm disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
              >
                Simulate CAD dispatch
              </button>
            )}
          </div>

          <div className="mt-3 grid gap-4 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <AttributeGrid facts={profile.facts} unknownKeys={profile.unknown_keys} />
            </div>
            <div className="space-y-4">
              <ConflictPanel
                conflicts={profile.conflicts}
                referrals={profile.open_referrals}
                onResolve={resolve}
                busy={busy}
                disabledReason={
                  incident
                    ? undefined
                    : 'An observation is recorded during an incident 360. Open an incident to settle this on scene.'
                }
              />
              <div>
                <h3 className="text-micro uppercase tracking-widest text-muted">Timeline</h3>
                <div className="mt-2">
                  <Timeline events={timeline} />
                </div>
              </div>
            </div>
          </div>
        </section>
      )}

      <section aria-labelledby="audit-heading" className="border-t border-line bg-ground p-4">
        <h2 id="audit-heading" className="mb-3 text-micro uppercase tracking-widest text-muted">
          Audit
        </h2>
        <AuditConsole events={events} decisions={decisions} log={log} emissions={emissions} />
      </section>

      <footer className="border-t border-line bg-surface px-4 py-3 text-micro leading-5 text-muted">
        {status?.disclosure ??
          'Decision-support prototype, not a certified public-safety system.'}
      </footer>
    </div>
  );
}
