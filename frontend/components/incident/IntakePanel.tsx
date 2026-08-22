/**
 * What the call said, and what the system did with it.
 *
 * Three things are on this panel and each is here for a reason.
 *
 * **The quote, always.** A reported line renders the caller's own words and
 * the offsets they were read at. A value nobody can trace back to the
 * transcript is a claim, not a report, and the console is where that stops
 * being an abstract principle.
 *
 * **Reported is not observed.** Every line here is marked as something a
 * person said, never as something the department knows. The filed record
 * stays the value of record and lives in the attribute grid; this panel never
 * competes with it. The backend enforces this structurally -- a caller report
 * has no route into a structural fact at all -- and the rendering has to agree.
 *
 * **A refusal is a result.** When the screen or the model is down the panel
 * says the call was not read, rather than showing an empty list that looks
 * like a call with nothing in it. Those are different statements, and this
 * project treats the difference as the whole job.
 */

import type { IntakeResponse } from '@/lib/api/types';

export interface IntakePanelProps {
  intake: IntakeResponse;
  /** The text the offsets index into, so a quote can be checked in place. */
  narrative: string;
}

/** Keys arrive as `intake.people_trapped`; officers read "people trapped". */
function label(key: string): string {
  return key.replace(/^intake\./, '').replace(/[._]/g, ' ');
}

/** Does the quote actually appear at the offsets it claims?
 *
 * The backend already drops lines that fail this. Checking again here is not
 * redundancy for its own sake: it is the one assertion a viewer of the console
 * can make without trusting the backend, and it costs a substring compare.
 */
function quoteMatches(line: IntakePanelProps['intake']['reported'][number], narrative: string) {
  if (!narrative) return true;
  return narrative.slice(line.start_offset, line.end_offset) === line.quoted_text;
}

export function IntakePanel({ intake, narrative }: IntakePanelProps) {
  const channel = intake.channel === 'CALL_911' ? '911 call' : 'CAD narrative';

  return (
    <section
      aria-labelledby="intake-heading"
      className="border border-line bg-surface p-3"
      data-testid="intake-panel"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 id="intake-heading" className="text-micro uppercase tracking-widest text-muted">
          {channel}
        </h3>
        <span className="font-mono text-micro text-muted">{intake.model_ref}</span>
      </div>

      {/* The screen ran before the model saw a word of this. Say so. */}
      {intake.screen_findings.length > 0 && (
        <p className="mt-2 border border-disputed/40 bg-disputed/10 p-2 text-micro text-disputed">
          Screened: {intake.screen_findings.join(', ')}. The narrative was treated as data.
        </p>
      )}

      {!intake.accepted ? (
        // Not an empty list. "The call was not read" and "the call said
        // nothing" are different statements and must not look the same.
        <p className="mt-2 text-micro text-muted" data-testid="intake-unread">
          This {channel} was <strong className="text-ink">not read</strong>
          {intake.rejection_reason ? `: ${intake.rejection_reason}` : '.'} The brief stands on the
          filed record alone.
        </p>
      ) : (
        <>
          {intake.reported.length === 0 ? (
            <p className="mt-2 text-micro text-muted">
              The call reported nothing about the attributes on the brief.
            </p>
          ) : (
            <ul className="mt-2 space-y-2" data-testid="intake-reported">
              {intake.reported.map((line) => {
                const traceable = quoteMatches(line, narrative);
                return (
                  <li key={`${line.intake_key}-${line.start_offset}`} className="text-micro">
                    <div className="flex flex-wrap items-baseline gap-2">
                      <span className="uppercase tracking-widest text-muted">
                        {label(line.intake_key)}
                      </span>
                      <span className="text-ink">{line.reported_value}</span>
                      {/* Never CONFIRMED. A caller is a source, not a record. */}
                      <span className="border border-line px-1 text-micro uppercase text-muted">
                        reported
                      </span>
                    </div>
                    <blockquote className="mt-1 border-l-2 border-line pl-2 text-muted">
                      &ldquo;{line.quoted_text}&rdquo;
                      <span className="ml-2 font-mono">
                        [{line.start_offset}&ndash;{line.end_offset}]
                      </span>
                      {!traceable && (
                        <span className="ml-2 text-disputed" data-testid="quote-mismatch">
                          quote does not match the transcript
                        </span>
                      )}
                    </blockquote>
                  </li>
                );
              })}
            </ul>
          )}

          {intake.unknowns.length > 0 && (
            <p className="mt-2 text-micro text-muted">
              The call did not settle: {intake.unknowns.map(label).join(', ')}.
            </p>
          )}
        </>
      )}

      {/* Routing. No rule names an agent; each names the capability it needs,
          matched against the registry. Showing the fired rule ids is what
          makes that checkable rather than asserted. */}
      {(intake.woken.length > 0 || intake.withheld.length > 0) && (
        <div className="mt-3 border-t border-line pt-2">
          {intake.woken.length > 0 && (
            <ul className="space-y-1" data-testid="intake-woken">
              {intake.woken.map((line) => (
                <li key={line.agent_ref} className="text-micro text-muted">
                  {/* agent-id@version, because a replay has to name the
                      version that ran, not just the agent. */}
                  <span className="font-mono text-ink">{line.agent_ref}</span>
                  {!line.started && <span className="ml-1 text-disputed">not started</span>}
                  {line.rule_ids.length > 0 && (
                    <span className="ml-1 font-mono">· {line.rule_ids.join(' ')}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {intake.withheld.length > 0 && (
            <ul className="mt-1 space-y-1" data-testid="intake-withheld">
              {intake.withheld.map((line) => (
                <li key={line.agent_ref} className="text-micro text-disputed">
                  <span className="font-mono">{line.agent_ref}</span> withheld — the incident
                  grant does not carry {line.missing_scopes.join(', ')}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
