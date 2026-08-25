/**
 * The regional fire activity panel, on its own.
 *
 * The property under test is the honesty of the empty case. Against the live
 * API San Francisco proper returns **zero** VIIRS detections and Northern
 * California returns **266**: a 375 m wildfire pixel does not see a structure
 * fire, so an empty city inside a busy region is the instrument working. The
 * panel has to render that as a plain fact -- never as an alarm, never as a
 * failure, and never by putting something in the city to make the map look
 * populated.
 *
 * The second property is the fire weather's provenance. It is NASA POWER
 * reanalysis, days behind real time, and the console shows live NWS wind
 * elsewhere. A POWER wind reading that does not carry its observation window is
 * the panel passing four-day-old air off as the weather on the fireground.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  FireActivityMap,
  confidenceBand,
  normalizeFireActivity,
  windowLabel,
  type FireActivity,
} from '@/components/standby/FireActivityMap';

/** The live answer's shape, with two detections standing in for the 266. */
const PAYLOAD = {
  district_id: 'sffd-district-03',
  available: true,
  unavailable_reason: null,
  source: 'nasa-firms/viirs-snpp-nrt',
  region_label: 'Northern California',
  city_label: 'San Francisco',
  bbox: { west: -124.4, south: 36.9, east: -119.9, north: 41.2 },
  city_bbox: { west: -122.52, south: 37.7, east: -122.35, north: 37.83 },
  regional_count: 266,
  in_city_count: 0,
  detections: [
    {
      latitude: 39.81,
      longitude: -121.44,
      confidence: 'h',
      frp: 42.6,
      acquired_at: '2026-08-22T21:10:00+00:00',
      satellite: 'N',
    },
    {
      latitude: 38.44,
      longitude: -122.71,
      confidence: 'l',
      frp: null,
      acquired_at: '2026-08-22T21:10:00+00:00',
      satellite: 'N',
    },
  ],
  fire_weather: {
    available: true,
    unavailable_reason: null,
    source: 'nasa-power',
    temperature_c: 24.3,
    relative_humidity_pct: 41,
    wind_speed_ms: 3.6,
    wind_direction_deg: 265,
    observation_start: '2026-08-18T00:00:00+00:00',
    observation_end: '2026-08-19T00:00:00+00:00',
  },
};

function activity(overrides: Partial<FireActivity> = {}): FireActivity {
  const base = normalizeFireActivity(PAYLOAD);
  expect(base).not.toBeNull();
  return { ...(base as FireActivity), ...overrides };
}

describe('reading the payload', () => {
  it('reads the counts, the boxes, the detections and the weather', () => {
    const read = activity();
    expect(read.available).toBe(true);
    expect(read.regionalCount).toBe(266);
    expect(read.inCityCount).toBe(0);
    expect(read.bbox).toEqual({ west: -124.4, south: 36.9, east: -119.9, north: 41.2 });
    expect(read.cityBBox).not.toBeNull();
    expect(read.detections).toHaveLength(2);
    expect(read.weather?.temperature_c).toBe(24.3);
    expect(read.weather?.wind_direction_deg).toBe(265);
  });

  it('accepts a bbox as an array in west, south, east, north order', () => {
    const read = normalizeFireActivity({ ...PAYLOAD, bbox: [-124.4, 36.9, -119.9, 41.2] });
    expect(read?.bbox).toEqual({ west: -124.4, south: 36.9, east: -119.9, north: 41.2 });
  });

  it('drops a detection with no position rather than placing it somewhere', () => {
    const read = normalizeFireActivity({
      ...PAYLOAD,
      detections: [...PAYLOAD.detections, { confidence: 'h', frp: 9 }],
    });
    expect(read?.detections).toHaveLength(2);
  });

  it('leaves a count it was not given as null, never as a zero', () => {
    const read = normalizeFireActivity({
      available: true,
      bbox: PAYLOAD.bbox,
      detections: [],
    });
    // No city box to count inside, no reported figure: an absence, and an
    // absence is not the same claim as "none in the city".
    expect(read?.inCityCount).toBeNull();
    expect(read?.regionalCount).toBeNull();
  });

  it('reads VIIRS letters, MODIS percentages, and the absence of either', () => {
    expect(confidenceBand('h')).toBe('high');
    expect(confidenceBand('nominal')).toBe('nominal');
    expect(confidenceBand('l')).toBe('low');
    expect(confidenceBand(91)).toBe('high');
    expect(confidenceBand(40)).toBe('nominal');
    expect(confidenceBand(5)).toBe('low');
    expect(confidenceBand(null)).toBe('unreported');
    expect(confidenceBand('')).toBe('unreported');
  });

  it('reports the observation window in UTC, so a label does not move with the reader', () => {
    expect(windowLabel(activity().weather)).toBe('2026-08-18 → 2026-08-19');
    expect(windowLabel(null)).toBeNull();
  });
});

