/**
 * Accessibility, tested as behaviour rather than as an audit checklist.
 *
 * The specific failures being prevented: an officer who cannot see colour
 * reading DISPUTED as CONFIRMED, a screen-reader user not knowing the brief
 * advanced, and a tablet user who cannot reach a control without a mouse.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AttributeGrid } from '@/components/profile/AttributeGrid';
import { BriefPanel, announcementFor } from '@/components/incident/BriefPanel';
import { CommandCenter } from '@/components/CommandCenter';
import { StatusPill } from '@/components/StatusPill';
import { SurveyQueue } from '@/components/standby/SurveyQueue';
import { ThermalPanel } from '@/components/incident/ThermalPanel';
import {
  AGENTS,
  DECISIONS,
  EVENTS,
  GEOMETRY,
  PROFILE,
  QUEUE,
  STATS,
  STATUS,
  SUBSCRIPTIONS,
  emission,
} from './fixtures';

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = (value: unknown) =>
        new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
      if (url.includes('/timeline')) return body([]);
      if (url.includes('/geometry')) return body(GEOMETRY);
      if (url.includes('/buildings/')) return body(PROFILE);
      return body({ status: 'ready', ready: true, mode: 'fake', checks: [] });
    }),
  );
  vi.stubGlobal('EventSource', undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderConsole() {
  return render(
    <CommandCenter
      status={STATUS}
      readiness={null}
      error={null}
      initialStats={STATS}
      initialQueue={QUEUE}
      initialAgents={AGENTS}
      initialSubscriptions={SUBSCRIPTIONS}
      initialEvents={EVENTS}
      initialDecisions={DECISIONS}
      forceSvgGeometry
    />,
  );
}

describe('landmarks and structure', () => {
  it('has the landmarks a screen reader navigates by', () => {
    renderConsole();
    expect(screen.getByRole('banner')).toBeInTheDocument();
    expect(screen.getByRole('main')).toBeInTheDocument();
    expect(screen.getByRole('contentinfo')).toBeInTheDocument();
  });

  it('offers a skip link before anything else', () => {
    renderConsole();
    const skip = screen.getByRole('link', { name: /skip to main content/i });
    expect(skip).toHaveAttribute('href', '#main');
  });

  it('gives every region an accessible name', () => {
    renderConsole();
    const main = screen.getByRole('main');
    const regions = within(main).getAllByRole('region', { hidden: true });
    for (const region of regions) {
      const labelled = region.getAttribute('aria-label') ?? region.getAttribute('aria-labelledby');
      expect(labelled).toBeTruthy();
    }
  });

  it('uses a real heading hierarchy', () => {
    renderConsole();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('FIRST DUE');
    expect(screen.getAllByRole('heading', { level: 2 }).length).toBeGreaterThan(0);
  });
});

describe('never colour alone', () => {
  it('pairs every state chip with a glyph and a word', () => {
    const { container } = render(
      <>
        <StatusPill tone="confirmed" label="confirmed" />
        <StatusPill tone="disputed" label="disputed" />
        <StatusPill tone="unknown" label="unknown" />
      </>,
    );
    // The glyph is decorative; the word is what carries the meaning.
    expect(container.querySelectorAll('[aria-hidden="true"]')).toHaveLength(3);
    expect(screen.getByText('confirmed')).toBeInTheDocument();
    expect(screen.getByText('disputed')).toBeInTheDocument();
    expect(screen.getByText('unknown')).toBeInTheDocument();
  });

  it('says DISPUTED in words in the attribute grid', () => {
    render(<AttributeGrid facts={PROFILE.facts} unknownKeys={PROFILE.unknown_keys} />);
    expect(screen.getByText('disputed')).toBeInTheDocument();
    expect(screen.getByText('unknown')).toBeInTheDocument();
  });

  it('never lets UNKNOWN read as a value', () => {
    render(<AttributeGrid facts={[]} unknownKeys={['suppression.sprinklered']} />);
    // An empty grid says there are no records, not that the building is fine.
    expect(screen.getByText('No structural attributes on record')).toBeInTheDocument();
    expect(screen.getByText(/absence of records/i)).toBeInTheDocument();
  });

  it('never lets UNSCANNED read as cool or clear', () => {
    render(<ThermalPanel faces={GEOMETRY.spec.faces} />);
    expect(screen.getAllByText('unscanned')).toHaveLength(4);
    expect(screen.getAllByText('UNSCANNED — no coverage')).toHaveLength(4);
    expect(screen.queryByText(/cool/i)).not.toBeInTheDocument();
    expect(screen.getByText(/cannot see through walls/i)).toBeInTheDocument();
  });
});

describe('announcements', () => {
  it('announces the instant stage and says no model was used', () => {
    expect(announcementFor(emission())).toMatch(/Instant brief ready, version 1/);
    expect(announcementFor(emission())).toMatch(/no model was invoked/);
  });

  it('announces an amendment as an amendment', () => {
    const text = announcementFor(emission({ version: 3, stage: 'AMENDMENT' }));
    expect(text).toMatch(/Amendment, version 3/);
  });

  it('announces an unavailable narrative rather than staying silent', () => {
    const text = announcementFor(
      emission({ version: 2, stage: 'ENRICHED', model_invoked: true }),
    );
    expect(text).toMatch(/narrative is unavailable/);
    expect(text).toMatch(/facts are unchanged/);
  });

  it('puts announcements in a polite live region', () => {
    renderConsole();
    const announcer = screen.getByTestId('brief-announcer');
    expect(announcer).toHaveAttribute('aria-live', 'polite');
    expect(announcer).toHaveAttribute('aria-atomic', 'true');
  });
});

describe('keyboard operation', () => {
  it('exposes the queue reasons through a real toggle button', () => {
    render(<SurveyQueue entries={QUEUE.entries} />);
    const toggles = screen.getAllByRole('button', { name: /why|hide why/i });
    expect(toggles[0]).toHaveAttribute('aria-expanded');

    fireEvent.click(toggles[1]!);
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'true');
  });

  it('makes the camera views a pressed-state button group', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: 'sf-0450-hayes' }));
    await waitFor(() =>
      expect(screen.getByRole('group', { name: /fixed camera views/i })).toBeInTheDocument(),
    );
    const group = screen.getByRole('group', { name: /fixed camera views/i });
    const buttons = within(group).getAllByRole('button');
    expect(buttons.length).toBeGreaterThan(1);
    expect(buttons[0]).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(buttons[1]!);
    expect(buttons[1]).toHaveAttribute('aria-pressed', 'true');
  });

  it('gives every interactive control a visible focus style', () => {
    const { container } = renderConsole();

    const controls = container.querySelectorAll('button, select, a[href], input');
    expect(controls.length).toBeGreaterThan(5);
    for (const control of Array.from(controls)) {
      const className = control.getAttribute('class') ?? '';
      // The skip link uses `focus:` rather than `focus-visible:`; both are visible.
      expect(className).toMatch(/focus-visible:|focus:/);
    }
  });

  it('names the profile region once it is opened', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: 'sf-0450-hayes' }));
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /sf-0450-hayes/ })).toBeInTheDocument(),
    );
    // The attribute table has a caption, so a screen reader announces what it is.
    expect(screen.getByRole('table')).toHaveAccessibleName(/Structural attributes/);
  });
});

describe('the brief panel', () => {
  it('shows the gaps section with all three absence kinds named', () => {
    render(
      <BriefPanel
        emission={emission({
          unknowns: ['suppression.sprinklered'],
          unavailable: ['tier-ii-confidential'],
          withheld: ['ems-derived'],
        })}
      />,
    );
    const gaps = screen.getByRole('heading', { name: 'Gaps' }).parentElement as HTMLElement;
    expect(within(gaps).getByText(/no record found/)).toBeInTheDocument();
    expect(within(gaps).getByText(/source unreachable/)).toBeInTheDocument();
    expect(within(gaps).getByText('WITHHELD')).toBeInTheDocument();
    expect(
      screen.getByText(/not findings that nothing is there/i),
    ).toBeInTheDocument();
  });
});
