/**
 * The regional heat map: its arithmetic, and what it says when it cannot draw.
 *
 * deck.gl needs a WebGL2 context and jsdom has none, so these tests exercise
 * the two things that survive without one and matter most:
 *
 * 1. **The distance arithmetic.** "Nearest detection 62 km" is a number an
 *    officer would act on, and it is computed here rather than reported by the
 *    backend, so it is the one piece of this panel that can be wrong quietly.
 * 2. **The no-canvas path.** A station tablet without WebGL2 must get the
 *    counts, the key and a sentence -- never a blank rectangle, which reads as
 *    "nothing is burning".
 *
 * The `webgl` prop exists for exactly this: the component asks the document for
 * a context by default, and the tests hand it the answer instead of stubbing
 * `HTMLCanvasElement.prototype.getContext` globally.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { FireActivity, FireDetection } from '@/components/standby/FireActivityMap';
import {
  RegionalHeatMap,
  distanceKm,
  hotspotsFrom,
  nearestDetection,
  totalFrp,
} from '@/components/standby/RegionalHeatMap';
import type { RegionBasemapView } from '@/lib/api/types';

/** San Francisco's centre, near enough for a distance check. */
const SF: [number, number] = [-122.45, 37.77];

function detection(overrides: Partial<FireDetection> = {}): FireDetection {
  return {
    latitude: 38.5,
    longitude: -122.0,
    confidence: 'n',
    frp: 12.5,
    acquired_at: '2026-08-24T09:34:00Z',
    satellite: 'VIIRS (N)',
    brightness_k: 331.4,
    daynight: 'night',
    ...overrides,
  };
}

function activity(overrides: Partial<FireActivity> = {}): FireActivity {
  return {
    available: true,
    unavailable_reason: null,
    bbox: { west: -124.5, south: 36.5, east: -119.5, north: 40.5 },
    cityBBox: { west: -122.55, south: 37.7, east: -122.35, north: 37.84 },
    cityLabel: 'San Francisco',
    regionLabel: 'Northern California',
    detections: [detection()],
    regionalCount: 236,
    inCityCount: 0,
    source: 'nasa-firms',
    resolution_note: 'VIIRS pixels are ~375 m and cannot resolve a structure fire.',
    weather: null,
    ...overrides,
  };
}

const BASEMAP: RegionBasemapView = {
  available: true,
  provider: 'static-map',
  content_type: 'image/png',
  data_url: 'data:image/png;base64,AAAA',
  bounds: { west: -125.5, south: 35.7, east: -118.4, north: 41.2 },
  zoom: 7,
  style: 'terrain',
  attribution: 'Map data © Google',
  unavailable_reason: '',
};

describe('the distance arithmetic', () => {
  it('measures a known separation to within a kilometre', () => {
    // San Francisco to Sacramento: 121 km, a distance anyone can check.
    const km = distanceKm(SF, [-121.49, 38.58]);
    expect(km).toBeGreaterThan(118);
    expect(km).toBeLessThan(124);
  });

  it('is symmetric', () => {
    const there = distanceKm(SF, [-121.49, 38.58]);
    const back = distanceKm([-121.49, 38.58], SF);
    expect(there).toBeCloseTo(back, 6);
  });

  it('picks the closest detection, not the first', () => {
    const far = detection({ latitude: 40.4, longitude: -120.0 });
    const near = detection({ latitude: 37.9, longitude: -122.3 });
    const found = nearestDetection([far, near], SF);
    expect(found?.detection).toBe(near);
    expect(found?.km).toBeLessThan(30);
  });

  it('reports no distance rather than zero when there is nothing to measure', () => {
    // A zero here would render as "a fire at the station".
    expect(nearestDetection([], SF)).toBeNull();
    expect(nearestDetection([detection()], null)).toBeNull();
  });

  it('sums fire radiative power, and reports null when nothing carries it', () => {
    expect(totalFrp([detection({ frp: 10 }), detection({ frp: 2.5 })])).toBeCloseTo(12.5);
    expect(totalFrp([detection({ frp: null }), detection({ frp: null })])).toBeNull();
  });

  it('sums only the detections that reported a value', () => {
    // A missing FRP is not a zero: it is a pixel the product did not rate, and
    // averaging or defaulting it would understate the region.
    expect(totalFrp([detection({ frp: 10 }), detection({ frp: null })])).toBeCloseTo(10);
  });
});

