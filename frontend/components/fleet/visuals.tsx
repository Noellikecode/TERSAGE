/**
 * One glyph per agent, because a rail of identical rows tells you nothing.
 *
 * Each visual answers the question that agent exists to answer, drawn from the
 * data the console already holds. Inline SVG only -- no chart library, nothing
 * fetched, nothing animated. Under 60px including the caption, because nine of
 * these share one column with nine terminals.
 *
 * The caption is not a legend. It carries the numbers in words, so the glyph
 * is never the only way to read the state, and it says "not reported" out loud
 * where the console has no data rather than drawing a hopeful zero.
 */

import type {
  AudienceRow,
  FanOutLine,
  FaceQuadrant,
  Ledger,
  Massing,
  Pass,
  Pipeline,
  RegistryPip,
} from '@/components/fleet/derive';
import { RANK_WEIGHTS } from '@/components/fleet/derive';

const VIEW_BOX = '0 0 240 40';

function Frame({
  agentId,
  kind,
  caption,
  children,
}: {
  agentId: string;
  kind: string;
  caption: string;
  children: React.ReactNode;
}) {
  return (
    <figure
      className="mt-2"
      data-testid={`fleet-visual-${agentId}`}
      data-visual={kind}
    >
      <svg
        viewBox={VIEW_BOX}
        preserveAspectRatio="xMinYMid meet"
        className="block h-10 w-full"
        aria-hidden="true"
        focusable="false"
      >
        {children}
      </svg>
      <figcaption className="mt-0.5 text-micro leading-4 text-muted">{caption}</figcaption>
    </figure>
  );
}

/** The shared "there is nothing to draw" body: a dashed rule, not a zero. */
function Absent() {
  return (
    <line
      x1="0"
      y1="20"
      x2="240"
      y2="20"
      strokeDasharray="4 4"
      strokeWidth="1"
      className="stroke-line"
    />
  );
}

// ------------------------------------------------------- records-watcher --

export function PassSpark({ agentId, passes }: { agentId: string; passes: Pass[] }) {
  const shown = passes.slice(Math.max(0, passes.length - 12));
  if (shown.length === 0) {
    return (
      <Frame agentId={agentId} kind="passes" caption="No polling pass recorded this session.">
        <Absent />
      </Frame>
    );
  }
  const peak = Math.max(...shown.map((pass) => pass.count));
  const slot = 240 / shown.length;
  const width = Math.max(4, slot - 4);
  const total = shown.reduce((sum, pass) => sum + pass.count, 0);
  return (
    <Frame
      agentId={agentId}
      kind="passes"
      caption={
        `${shown.length} correlated pass${shown.length === 1 ? '' : 'es'} · ` +
        `${total} event${total === 1 ? '' : 's'} recorded · peak ${peak}`
      }
    >
      <line x1="0" y1="38" x2="240" y2="38" strokeWidth="1" className="stroke-line" />
      {shown.map((pass, index) => {
        const height = Math.max(3, Math.round((pass.count / peak) * 32));
        return (
          <rect
            key={pass.correlationId}
            x={index * slot}
            y={38 - height}
            width={width}
            height={height}
            className="fill-live"
          />
        );
      })}
    </Frame>
  );
}

// -------------------------------------------------------- hazard-watcher --

const PIP_GLYPH: Record<RegistryPip['state'], string> = {
  reached: '●',
  unreachable: '✕',
  unreported: '·',
};

export function RegistryPipsVisual({ agentId, pips }: { agentId: string; pips: RegistryPip[] }) {
  if (pips.length === 0) {
    return (
      <Frame agentId={agentId} kind="pips" caption="No registries declared for this agent.">
        <Absent />
      </Frame>
    );
  }
  const reached = pips.filter((pip) => pip.state === 'reached').length;
  const unreachable = pips.filter((pip) => pip.state === 'unreachable').length;
  const unreported = pips.filter((pip) => pip.state === 'unreported').length;
  const slot = 240 / pips.length;
  return (
    <Frame
      agentId={agentId}
      kind="pips"
      caption={
        `${reached} of ${pips.length} registries reached, ${unreachable} unreachable` +
        (unreported > 0 ? `, ${unreported} not reported` : '')
      }
    >
      {pips.map((pip, index) => {
        const x = index * slot;
        const width = slot - 6;
        return (
          <g key={pip.sourceId}>
            <rect
              x={x}
              y="2"
              width={width}
              height="16"
              strokeWidth="1"
              strokeDasharray={pip.state === 'unreported' ? '3 3' : undefined}
              className={
                pip.state === 'reached'
                  ? 'fill-confirmed stroke-confirmed'
                  : pip.state === 'unreachable'
                    ? 'fill-transparent stroke-alarm'
                    : 'fill-transparent stroke-line'
              }
            />
            {pip.state === 'unreachable' && (
              <line
                x1={x + 3}
                y1="5"
                x2={x + width - 3}
                y2="15"
                strokeWidth="1.5"
                className="stroke-alarm"
              />
            )}
            <text
              x={x}
              y="32"
              className="fill-muted font-mono"
              style={{ fontSize: '9px' }}
            >
              {PIP_GLYPH[pip.state]} {pip.short}
            </text>
          </g>
        );
      })}
    </Frame>
  );
}

