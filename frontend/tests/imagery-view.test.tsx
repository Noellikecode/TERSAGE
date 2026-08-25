/**
 * Two views of one building, and the line between them.
 *
 * The failure this prevents: an aerial panel that quietly serves a kerb-level
 * frame. A commander told they are looking straight down at a roof, and shown a
 * street, is worse served than one told there is no aerial — so the view asked
 * for is the view fetched, and the caption says which one arrived.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BuildingImagery } from '@/components/incident/BuildingImagery';

const requested: string[] = [];

function reply(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  requested.length = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      return reply({
        address_id: 'sf-0450-hayes',
        available: true,
        provider: url.includes('aerial') ? 'satellite' : 'street-view',
        content_type: 'image/svg+xml',
        data_url: 'data:image/svg+xml;base64,PHN2Zy8+',
        attribution: 'Imagery © 2026 Google',
        captured_hint: '',
        unavailable_reason: null,
      });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe('asking for a viewpoint', () => {
  it('asks the backend for the street view by default', async () => {
    render(<BuildingImagery addressId="sf-0450-hayes" />);
    await screen.findByRole('img');
    expect(requested.some((u) => u.includes('view=street'))).toBe(true);
  });

  it('asks for the aerial when the aerial is what is on screen', async () => {
    render(<BuildingImagery addressId="sf-0450-hayes" view="aerial" />);
    await screen.findByRole('img');
    expect(requested.some((u) => u.includes('view=aerial'))).toBe(true);
  });

  it('names the viewpoint in the alt text, so the two are never confused', async () => {
    render(<BuildingImagery addressId="sf-0450-hayes" view="aerial" />);
    const photo = await screen.findByRole('img');
    expect(photo).toHaveAccessibleName(/Overhead photograph of sf-0450-hayes/i);
  });

  it('refetches when the viewpoint changes rather than keeping the old frame', async () => {
    const { rerender } = render(<BuildingImagery addressId="sf-0450-hayes" view="street" />);
    await screen.findByRole('img');
    rerender(<BuildingImagery addressId="sf-0450-hayes" view="aerial" />);
    await waitFor(() => expect(requested.some((u) => u.includes('view=aerial'))).toBe(true));
  });

  it('says an aerial is a flyover date, not a measurement', async () => {
    render(<BuildingImagery addressId="sf-0450-hayes" view="aerial" />);
    await screen.findByRole('img');
    expect(screen.getByText(/when the tile was flown/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing here is a measurement/)).toBeInTheDocument();
  });

  it('still renders the provider attribution, which the licence requires', async () => {
    render(<BuildingImagery addressId="sf-0450-hayes" view="aerial" />);
    await screen.findByRole('img');
    expect(screen.getByTestId('imagery-attribution')).toHaveTextContent('Imagery © 2026 Google');
  });
});