describe('an empty city inside a busy region', () => {
  it('says it in one line, as a fact', () => {
    render(<FireActivityMap activity={activity()} />);
    const counts = screen.getByTestId('fire-activity-counts');
    expect(counts).toHaveTextContent('0');
    expect(counts).toHaveTextContent(/active detections in San Francisco/);
    expect(counts).toHaveTextContent('266');
    expect(counts).toHaveTextContent(/across Northern California/);
  });

  it('is neither an alarm nor a failure', () => {
    const { container } = render(<FireActivityMap activity={activity()} />);
    // No alert role, and the count line is not painted in the alarm colour: a
    // quiet city is the expected reading, not an incident.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('fire-activity-counts').className).not.toMatch(/text-alarm/);
    expect(container.textContent).not.toMatch(/error|failed/i);
  });

  it('explains why a zero is the correct reading, with the zero', () => {
    render(<FireActivityMap activity={activity()} />);
    expect(screen.getByText(/375 m and built for wildfire/)).toBeInTheDocument();
    expect(screen.getByText(/instrument working, not a fault/)).toBeInTheDocument();
  });

  it('states the empty city in words, with no map to read it off', () => {
    // The scatter is gone: dots over a black rectangle with no coastline and
    // no coordinates could not tell anybody where anything was, and it took
    // the top of the page to say "nothing is happening here". The sentence
    // carries the whole argument and always did.
    render(<FireActivityMap activity={activity()} />);
    expect(screen.queryByTestId('fire-activity-scatter')).not.toBeInTheDocument();
    expect(screen.queryByTestId('fire-activity-city-outline')).not.toBeInTheDocument();

    const counts = screen.getByTestId('fire-activity-counts');
    expect(counts).toHaveTextContent(/0 active detections in San Francisco/);
    expect(counts).toHaveTextContent(/266.*across/);
    expect(screen.getByText(/the instrument working, not a fault/)).toBeInTheDocument();
  });

  it('says "not reported" where a count is absent instead of drawing a zero', () => {
    render(<FireActivityMap activity={activity({ inCityCount: null, regionalCount: null })} />);
    const counts = screen.getByTestId('fire-activity-counts');
    expect(counts).toHaveTextContent(/In-city count not reported/);
    expect(counts).toHaveTextContent(/regional count not reported/);
  });
});