describe('hotspot clustering', () => {
  /** Two tight scatters, far apart: one fire's worth of pixels each. */
  function twoClusters() {
    const north = Array.from({ length: 4 }, (_, i) =>
      detection({ latitude: 40.0 + i * 0.02, longitude: -121.0 + i * 0.02, frp: 30 }),
    );
    const south = Array.from({ length: 3 }, (_, i) =>
      detection({ latitude: 37.2 + i * 0.02, longitude: -122.0 + i * 0.02, frp: 5 }),
    );
    return [...south, ...north];
  }

  it('gathers a scatter into one hotspot rather than six', () => {
    // A VIIRS pass lays a fire down as a line of pixels along the scan. Six
    // pins on one fire would read as six fires.
    const found = hotspotsFrom(twoClusters(), SF);
    expect(found).toHaveLength(2);
    expect(found[0]?.detections).toHaveLength(4);
  });

  it('ranks by summed radiative power, strongest first', () => {
    const found = hotspotsFrom(twoClusters(), SF);
    expect(found[0]?.rank).toBe(1);
    expect(found[0]?.totalFrp).toBeCloseTo(120);
    expect(found[1]?.totalFrp).toBeCloseTo(15);
  });

  it('puts the centre on the energy, not the middle of the bounding box', () => {
    // One hot pixel and three faint ones: the marker belongs on the hot one.
    const skewed = [
      detection({ latitude: 39.0, longitude: -121.0, frp: 100 }),
      detection({ latitude: 39.1, longitude: -121.1, frp: 1 }),
      detection({ latitude: 39.12, longitude: -121.12, frp: 1 }),
    ];
    const [hotspot] = hotspotsFrom(skewed, SF);
    expect(hotspot?.latitude).toBeLessThan(39.02);
  });

  it('keeps the peak brightness and the latest pass', () => {
    const group = [
      detection({ latitude: 39, longitude: -121, brightness_k: 310, acquired_at: '2026-08-20T00:00:00Z' }),
      detection({ latitude: 39.01, longitude: -121.01, brightness_k: 355, acquired_at: '2026-08-24T00:00:00Z' }),
    ];
    const [hotspot] = hotspotsFrom(group, SF);
    expect(hotspot?.peakBrightnessK).toBe(355);
    expect(hotspot?.lastSeen).toBe('2026-08-24T00:00:00Z');
  });

  it('reports no brightness rather than zero when the feed omitted it', () => {
    // Zero kelvin is 273 degrees of claim.
    const [hotspot] = hotspotsFrom([detection({ brightness_k: null })], SF);
    expect(hotspot?.peakBrightnessK).toBeNull();
  });

  it('counts the confidence flags instead of averaging them', () => {
    const group = [
      detection({ latitude: 39, longitude: -121, confidence: 'h' }),
      detection({ latitude: 39.01, longitude: -121.01, confidence: 'n' }),
      detection({ latitude: 39.02, longitude: -121.02, confidence: 'n' }),
    ];
    const [hotspot] = hotspotsFrom(group, SF);
    expect(hotspot?.confidence).toEqual({ high: 1, nominal: 2, low: 0, unknown: 0 });
  });

  it('caps the numbered pins without dropping anything from the totals', () => {
    // Twelve separate fires, six pins. The rest stay in the field and in the
    // region total -- the numbering is a reading order, not a filter.
    const many = Array.from({ length: 12 }, (_, i) =>
      detection({ latitude: 37 + i * 0.5, longitude: -122, frp: i + 1 }),
    );
    expect(hotspotsFrom(many, SF)).toHaveLength(6);
    expect(totalFrp(many)).toBeCloseTo(78);
  });

  it('survives a cluster where nothing reported a power', () => {
    const [hotspot] = hotspotsFrom(
      [detection({ frp: null, latitude: 39, longitude: -121 })],
      SF,
    );
    expect(hotspot?.totalFrp).toBe(0);
    expect(hotspot?.latitude).toBeCloseTo(39);
  });
});

