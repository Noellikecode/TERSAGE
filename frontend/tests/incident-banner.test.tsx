/**
 * The banner at the moment a call lands.
 *
 * What it leads with is the whole point: a commander reads a street address,
 * not an internal key. The key still has to be on screen, because it is what
 * every event, grant and log entry this incident produces is filed under, and
 * an operator matching the screen against the record needs both.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { IncidentBanner, streetPart } from '@/components/incident/IncidentBanner';

const BASE = {
  incidentId: 'inc-1',
  addressId: 'sf-0450-hayes',
  alarmLevel: 2,
  dispatchedAt: new Date().toISOString(),
  coldStart: false,
};

describe('the incident banner', () => {
  it('leads with the street address and keeps the id in parentheses', () => {
    render(<IncidentBanner {...BASE} addressDisplay="450 Hayes St, San Francisco, CA 94102" />);
    const banner = screen.getByLabelText('Active incident');
    expect(within(banner).getByText('450 Hayes St')).toBeInTheDocument();
    expect(within(banner).getByText('(sf-0450-hayes)')).toBeInTheDocument();
  });

  it('draws the address larger than the id', () => {
    render(<IncidentBanner {...BASE} addressDisplay="450 Hayes St, San Francisco, CA 94102" />);
    const banner = screen.getByLabelText('Active incident');
    expect(within(banner).getByText('450 Hayes St').className).toContain('text-hero');
    expect(within(banner).getByText('(sf-0450-hayes)').className).toContain('text-body');
  });

  it('falls back to the id when the city could not place the address', () => {
    // Never a placeholder: printing a slug styled as a street address, or an
    // empty space where the address belongs, are both worse than the id.
    render(<IncidentBanner {...BASE} addressDisplay="" />);
    const banner = screen.getByLabelText('Active incident');
    expect(within(banner).getByText('sf-0450-hayes')).toBeInTheDocument();
    expect(within(banner).queryByText('(sf-0450-hayes)')).toBeNull();
  });
});

describe('the street part of an address', () => {
  it('drops the city and postcode, which are constant across a district', () => {
    expect(streetPart('450 Hayes St, San Francisco, CA 94102')).toBe('450 Hayes St');
  });

  it('returns an address with no comma whole rather than truncating it', () => {
    expect(streetPart('450 Hayes St')).toBe('450 Hayes St');
  });
});
