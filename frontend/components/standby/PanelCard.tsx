/**
 * The card a standby panel sits in.
 *
 * The fleet column has had this shape since the rail was rebuilt -- a rounded,
 * hairlined block on `surface` with a ruled header carrying a name, a count and
 * a line of plain English under it. Regional fire activity and Records disagree
 * were the two panels that did not have it: they ran as bare sections stacked
 * in a scrolling column, separated by nothing but a one-pixel rule, so the eye
 * read them as one long list rather than as two answers to two questions.
 *
 * This is that chrome, extracted so the three columns are visibly the same kind
 * of object. Nothing here decides anything -- it is a frame, and every panel
 * inside one keeps its own empty and refusal states.
 *
 * `note` is for a count or a status word, set flush right against the heading.
 * `subheading` is for what this panel is *for*, in words nobody has to read the
 * repository to understand: "Slow loop" is the architecture and it keeps its
 * name, but an inspector reading it cold learns nothing from it.
 */

import type { ReactNode } from 'react';

export interface PanelCardProps {
  /** The id the heading carries, so the section can be labelled by it. */
  id: string;
  heading: string;
  /** What this panel is for. Optional, and worth writing where it is not obvious. */
  subheading?: string;
  /** A count or a status word, right-aligned against the heading. */
  note?: ReactNode;
  /** Extra classes for the section itself -- sizing and flex behaviour. */
  className?: string;
  /** Set when the panel body scrolls rather than the column around it. */
  bodyClassName?: string;
  children: ReactNode;
}

export function PanelCard({
  id,
  heading,
  subheading,
  note,
  className = '',
  bodyClassName = '',
  children,
}: PanelCardProps) {
  return (
    <section
      aria-labelledby={id}
      className={`flex min-w-0 flex-col bg-surface lg:rounded-lg lg:border lg:border-line lg:overflow-hidden ${className}`}
    >
      <div className="flex shrink-0 flex-wrap items-baseline justify-between gap-2 border-b border-line px-4 pb-2 pt-3">
        <h2 id={id} className="text-label uppercase tracking-widest text-muted">
          {heading}
        </h2>
        {note !== undefined && note !== null && (
          <span className="text-label uppercase text-muted">{note}</span>
        )}
        {subheading && (
          <p className="w-full text-micro normal-case text-muted">{subheading}</p>
        )}
      </div>
      <div className={`min-w-0 ${bodyClassName}`}>{children}</div>
    </section>
  );
}