describe('the regional heat map', () => {
  it('says the display cannot draw rather than showing an empty frame', () => {
    render(<RegionalHeatMap activity={activity()} basemap={BASEMAP} webgl={false} />);
    expect(screen.getByText(/no WebGL2/i)).toBeInTheDocument();
  });

  it('still carries the key when the map cannot be drawn', () => {
    // The key is where the units, the relativity and the exaggeration are
    // stated. Losing it with the canvas would leave numbers with no meaning.
    render(
      <RegionalHeatMap
        activity={activity()}
        basemap={BASEMAP}
        webgl={false}
      />,
    );
    expect(screen.getByText(/fire radiative power/i)).toBeInTheDocument();
    expect(screen.getByText(/relative to the busiest area/i)).toBeInTheDocument();
  });

  it('declares the vertical exaggeration rather than leaving it implied', () => {
    // An unlabelled exaggeration is a claim about how steep the country is.
    render(
      <RegionalHeatMap
        activity={activity()}
        basemap={BASEMAP}
        webgl={false}
      />,
    );
    expect(screen.getByText(/vertical exaggeration/i)).toBeInTheDocument();
    expect(screen.getByText(/the shape is real, the steepness is not/i)).toBeInTheDocument();
  });

  it('leads with the distance to the nearest detection', () => {
    render(
      <RegionalHeatMap
        activity={activity({ detections: [detection({ latitude: 37.9, longitude: -122.3 })] })}
        basemap={BASEMAP}
        webgl={false}
      />,
    );
    expect(screen.getByTestId('regional-heat-lede')).toHaveTextContent(/Nearest detection/i);
    expect(screen.getByTestId('regional-heat-lede')).toHaveTextContent(/km from San Francisco/i);
  });

  it('claims no distance when there is no city box to measure from', () => {
    render(
      <RegionalHeatMap activity={activity({ cityBBox: null })} basemap={BASEMAP} webgl={false} />,
    );
    expect(screen.getByTestId('regional-heat-lede')).toHaveTextContent(/no distance is claimed/i);
  });

  it('says an empty region is empty rather than drawing nothing silently', () => {
    render(
      <RegionalHeatMap
        activity={activity({ detections: [], regionalCount: 0 })}
        basemap={BASEMAP}
        webgl={false}
      />,
    );
    expect(screen.getByTestId('regional-heat-lede')).toHaveTextContent(/No detections/i);
  });

  it('renders the backend resolution note verbatim rather than its own wording', () => {
    // The sentence that makes "0 in the city" read as the instrument working.
    // Read from the payload so the console cannot drift from what the port
    // actually claims about the product.
    render(<RegionalHeatMap activity={activity()} basemap={BASEMAP} webgl={false} />);
    expect(screen.getByText(/VIIRS pixels are ~375 m/)).toBeInTheDocument();
  });

  it('falls back to its own explanation when the payload carries no note', () => {
    render(
      <RegionalHeatMap
        activity={activity({ resolution_note: null })}
        basemap={BASEMAP}
        webgl={false}
      />,
    );
    expect(screen.getByText(/Why the city is always empty/i)).toBeInTheDocument();
  });

  it('shows the map attribution, because the licence requires it', () => {
    render(<RegionalHeatMap activity={activity()} basemap={BASEMAP} webgl={false} />);
    expect(screen.getByText('Map data © Google')).toBeInTheDocument();
  });

  it('distinguishes a failed request from an answered refusal', () => {
    const { rerender } = render(
      <RegionalHeatMap activity={null} basemap={null} error="fetch failed" webgl={false} />,
    );
    expect(screen.getByText(/Fire-activity request failed/i)).toBeInTheDocument();

    rerender(
      <RegionalHeatMap
        activity={activity({ available: false, unavailable_reason: 'no map key' })}
        basemap={null}
        webgl={false}
      />,
    );
    expect(screen.getByText(/UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByText(/no map key/)).toBeInTheDocument();
  });

  it('puts the frame on screen when activity arrives after a refusal', () => {
    // The sequence that broke it. The frame lives only in the success branch,
    // so the first render -- before any fire-activity answer -- has no frame in
    // it. A mount effect holding a `useRef` therefore ran once against `null`
    // and never again, and the map sat on "Drawing the region…" forever while
    // every other part of the panel worked. A callback ref attaches whenever
    // the node does; this asserts the node is actually there on the second
    // render, which is the precondition for that.
    const { rerender } = render(
      <RegionalHeatMap activity={null} basemap={null} webgl={false} />,
    );
    expect(screen.queryByTestId('regional-heat-canvas')).not.toBeInTheDocument();

    rerender(<RegionalHeatMap activity={activity()} basemap={BASEMAP} webgl={false} />);
    expect(screen.getByTestId('regional-heat-canvas')).toBeInTheDocument();
  });

  it('draws without a ground plane rather than refusing to draw at all', () => {
    // A missing basemap costs the coastline, not the map. The panel must not
    // treat it as a failure.
    render(<RegionalHeatMap activity={activity()} basemap={null} webgl={false} />);
    expect(screen.queryByText(/request failed/i)).not.toBeInTheDocument();
    expect(screen.getByTestId('regional-heat-lede')).toBeInTheDocument();
  });
});
