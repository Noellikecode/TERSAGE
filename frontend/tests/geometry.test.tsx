/**
 * The geometry view: WebGL, its absence, and what the picture says out loud.
 *
 * jsdom has no WebGL, so these exercise exactly the path a locked-down tablet
 * takes -- which is the point. The static SVG has to carry the same disputed
 * mass the interactive view would.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { StructureModel, describeGeometry, faceLabelFor, ThermalLegend, thermalStep } from '@/components/StructureModel';
import { GEOMETRY, GEOMETRY_SCANNED } from './fixtures';

describe('the geometry view', () => {
  it('falls back to the static SVG when WebGL is unavailable', () => {
    render(<StructureModel geometry={GEOMETRY} forceFallback />);
    expect(screen.getByTestId('geometry-svg')).toBeInTheDocument();
    expect(screen.queryByTestId('geometry-canvas')).not.toBeInTheDocument();
  });

  it('says why it is showing the fallback', () => {
    render(<StructureModel geometry={GEOMETRY} forceFallback />);
    expect(screen.getByText(/static elevation/i)).toBeInTheDocument();
  });

  it('marks the disputed mass in the fallback, not only in the canvas', () => {
    render(<StructureModel geometry={GEOMETRY} forceFallback />);
    // The backend's SVG carries the dashed outline and the word.
    expect(screen.getByTestId('geometry-svg').innerHTML).toContain('DISPUTED');
    expect(screen.getByTestId('geometry-svg').innerHTML).toContain('stroke-dasharray');
  });

  it('describes the structure for a screen reader, disputes included', () => {
    render(<StructureModel geometry={GEOMETRY} forceFallback />);
    const description = screen.getByRole('img').getAttribute('aria-label') ?? '';
    expect(description).toContain('3 levels');
    expect(description).toContain('DISPUTED');
    expect(description).toContain('Collapse zone');
    expect(description).toContain('UNSCANNED, not cool');
  });

  it('renders an explicit panel when there is no geometry at all', () => {
    render(<StructureModel geometry={null} />);
    expect(screen.getByText('No geometry on record')).toBeInTheDocument();
    expect(
      screen.getByText(/absence of measurement, not a measurement/i),
    ).toBeInTheDocument();
  });
});

describe('the geometry description', () => {
  it('names the face an officer would say over the radio', () => {
    expect(faceLabelFor('CHARLIE')).toBe('CHARLIE face');
    expect(faceLabelFor('ISO')).toBe('Isometric');
  });

  it('states the collapse zone as a convention, not a prediction', () => {
    const description = describeGeometry(GEOMETRY, 'ISO');
    expect(description).toContain('1.5x measured-height convention');
    expect(description).not.toMatch(/will collapse/i);
  });

  it('never describes an unscanned face as cool or clear', () => {
    const description = describeGeometry(GEOMETRY, 'ISO');
    expect(description).toMatch(/UNSCANNED/);
    expect(description).not.toMatch(/\bcool\b(?!\.)/i);
    expect(description).not.toMatch(/\bclear\b/i);
  });
});

describe('the thermal heat map', () => {
  it('reads every measured cell as a number, not colour alone', () => {
    render(<ThermalLegend faces={GEOMETRY_SCANNED.spec.faces} />);
    // The two darkest ramp steps fall below 3:1 against the surface, so these
    // labels are what makes the overlay legible at all.
    expect(screen.getByText('22 C')).toBeInTheDocument();
    expect(screen.getByText('120 C')).toBeInTheDocument();
    expect(screen.getByText('340 C')).toBeInTheDocument();
  });

  it('lists only faces that were actually flown', () => {
    render(<ThermalLegend faces={GEOMETRY_SCANNED.spec.faces} />);
    expect(screen.getByText('ALPHA')).toBeInTheDocument();
    // An unflown face must not appear with a temperature beside it.
    expect(screen.queryByText('BRAVO')).not.toBeInTheDocument();
    expect(screen.queryByText('CHARLIE')).not.toBeInTheDocument();
  });

  it('renders nothing at all when no face has been flown', () => {
    const { container } = render(<ThermalLegend faces={GEOMETRY.spec.faces} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says the caveat where the reading is', () => {
    render(<ThermalLegend faces={GEOMETRY_SCANNED.spec.faces} />);
    expect(screen.getByText(/cannot see through walls/i)).toBeInTheDocument();
    expect(screen.getByText(/UNSCANNED, not cool/i)).toBeInTheDocument();
  });

  it('puts the measured readings in the screen-reader description', () => {
    const described = describeGeometry(GEOMETRY_SCANNED, 'ALPHA');
    expect(described).toContain('ALPHA measured ground up: 22 C, 120 C, 340 C');
    expect(described).toContain('Surface temperature only');
  });

  it('maps temperature onto a monotonic ramp', () => {
    // Hotter is never a lower step. That is the whole contract of a sequential
    // ramp, and it is what lets a reader order two cells without a legend.
    const steps = [20, 60, 140, 250, 400, 900].map(thermalStep);
    expect(steps).toEqual([...steps].sort((a, b) => a - b));
    expect(thermalStep(-40)).toBe(0);
    expect(thermalStep(9000)).toBe(4);
  });
});
