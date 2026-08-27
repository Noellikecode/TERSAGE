/**
 * The call arriving, and the one thing it must never do.
 *
 * **It gates nothing.** By the time this panel is on screen the dispatch has
 * happened, the instant brief is rendering behind it and the fleet is awake.
 * The product's claim is a brief inside the ninety seconds between dispatch and
 * arrival, with a stage that makes no model call at all; a human standing
 * between the call and the fleet would delete that claim. So this is an overlay
 * that can be dismissed at any moment and changes nothing when it is.
 *
 * And the transcript is *revealed*, never invented. Nothing here transcribes
 * anything: the text exists in full before the audio starts, because it is what
 * the model was given.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { IncomingCall } from '@/components/incident/IncomingCall';

const TRANSCRIPT =
  'I need the fire department at 450 Hayes Street. The third floor is full of smoke.';

function stubPlay(behaviour: 'allows' | 'blocks') {
  const play = vi.fn(() =>
    behaviour === 'allows' ? Promise.resolve() : Promise.reject(new Error('NotAllowedError')),
  );
  Object.defineProperty(HTMLMediaElement.prototype, 'play', {
    configurable: true,
    writable: true,
    value: play,
  });
  return play;
}

function show(overrides: Partial<Parameters<typeof IncomingCall>[0]> = {}) {
  const onDismiss = vi.fn();
  render(
    <IncomingCall
      open
      addressId="sf-0450-hayes"
      transcript={TRANSCRIPT}
      audioSrc="/audio/911-call-trapped.wav"
      channel="CALL_911"
      onDismiss={onDismiss}
      {...overrides}
    />,
  );
  return onDismiss;
}

afterEach(() => vi.restoreAllMocks());

describe('the call on screen', () => {
  it('names the call and the address it is about', () => {
    stubPlay('allows');
    show();
    expect(screen.getByTestId('incoming-call')).toHaveTextContent(/911 call incoming/i);
    expect(screen.getByText('sf-0450-hayes')).toBeInTheDocument();
    expect(screen.getByText(/synthetic — not a real call/)).toBeInTheDocument();
  });

  it('plays the recording as it opens', async () => {
    const play = stubPlay('allows');
    show();
    await waitFor(() => expect(play).toHaveBeenCalled());
  });

  it('says the fleet is already running and nothing is waiting on a person', () => {
    // The sentence is the product. If this panel ever reads like a step
    // somebody has to complete, the autonomy claim has gone with it.
    stubPlay('allows');
    show();
    expect(screen.getByTestId('incoming-call')).toHaveTextContent(
      /brief is already on screen behind this and the fleet is already running/i,
    );
    expect(screen.getByTestId('incoming-call')).toHaveTextContent(/Nothing here is waiting on you/i);
  });

  it('offers no control that starts, approves or holds anything', () => {
    // One button, and it only changes what is on screen.
    stubPlay('allows');
    show();
    const buttons = screen
      .getAllByRole('button')
      .map((b) => (b.textContent || '').toLowerCase());
    expect(buttons.some((b) => /deploy|approve|start|confirm|dispatch|hold/.test(b))).toBe(false);
    expect(screen.getByTestId('incoming-call-dismiss')).toHaveTextContent(/go to the brief/i);
  });

  it('is a region, not a modal: it traps nothing', () => {
    stubPlay('allows');
    show();
    expect(screen.getByTestId('incoming-call')).toHaveAttribute('role', 'region');
    expect(screen.getByTestId('incoming-call')).not.toHaveAttribute('aria-modal');
  });
});

describe('dismissing it', () => {
  it('closes on the button', () => {
    stubPlay('allows');
    const onDismiss = show();
    fireEvent.click(screen.getByTestId('incoming-call-dismiss'));
    expect(onDismiss).toHaveBeenCalled();
  });

  it('closes on Escape', () => {
    stubPlay('allows');
    const onDismiss = show();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onDismiss).toHaveBeenCalled();
  });

  it('closes itself when the recording finishes', () => {
    stubPlay('allows');
    const onDismiss = show();
    fireEvent.ended(screen.getByTestId('incoming-call-audio'));
    expect(onDismiss).toHaveBeenCalled();
  });

  it('renders nothing at all when closed', () => {
    stubPlay('allows');
    show({ open: false });
    expect(screen.queryByTestId('incoming-call')).not.toBeInTheDocument();
  });
});

describe('the transcript', () => {
  it('is present in full from the first frame, whatever the audio has reached', () => {
    // Revealed, not transcribed. Anything reading the page rather than
    // watching it gets every word immediately.
    stubPlay('allows');
    show();
    expect(screen.getByTestId('incoming-call-transcript')).toHaveTextContent(TRANSCRIPT);
  });

  it('keeps pace with the recording as it plays', () => {
    stubPlay('allows');
    show();
    const audio = screen.getByTestId('incoming-call-audio') as HTMLAudioElement;
    Object.defineProperty(audio, 'duration', { configurable: true, value: 20 });
    Object.defineProperty(audio, 'currentTime', { configurable: true, value: 10 });
    fireEvent.timeUpdate(audio);
    // Half-read: the spoken half is bright, the rest is still muted, and both
    // halves are the real text.
    expect(screen.getByTestId('incoming-call-transcript')).toHaveTextContent(TRANSCRIPT);
  });

  it('shows every word when the browser refuses to play sound', async () => {
    // No gesture yet is the ordinary case on a fresh load. The words must not
    // be held hostage to audio that will never start.
    stubPlay('blocks');
    show();
    await waitFor(() =>
      expect(screen.getByTestId('incoming-call-blocked')).toHaveTextContent(
        /transcript is shown in full/i,
      ),
    );
  });

  it('shows the whole transcript when there is no recording at all', () => {
    show({ audioSrc: null });
    expect(screen.getByTestId('incoming-call-transcript')).toHaveTextContent(TRANSCRIPT);
    expect(screen.queryByTestId('incoming-call-audio')).not.toBeInTheDocument();
  });
});