// ------------------------------------------------------ geometry-watcher --

export function MassingGlyph({ agentId, mass }: { agentId: string; mass: Massing | null }) {
  if (!mass || mass.levels.length === 0) {
    return (
      <Frame
        agentId={agentId}
        kind="massing"
        caption="No derived geometry on this screen. Open a structure to see its massing."
      >
        <Absent />
      </Frame>
    );
  }
  const tallest = Math.max(...mass.levels.map((level) => level.heightM), 0.1);
  const unit = Math.min(10, 30 / mass.levels.length);
  const disputed = mass.levels.filter((level) => level.disputed).length;
  return (
    <Frame
      agentId={agentId}
      kind="massing"
      caption={
        `${mass.levels.length} levels · ${mass.totalHeightM.toFixed(1)} m · collapse zone ` +
        `${mass.collapseZoneM.toFixed(1)} m` +
        (disputed > 0 ? ` · ${disputed} disputed level${disputed > 1 ? 's' : ''}` : '')
      }
    >
      {/* Ground line, then the levels stacked up from it. */}
      <line x1="0" y1="36" x2="120" y2="36" strokeWidth="1" className="stroke-line" />
      {mass.levels.map((level, index) => {
        const height = Math.max(4, (level.heightM / tallest) * unit);
        const y = 36 - (index + 1) * unit;
        return (
          <rect
            key={`${index}-${level.heightM}`}
            x="18"
            y={y}
            width="52"
            height={height}
            strokeWidth="1"
            strokeDasharray={level.disputed ? '3 2' : undefined}
            className={
              level.disputed
                ? 'fill-transparent stroke-disputed'
                : 'fill-raised stroke-confirmed'
            }
          />
        );
      })}
      {/* The collapse zone, as the distance it actually is from the wall. */}
      <line
        x1="70"
        y1="36"
        x2={Math.min(230, 70 + mass.collapseZoneM * 4)}
        y2="36"
        strokeWidth="1"
        strokeDasharray="2 3"
        className="stroke-alarm"
      />
      <text x="76" y="20" className="fill-muted font-mono" style={{ fontSize: '9px' }}>
        {mass.totalHeightM.toFixed(1)} m
      </text>
    </Frame>
  );
}

// -------------------------------------------------------- structure-watch --

export function WeightBar({ agentId }: { agentId: string }) {
  const shades = ['opacity-100', 'opacity-75', 'opacity-50', 'opacity-30'];
  let cursor = 0;
  return (
    <Frame
      agentId={agentId}
      kind="weights"
      caption={RANK_WEIGHTS.map((w) => `${w.label} ${w.weight.toFixed(2)}`).join(' · ')}
    >
      {RANK_WEIGHTS.map((signal, index) => {
        const width = signal.weight * 240;
        const x = cursor;
        cursor += width;
        return (
          <g key={signal.ruleId}>
            <rect
              x={x}
              y="2"
              width={width - 2}
              height="16"
              className={`fill-live ${shades[index] ?? 'opacity-30'}`}
            />
            <text
              x={x}
              y="32"
              className="fill-muted font-mono"
              style={{ fontSize: '9px' }}
            >
              {signal.weight.toFixed(2)}
            </text>
          </g>
        );
      })}
    </Frame>
  );
}

// --------------------------------------------------------- referral-clerk --

