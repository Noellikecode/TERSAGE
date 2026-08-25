/**
 * Accessibility, tested as behaviour rather than as an audit checklist.
 *
 * The specific failures being prevented: an officer who cannot see colour
 * reading DISPUTED as CONFIRMED, a screen-reader user not knowing the brief
 * advanced, and a tablet user who cannot reach a control without a mouse.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AttributeGrid } from '@/components/profile/AttributeGrid';
import { BriefPanel, announcementFor } from '@/components/incident/BriefPanel';
import { BuildingImagery } from '@/components/incident/BuildingImagery';
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

/** A photograph as the imagery endpoint returns one. */
const IMAGERY = {
  address_id: 'sf-0450-hayes',
  available: true,
  provider: 'google-street-view',
  content_type: 'image/jpeg',
  data_url: 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAA',
  attribution: 'Imagery © 2026 Google',
  captured_hint: 'June 2025',
  unavailable_reason: null,
};

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = (value: unknown) =>
        new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
      if (url.includes('/imagery')) return body(IMAGERY);
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
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('TERSAGE');
    expect(screen.getAllByRole('heading', { level: 2 }).length).toBeGreaterThan(0);
  });

  it('names each standby region after its own heading', () => {
    renderConsole();
    // Standby is two columns: the fleet on the left and the region beside it.
    // Each region has a heading of its own, because
    // two regions with the same name are two regions a screen-reader user
    // cannot tell apart. The slow loop is one region now, not two halves.
    for (const name of [
      'District readiness',
      'Regional fire activity',
      'Ranked for survey',
      'Slow loop',
    ]) {
      const region = screen.getByRole('region', { name });
      const labelledBy = region.getAttribute('aria-labelledby');
      expect(labelledBy).toBeTruthy();
      expect(document.getElementById(labelledBy as string)).toHaveTextContent(name);
    }
  });

  it('puts the district bar inside the main landmark, not adrift above it', () => {
    renderConsole();
    // It reads as a bar under the header, but a bar of live numbers outside
    // every landmark is content a screen reader user can only reach by
    // stumbling into it.
    const bar = screen.getByRole('region', { name: 'District readiness' });
    expect(screen.getByRole('main')).toContainElement(bar);
    // Its meters are decoration over numbers that are already text.
    for (const meter of screen.getAllByTestId('meter')) {
      expect(meter).toHaveAttribute('aria-hidden', 'true');
    }
  });

  it('names the massing model region once a structure is selected', async () => {
    renderConsole();
    fireEvent.click(screen.getByRole('button', { name: 'sf-0450-hayes' }));
    await waitFor(() =>
      expect(screen.getByRole('region', { name: 'Massing model' })).toBeInTheDocument(),
    );
    const region = screen.getByRole('region', { name: 'Massing model' });
    expect(document.getElementById(region.getAttribute('aria-labelledby') as string))
      .toHaveTextContent('Massing model');
  });
});

describe('the building imagery panel', () => {
  it('gives the photograph alt text and shows the attribution as text', async () => {
    render(<BuildingImagery addressId="sf-0450-hayes" />);
    const photo = await screen.findByRole('img');
    // The alt text names the address; "photo of a building" tells a screen
    // reader user nothing they can act on.
    expect(photo).toHaveAccessibleName(/photograph of sf-0450-hayes/i);
    // Attribution is text, not a watermark burnt into the image.
    expect(screen.getByTestId('imagery-attribution')).toHaveTextContent('Imagery © 2026 Google');
  });

  it('says an absent photograph is absent, in words', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            ...IMAGERY,
            available: false,
            data_url: null,
            attribution: null,
            unavailable_reason: 'No street-level coverage at this address.',
          }),
          { headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
    render(<BuildingImagery addressId="sf-0450-hayes" />);
    expect(await screen.findByText(/No photograph available/)).toBeInTheDocument();
    expect(screen.getByText(/No street-level coverage/)).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
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

  it('gives every interactive control a visible focus style', async () => {
    const { container } = renderConsole();
    // With a structure selected: that is the state with the most controls on
    // screen -- the camera views and the whole dispatch panel are in it.
    fireEvent.click(screen.getByRole('button', { name: 'sf-0450-hayes' }));
    await waitFor(() =>
      expect(screen.getByRole('group', { name: /fixed camera views/i })).toBeInTheDocument(),
    );

    const controls = Array.from(
      container.querySelectorAll('button, select, a[href], input, textarea, [tabindex]'),
    );
    expect(controls.length).toBeGreaterThan(5);

    // Two ways a control can be covered: its own classes, or the console-wide
    // rule in globals.css. Read the rule rather than assuming it -- a radio
    // with neither is focusable with nothing to show for it.
    const globals = readFileSync(resolve(process.cwd(), 'app/globals.css'), 'utf8');
    const rule = globals.match(/:where\(([^)]*)\):focus-visible\s*\{[^}]*outline:/);
    expect(rule).toBeTruthy();
    const covered = new Set(rule![1]!.split(',').map((part) => part.trim().toUpperCase()));
    expect(covered.has('INPUT')).toBe(true);

    for (const control of controls) {
      const className = control.getAttribute('class') ?? '';
      // The skip link uses `focus:` rather than `focus-visible:`; both are visible.
      const styled = /focus-visible:|focus:/.test(className);
      const globallyStyled =
        covered.has(control.tagName) ||
        (covered.has('[TABINDEX]') && control.hasAttribute('tabindex'));
      expect(styled || globallyStyled).toBe(true);
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
