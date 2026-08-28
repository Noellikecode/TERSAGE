'use client';

/**
 * The 911 call, playing while the brief assembles.
 *
 * Three things this is careful about.
 *
 * **The audio is never an input.** The transcript is what the model reads and
 * what every extracted line is bound back to; this element plays a recording of
 * the same call for a person in the room. Nothing downstream consumes it, so a
 * missing file, a codec the browser will not take, or a blocked autoplay costs
 * the sound and never the brief.
 *
 * **A recording that is not there says so.** Rendering a dead player would
 * teach an officer that this call had no audio, which is a claim. An absent
 * file reports itself the way an absent record does everywhere else here.
 *
 * **Autoplay is asked for, never assumed.** Browsers refuse audible playback
 * until the page has been interacted with, and on a fresh demo load it usually
 * has not been. The attempt is made, the refusal is caught, and the controls
 * are on screen either way -- so the operator presses play rather than
 * wondering why the console is silent.
 */

import { useEffect, useRef, useState } from 'react';

export interface CallAudioProps {
  /** Path under `public/`. Absent when this call has no recording attached. */
  src?: string | null;
  /** What the recording is of, for the label and the accessible name. */
  label: string;
  /** Attempt playback as soon as it is mounted. The demo's call does. */
  autoPlay?: boolean;
}

type Status = 'idle' | 'playing' | 'blocked' | 'missing';

export function CallAudio({ src, label, autoPlay = false }: CallAudioProps) {
  const ref = useRef<HTMLAudioElement | null>(null);
  const [status, setStatus] = useState<Status>('idle');

  useEffect(() => {
    setStatus('idle');
    if (!src || !autoPlay) return;
    const el = ref.current;
    if (!el) return;
    // `play()` rejects when the browser has no gesture to justify sound. That
    // is the ordinary case on a fresh load and is not an error worth showing
    // as one -- the controls below are the answer.
    //
    // And it does not always return a promise to reject: it is specified to,
    // and every browser this console runs in does, but jsdom returns
    // `undefined`. Calling `.then` on that throws out of the effect and takes
    // the panel down -- a hard failure over an autoplay nicety. Same guard as
    // `IncomingCall`, for the same reason.
    const started = el.play();
    if (!started) {
      setStatus('idle');
      return;
    }
    void started.then(() => setStatus('playing')).catch(() => setStatus('blocked'));
  }, [src, autoPlay]);

  if (!src) {
    return (
      <p
        className="border border-dashed border-line p-3 text-micro leading-5 text-muted"
        data-testid="call-audio-absent"
      >
        No recording attached to this call. The transcript below is what was read.
      </p>
    );
  }

  return (
    <figure className="m-0" data-testid="call-audio">
      <figcaption className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-micro uppercase tracking-widest text-muted">{label}</span>
        {/* Never let a recording of the demo be mistaken for real 911 audio. */}
        <span className="text-micro text-disputed">synthetic — not a real call</span>
      </figcaption>

      <audio
        ref={ref}
        src={src}
        controls
        preload="auto"
        aria-label={`${label}, synthetic recording`}
        data-testid="call-audio-player"
        className="mt-1 w-full"
        onPlay={() => setStatus('playing')}
        onError={() => setStatus('missing')}
      >
        {/* Browsers that cannot play it still say what is here. */}
        Your browser cannot play this recording.
      </audio>

      {status === 'blocked' && (
        <p className="mt-1 text-micro text-muted" data-testid="call-audio-blocked">
          The browser held the audio until someone presses play. The brief did not wait.
        </p>
      )}
      {status === 'missing' && (
        <p className="mt-1 text-micro text-disputed" data-testid="call-audio-failed">
          The recording could not be loaded. The transcript is unaffected — it is what the model
          read.
        </p>
      )}
    </figure>
  );
}
