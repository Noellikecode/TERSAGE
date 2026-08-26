/**
 * The photorealistic view's states, and the one that deadlocked.
 *
 * The bug worth a test: the error states used to render a bare paragraph
 * *instead of* the canvas mount node. So when coordinates arrived a render
 * later, the effect re-ran, found `mount.current` still null, and returned
 * without setting a status -- leaving a message about missing coordinates on
 * screen permanently, over a request that had already succeeded.
 *
 * The invariant that prevents it: the mount node is in the tree in every state.
 */

import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PhotorealisticModel } from '@/components/PhotorealisticModel';

/** The mount node the effect draws into. Present in every state or the
    component cannot recover from one. */
function mountNode(container: HTMLElement): HTMLElement | null {
  return container.querySelector('div.bg-ground');
}

/** The key gate runs first, and rightly: with no key nothing else matters.
    So the geometry-state branches need one present to be reachable at all. */
function withKey() {
  vi.stubEnv('NEXT_PUBLIC_GOOGLE_MAPS_API_KEY', 'test-key');
}

describe('the photorealistic view', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('keeps its canvas mount node in the tree while it is waiting', () => {
    withKey();
    const { container } = render(
      <PhotorealisticModel latitude={null} longitude={null} label="sf-0450-hayes" geometryState="loading" />,
    );
    expect(mountNode(container)).not.toBeNull();
    expect(screen.getByText(/Locating the structure/)).toBeInTheDocument();
  });

  it('keeps it while unconfigured, so a later fix can take effect', () => {
    const { container } = render(
      <PhotorealisticModel latitude={37.77} longitude={-122.42} label="sf-0450-hayes" geometryState="ready" />,
    );
    // No key is set under test, so this is the unconfigured branch.
    expect(mountNode(container)).not.toBeNull();
    expect(screen.getByText(/NEXT_PUBLIC_GOOGLE_MAPS_API_KEY is not set/)).toBeInTheDocument();
  });

  it('does not blame the backend while the request is still open', () => {
    withKey();
    render(
      <PhotorealisticModel latitude={null} longitude={null} label="sf-0450-hayes" geometryState="loading" />,
    );
    expect(screen.queryByText(/without coordinates/)).toBeNull();
  });

  it('says no geometry reached the console when the fetch failed', () => {
    withKey();
    render(
      <PhotorealisticModel latitude={null} longitude={null} label="sf-0450-hayes" geometryState="unavailable" />,
    );
    expect(screen.getByText(/no geometry for this structure reached the console/i)).toBeInTheDocument();
  });
});