export function PipelineGlyph({ agentId, pipeline }: { agentId: string; pipeline: Pipeline }) {
  const stages: { label: string; count: number }[] = [
    { label: 'staged', count: pipeline.staged },
    { label: 'approved', count: pipeline.approved },
    { label: 'filed', count: pipeline.filed },
  ];
  const empty = stages.every((stage) => stage.count === 0);
  return (
    <Frame
      agentId={agentId}
      kind="pipeline"
      caption={
        empty
          ? 'No referral staged this session. A captain files them; the agent never does.'
          : stages.map((stage) => `${stage.count} ${stage.label}`).join(' → ')
      }
    >
      {stages.map((stage, index) => {
        const x = index * 82;
        return (
          <g key={stage.label}>
            <rect
              x={x}
              y="2"
              width="64"
              height="18"
              strokeWidth="1"
              strokeDasharray={stage.count === 0 ? '3 3' : undefined}
              className={
                stage.count === 0
                  ? 'fill-transparent stroke-line'
                  : 'fill-raised stroke-confirmed'
              }
            />
            <text
              x={x + 6}
              y="15"
              className="fill-ink font-mono"
              style={{ fontSize: '10px' }}
            >
              {stage.count}
            </text>
            <text x={x} y="33" className="fill-muted font-mono" style={{ fontSize: '9px' }}>
              {stage.label}
            </text>
            {index < stages.length - 1 && (
              <line
                x1={x + 66}
                y1="11"
                x2={x + 80}
                y2="11"
                strokeWidth="1"
                className="stroke-line"
              />
            )}
          </g>
        );
      })}
    </Frame>
  );
}

// --------------------------------------------------- incident-interceptor --

export function FanOutGlyph({ agentId, lines }: { agentId: string; lines: FanOutLine[] }) {
  if (lines.length === 0) {
    return (
      <Frame
        agentId={agentId}
        kind="fanout"
        caption="No incident routed this session. Routing is recorded when a narrative arrives."
      >
        <Absent />
      </Frame>
    );
  }
  const shown = lines.slice(0, 4);
  const started = lines.filter((line) => line.state === 'started').length;
  const withheld = lines.filter((line) => line.state === 'withheld').length;
  const step = 36 / (shown.length + 1);
  return (
    <Frame
      agentId={agentId}
      kind="fanout"
      caption={
        `${started} agent${started === 1 ? '' : 's'} woken` +
        (withheld > 0 ? `, ${withheld} withheld for missing scopes` : '') +
        ` · ${shown.map((line) => line.agentId).join(', ')}`
      }
    >
      <circle cx="8" cy="20" r="4" className="fill-live" />
      {shown.map((line, index) => {
        const y = 4 + step * (index + 1);
        const withheldLine = line.state === 'withheld';
        return (
          <g key={`${line.agentId}-${line.state}`}>
            <path
              d={`M12 20 C 34 20, 34 ${y}, 56 ${y}`}
              fill="none"
              strokeWidth="1"
              strokeDasharray={withheldLine ? '3 3' : undefined}
              className={withheldLine ? 'stroke-disputed' : 'stroke-live'}
            />
            <circle
              cx="58"
              cy={y}
              r="3"
              strokeWidth="1"
              className={
                withheldLine
                  ? 'fill-transparent stroke-disputed'
                  : line.state === 'started'
                    ? 'fill-confirmed stroke-confirmed'
                    : 'fill-transparent stroke-line'
              }
            />
            <text
              x="66"
              y={y + 3}
              className="fill-muted font-mono"
              style={{ fontSize: '8px' }}
            >
              {line.agentId}
              {withheldLine ? ' (withheld)' : ''}
            </text>
          </g>
        );
      })}
    </Frame>
  );
}

// ----------------------------------------------------------- sensor-fusion --

const FACE_GLYPH: Record<FaceQuadrant['state'], string> = {
  scanned: '●',
  unscanned: '·',
  unavailable: '✕',
};

export function FaceQuadrants({
  agentId,
  quadrants,
}: {
  agentId: string;
  quadrants: FaceQuadrant[];
}) {
  if (quadrants.length === 0) {
    return (
      <Frame
        agentId={agentId}
        kind="faces"
        caption="No structure loaded, so no face coverage to report."
      >
        <Absent />
      </Frame>
    );
  }
  const scanned = quadrants.filter((face) => face.state === 'scanned');
  const unavailable = quadrants.filter((face) => face.state === 'unavailable');
  return (
    <Frame
      agentId={agentId}
      kind="faces"
      caption={
        `${scanned.length} of 4 faces scanned` +
        (scanned.length > 0
          ? ` · ${scanned.map((face) => `${face.label.charAt(0)} ${face.reading}`).join(' · ')}`
          : ' · no thermal pass registered') +
        (unavailable.length > 0 ? ` · ${unavailable.length} unavailable` : '')
      }
    >
      {quadrants.map((face, index) => {
        const x = index % 2 === 0 ? 0 : 62;
        const y = index < 2 ? 2 : 21;
        return (
          <g key={face.label}>
            <rect
              x={x}
              y={y}
              width="58"
              height="16"
              strokeWidth="1"
              strokeDasharray={face.state === 'unscanned' ? '3 3' : undefined}
              className={
                face.state === 'scanned'
                  ? 'fill-raised stroke-live'
                  : face.state === 'unavailable'
                    ? 'fill-transparent stroke-alarm'
                    : 'fill-transparent stroke-line'
              }
            />
            <text
              x={x + 4}
              y={y + 12}
              className="fill-muted font-mono"
              style={{ fontSize: '9px' }}
            >
              {FACE_GLYPH[face.state]} {face.label.charAt(0)}
              {face.state === 'scanned' ? ` ${face.reading}` : ''}
            </text>
          </g>
        );
      })}
    </Frame>
  );
}

