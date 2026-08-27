/**
 * The call, playing beside what was read out of it.
 *
 * The rule this holds: **the audio is never an input.** The transcript is what
 * the model reads and what every extracted line is bound back to. A missing
 * file, a codec the browser refuses, or an autoplay policy costs the sound and
 * never the brief — so each of those has to render as itself rather than as a
 * dead player an officer would read as "this call had no audio".
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CallAudio } from '@/components/incident/CallAudio';
import { DispatchPanel, SAMPLE_CALLS } from '@/components/standby/DispatchPanel';

/** Stand in for the browser's playback, which jsdom does not implement. */
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

afterEach(() => vi.restoreAllMocks());

describe('the recording', () => {
  it('renders a real player, labelled and marked synthetic', () => {
    stubPlay('allows');
    render(<CallAudio src="/audio/911-call-trapped.wav" label="911 call" />);

    const player = screen.getByTestId('call-audio-player');
    expect(player).toHaveAttribute('src', '/audio/911-call-trapped.wav');
    expect(player).toHaveAttribute('controls');
    // A recording of the demo must never be mistakable for real 911 audio.
    expect(screen.getByText(/synthetic — not a real call/)).toBeInTheDocument();
    expect(player).toHaveAccessibleName(/911 call, synthetic recording/);
  });

  it('asks to play when the call arrives', async () => {
    const play = stubPlay('allows');
    render(<CallAudio src="/audio/911-call-trapped.wav" label="911 call" autoPlay />);
    await waitFor(() => expect(play).toHaveBeenCalled());
  });

  it('does not play on its own when nobody asked it to', () => {
    const play = stubPlay('allows');
    render(<CallAudio src="/audio/911-call-trapped.wav" label="911 call" />);
    expect(play).not.toHaveBeenCalled();
  });

  it('says the browser held the sound rather than sitting silent', async () => {
    // Browsers refuse audible playback without a gesture, which is the ordinary
    // case on a fresh demo load. The controls are the answer, and the console
    // says so instead of looking broken.
    stubPlay('blocks');
    render(<CallAudio src="/audio/911-call-trapped.wav" label="911 call" autoPlay />);
    await waitFor(() =>
      expect(screen.getByTestId('call-audio-blocked')).toHaveTextContent(
        /held the audio until someone presses play/,
      ),
    );
    // And it is explicit that this cost nothing.
    expect(screen.getByTestId('call-audio-blocked')).toHaveTextContent(/brief did not wait/);
  });

  it('reports a recording it could not load, and says the transcript is unaffected', async () => {
    stubPlay('allows');
    render(<CallAudio src="/audio/missing.wav" label="911 call" autoPlay />);
    fireEvent.error(screen.getByTestId('call-audio-player'));
    await waitFor(() =>
      expect(screen.getByTestId('call-audio-failed')).toHaveTextContent(
        /transcript is unaffected/,
      ),
    );
  });

  it('says a call has no recording rather than drawing a dead player', () => {
    // "This call had no audio" is a claim. An absent file reports itself the
    // way an absent record does everywhere else here.
    render(<CallAudio src={null} label="911 call" autoPlay />);
    expect(screen.getByTestId('call-audio-absent')).toHaveTextContent(/No recording attached/);
    expect(screen.queryByTestId('call-audio-player')).not.toBeInTheDocument();
  });
});

describe('which recording travels with a dispatch', () => {
  it('sends the sample’s audio alongside its transcript', () => {
    const onDispatch = vi.fn();
    render(<DispatchPanel addressId="sf-0450-hayes" busy={false} onDispatch={onDispatch} />);

    const withAudio = SAMPLE_CALLS.find((s) => s.audioSrc)!;
    fireEvent.click(screen.getByRole('button', { name: withAudio.label }));
    fireEvent.click(screen.getByTestId('dispatch-button'));

    expect(onDispatch).toHaveBeenCalledWith(
      'sf-0450-hayes',
      withAudio.text,
      withAudio.channel,
      withAudio.audioSrc,
    );
  });

  it('drops the recording the moment the transcript is edited', () => {
    // A recording that no longer matches the text is worse than none: the two
    // would disagree on screen, and the transcript is the one that is read.
    const onDispatch = vi.fn();
    render(<DispatchPanel addressId="sf-0450-hayes" busy={false} onDispatch={onDispatch} />);

    const withAudio = SAMPLE_CALLS.find((s) => s.audioSrc)!;
    fireEvent.click(screen.getByRole('button', { name: withAudio.label }));
    fireEvent.change(screen.getByLabelText(/transcript or CAD narrative/i), {
      target: { value: 'Something else entirely.' },
    });
    fireEvent.click(screen.getByTestId('dispatch-button'));

    expect(onDispatch).toHaveBeenCalledWith(
      'sf-0450-hayes',
      'Something else entirely.',
      withAudio.channel,
      undefined,
    );
  });

  it('ships a recording for the call the demo dispatches', () => {
    // The demo timer opens `SAMPLE_CALLS[0]`. If that one has no audio the
    // countdown lands on a silent incident, which is the whole point of this.
    expect(SAMPLE_CALLS[0]?.audioSrc).toBeTruthy();
  });
});