describe('the fire weather', () => {
  it('shows temperature, humidity and wind, each with its observation window', () => {
    render(<FireActivityMap activity={activity()} />);
    const weather = screen.getByTestId('fire-weather');
    expect(within(weather).getByText('24.3')).toBeInTheDocument();
    expect(within(weather).getByText('°C')).toBeInTheDocument();
    expect(within(weather).getByText('41')).toBeInTheDocument();
    expect(within(weather).getByText('3.6')).toBeInTheDocument();
    expect(within(weather).getByText('m/s @ 265°')).toBeInTheDocument();
    // One window per reading. A number without one reads as current.
    expect(within(weather).getAllByText('2026-08-18 → 2026-08-19')).toHaveLength(3);
  });

  it('says it is reanalysis and not the live NWS wind', () => {
    render(<FireActivityMap activity={activity()} />);
    const weather = screen.getByTestId('fire-weather');
    expect(within(weather).getByText(/days behind real time/)).toBeInTheDocument();
    expect(within(weather).getByText(/recent conditions, not current/)).toBeInTheDocument();
    expect(within(weather).getByText(/not the live NWS wind/)).toBeInTheDocument();
  });

  it('marks a missing reading "not reported" rather than blank', () => {
    const read = activity();
    render(
      <FireActivityMap
        activity={{
          ...read,
          weather: { ...read.weather!, wind_speed_ms: null, wind_direction_deg: null },
        }}
      />,
    );
    expect(within(screen.getByTestId('fire-weather')).getByText('not reported')).toBeInTheDocument();
  });

  it('renders a weather refusal as a state, carrying its reason', () => {
    const read = activity();
    render(
      <FireActivityMap
        activity={{
          ...read,
          weather: {
            ...read.weather!,
            available: false,
            unavailable_reason: 'POWER has no cell covering this bbox yet.',
          },
        }}
      />,
    );
    const weather = screen.getByTestId('fire-weather');
    expect(within(weather).getByText(/Fire weather UNAVAILABLE/)).toBeInTheDocument();
    expect(within(weather).getByText(/no cell covering this bbox/)).toBeInTheDocument();
  });

  it('says fire weather is absent when the payload carries none', () => {
    render(<FireActivityMap activity={activity({ weather: null })} />);
    expect(screen.getByText(/Fire weather UNAVAILABLE/)).toBeInTheDocument();
  });
});

describe('refusals and failures', () => {
  it('renders an answered refusal as a state, not an error', () => {
    render(
      <FireActivityMap
        activity={activity({
          available: false,
          unavailable_reason: 'FIRMS map key is not configured for this deployment.',
        })}
      />,
    );
    expect(screen.getByText(/Fire activity UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByText(/FIRMS map key is not configured/)).toBeInTheDocument();
    expect(screen.queryByTestId('fire-activity-scatter')).not.toBeInTheDocument();
  });

  it('keeps a failed request distinct from an answered one', () => {
    render(<FireActivityMap activity={null} error="fire activity timed out" />);
    expect(screen.getByText(/Fire-activity request failed: fire activity timed out/)).toBeInTheDocument();
  });

  it('says nothing is known before the first answer, rather than nothing burning', () => {
    render(<FireActivityMap activity={null} />);
    expect(screen.getByText(/Fire activity UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing is inferred here/)).toBeInTheDocument();
  });

  it('keeps its heading and its region in every state', () => {
    for (const props of [
      { activity: activity() },
      { activity: activity({ available: false }) },
      { activity: null, error: 'boom' },
    ]) {
      const { unmount } = render(<FireActivityMap {...props} />);
      const region = screen.getByRole('region', { name: 'Regional fire activity' });
      expect(region.getAttribute('aria-labelledby')).toBe('fire-activity-heading');
      unmount();
    }
  });
});

describe('the bounding box this backend actually sends', () => {
  it('projects onto `region`, not only the FIRMS-shaped aliases', () => {
    // The map spent its life printing "no bounding box reported" while holding
    // one: it read `bbox`/`region_bbox`/`query_bbox` and the API sends `region`.
    const activity = normalizeFireActivity({
      district_id: 'sffd-district-03',
      available: true,
      provider: 'synthetic',
      region: { west: -124.5, south: 36.5, east: -119.5, north: 40.5 },
      city: { west: -122.55, south: 37.7, east: -122.35, north: 37.84 },
      regional_count: 26,
      in_city_count: 0,
      detections: [],
    });

    expect(activity!.bbox).toEqual({ west: -124.5, south: 36.5, east: -119.5, north: 40.5 });
    expect(activity!.cityBBox).toEqual({
      west: -122.55,
      south: 37.7,
      east: -122.35,
      north: 37.84,
    });
  });

  it('still prefers an explicit bbox when one is sent', () => {
    const activity = normalizeFireActivity({
      bbox: [-1, -2, 3, 4],
      region: { west: -124.5, south: 36.5, east: -119.5, north: 40.5 },
    });
    expect(activity!.bbox).toEqual({ west: -1, south: -2, east: 3, north: 4 });
  });

  it('does not read the region box as the region’s name', () => {
    const activity = normalizeFireActivity({
      region: { west: -124.5, south: 36.5, east: -119.5, north: 40.5 },
    });
    expect(activity!.regionLabel).toBe('the region');
  });
});