// --------------------------------------------------------- agency-notifier --

export function AudienceBars({ agentId, rows }: { agentId: string; rows: AudienceRow[] }) {
  if (rows.length === 0) {
    return (
      <Frame
        agentId={agentId}
        kind="audiences"
        caption="No partner contacted this session."
      >
        <Absent />
      </Frame>
    );
  }
  const shown = rows.slice(0, 3);
  const peak = Math.max(
    1,
    ...shown.map((row) => row.sent + row.awaiting + row.refused),
  );
  return (
    <Frame
      agentId={agentId}
      kind="audiences"
      caption={shown
        .map(
          (row) =>
            `${row.target}: ${row.sent} sent` +
            (row.awaiting > 0 ? `, ${row.awaiting} awaiting approval` : '') +
            (row.refused > 0 ? `, ${row.refused} refused` : ''),
        )
        .join(' · ')}
    >
      {shown.map((row, index) => {
        const y = index * 12 + 2;
        const scale = 150 / peak;
        const sent = row.sent * scale;
        const awaiting = row.awaiting * scale;
        const refused = row.refused * scale;
        return (
          <g key={row.target}>
            <text x="0" y={y + 8} className="fill-muted font-mono" style={{ fontSize: '8px' }}>
              {row.target.slice(0, 14)}
            </text>
            <rect x="86" y={y} width={sent} height="9" className="fill-confirmed" />
            <rect x={86 + sent} y={y} width={awaiting} height="9" className="fill-disputed" />
            <rect
              x={86 + sent + awaiting}
              y={y}
              width={refused}
              height="9"
              className="fill-alarm"
            />
          </g>
        );
      })}
    </Frame>
  );
}

// -------------------------------------------------------- incident-recorder --

export function LedgerMeter({ agentId, ledger }: { agentId: string; ledger: Ledger }) {
  if (ledger.passes === 0) {
    return (
      <Frame
        agentId={agentId}
        kind="ledger"
        caption="No flush recorded this session. Questions closed are not reported to this console."
      >
        <Absent />
      </Frame>
    );
  }
  const ticks = Math.min(24, Math.max(ledger.attempted, 1));
  const filled = Math.round((ledger.flushed / Math.max(ledger.attempted, 1)) * ticks);
  const slot = 240 / ticks;
  return (
    <Frame
      agentId={agentId}
      kind="ledger"
      caption={
        `${ledger.flushed} of ${ledger.attempted} entries written through in ${ledger.passes} ` +
        `flush${ledger.passes === 1 ? '' : 'es'} · questions closed not reported`
      }
    >
      {Array.from({ length: ticks }, (_, index) => (
        <rect
          key={index}
          x={index * slot}
          y="4"
          width={Math.max(2, slot - 3)}
          height="14"
          strokeWidth="1"
          className={
            index < filled ? 'fill-confirmed stroke-confirmed' : 'fill-transparent stroke-line'
          }
        />
      ))}
      <line
        x1="0"
        y1="28"
        x2="240"
        y2="28"
        strokeWidth="1"
        strokeDasharray="3 3"
        className="stroke-line"
      />
      <text x="0" y="38" className="fill-muted font-mono" style={{ fontSize: '8px' }}>
        questions closed · not reported
      </text>
    </Frame>
  );
}

// ------------------------------------------------------------------ other --

/**
 * Any agent this build does not have a purpose-drawn glyph for.
 *
 * Deliberately plain: an agent that gets a borrowed visual would be claiming
 * to do something it does not do.
 */
export function GenericTicks({ agentId, passes }: { agentId: string; passes: Pass[] }) {
  const shown = passes.slice(Math.max(0, passes.length - 24));
  if (shown.length === 0) {
    return (
      <Frame agentId={agentId} kind="ticks" caption="No activity recorded this session.">
        <Absent />
      </Frame>
    );
  }
  const slot = 240 / Math.max(shown.length, 8);
  return (
    <Frame
      agentId={agentId}
      kind="ticks"
      caption={`${shown.length} correlated pass${shown.length === 1 ? '' : 'es'} recorded`}
    >
      {shown.map((pass, index) => (
        <rect
          key={pass.correlationId}
          x={index * slot}
          y="12"
          width={Math.max(2, slot - 3)}
          height="14"
          className="fill-muted"
        />
      ))}
    </Frame>
  );
}
