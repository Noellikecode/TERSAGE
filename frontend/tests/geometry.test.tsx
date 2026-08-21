/**
 * The geometry view: WebGL, its absence, and what the picture says out loud.
 *
 * jsdom has no WebGL, so these exercise exactly the path a locked-down tablet
 * takes -- which is the point. The static SVG has to carry the same disputed
 * mass the interactive view would.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { GeometryCanvas, describeGeometry, faceLabelFor } from '@/components/GeometryCanvas';
import { GEOMETRY } from './fixtures';

describe('the geometry view', () => {
  it('falls back to the static SVG when WebGL is unavailable', () => {
    render(<GeometryCanvas geometry={GEOMETRY} forceFallback />);
    expect(screen.getByTestId('geometry-svg')).toBeInTheDocument();
    expect(screen.queryByTestId('geometry-canvas')).not.toBeInTheDocument();
  });

  it('says why it is showing the fallback', () => {
    render(<GeometryCanvas geometry={GEOMETRY} forceFallback />);
    expect(screen.getByText(/static elevation/i)).toBeInTheDocument();
  });

  it('marks the disputed mass in the fallback, not only in the canvas', () => {
    render(<GeometryCanvas geometry={GEOMETRY} forceFallback />);
    // The backend's SVG carries the dashed outline and the word.
    expect(screen.getByTestId('geometry-svg').innerHTML).toContain('DISPUTED');
    expect(screen.getByTestId('geometry-svg').innerHTML).toContain('stroke-dasharray');
  });

  it('describes the structure for a screen reader, disputes included', () => {
    render(<GeometryCanvas geometry={GEOMETRY} forceFallback />);
    const description = screen.getByRole('img').getAttribute('aria-label') ?? '';
    expect(description).toContain('3 levels');
    expect(description).toContain('DISPUTED');
    expect(description).toContain('Collapse zone');
    expect(description).toContain('UNSCANNED, not cool');
  });

  it('renders an explicit panel when there is no geometry at all', () => {
    render(<GeometryCanvas geometry={null} />);
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
