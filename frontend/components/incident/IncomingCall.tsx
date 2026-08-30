'use client';

/**
 * The call arriving, full screen, while the brief lands behind it.
 *
 * **It gates nothing.** The dispatch has already happened when this appears:
 * the instant brief is rendering underneath, the agents are being woken by
 * their declared capabilities, and none of it is waiting on anybody to read,
 * edit or approve what is on this panel. That is the product's whole claim --
 * a brief in the ninety seconds between dispatch and arrival, with a 500ms
 * stage that makes no model call at all -- and a human standing between the
 * call and the fleet would delete it.
 *
 * So this is an overlay, not a modal. Escape closes it, the button closes it,
 * the end of the recording closes it, and closing it changes nothing except
 * what is on screen.
 *
 * **The transcript is revealed, never invented.** It exists in full before the
 * audio starts -- it is what the model was given and what every extracted line
 * is bound back to. The reveal is paced to the recording because that is how a
 * person hears it, and the whole text is exposed to assistive tech from the
 * first frame. This is not speech-to-text and does not claim to be: nothing
 * here transcribes anything.
 */

import { useEffect, useRef, useState } from 'react';

export interface IncomingCallProps {
  open: boolean;
  /** Where the call is about. Displayed, never parsed. */
  addressId: string;
  /** The words the recording says, in full, from the first frame. */
  transcript: string;
  /** The recording. Absent is fine -- the panel still shows the transcript. */
  audioSrc?: string | null;
  channel: 'CALL_911' | 'CAD_NARRATIVE';
  onDismiss: () => void;
}

/**
 * How long the transcript waits for the recording before showing itself.
 *
 * Long enough that a recording which starts promptly still paces the words,
 * short enough that nobody watches an empty card wondering whether the call
 * failed. A dispatch overlay has about a second of credibility.
 */
const TRANSCRIPT_GRACE_MS = 1200;

export function IncomingCall({
  open,
  addressId,
  transcript,
  audioSrc,
  channel,
  onDismiss,
}: IncomingCallProps) {
  const audio = useRef<HTMLAudioElement | null>(null);
  // How much of the transcript the recording has reached. Starts whole when
  // there is no recording to pace it against -- a silent call is not a call
  // with no words.
  const [spoken, setSpoken] = useState(audioSrc ? 0 : 1);
  const [blocked, setBlocked] = useState(false);

  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onDismiss();
    };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, [open, onDismiss]);

  useEffect(() => {
    if (!open) {
      setSpoken(audioSrc ? 0 : 1);
      setBlocked(false);
      return;
    }
    const el = audio.current;
    if (!el) return;
    // `play()` does not always return a promise.
    //
    // It is specified to, and every browser this console runs in does -- but
    // jsdom returns `undefined`, and so do browsers old enough to predate the
    // promise form. Calling `.then` on that throws out of an effect and takes
    // the overlay down, which is a hard failure over an autoplay nicety.
    const started = el.play();
    if (!started) {
      setBlocked(false);
      return;
    }
    void started
      .then(() => setBlocked(false))
      .catch(() => {
        // No gesture yet, so the browser refuses sound. The words still need
        // to be readable, so the reveal gives up its pacing rather than
        // holding the transcript hostage to audio that will not play.
        setBlocked(true);
        setSpoken(1);
      });
  }, [open, audioSrc]);

  /**
   * The words arrive whether or not the recording does.
   *
   * `spoken` opens at 0 when there is audio, because the transcript is paced
   * against the recording -- and that is right once the recording is playing.
   * It is wrong in the seconds before: a call that has not finished loading
   * put an empty card on screen at the exact moment a dispatch arrived, which
   * reads as a broken overlay rather than as audio still buffering.
   *
   * So the pacing gets a grace period and no more. If nothing has been spoken
   * by the time it lapses, the transcript is revealed whole -- the same answer
   * the autoplay-refused path already gives, for the same reason: the words
   * are the point and the pacing is a nicety. Audio that arrives later simply
   * plays under a transcript already on screen.
   */
  useEffect(() => {
    if (!open || !audioSrc) return;
    const grace = setTimeout(() => {
      setSpoken((current) => (current > 0 ? current : 1));
    }, TRANSCRIPT_GRACE_MS);
    return () => clearTimeout(grace);
  }, [open, audioSrc]);

  if (!open) return null;

  const label = channel === 'CALL_911' ? '911 call incoming' : 'CAD narrative incoming';
  const shown = Math.max(0, Math.min(transcript.length, Math.round(transcript.length * spoken)));

  return (
    <div
      // A region, not a dialog: it is not modal, it traps nothing, and the
      // console behind it is live and being updated the entire time.
      role="region"
      aria-label={label}
      data-testid="incoming-call"
      className="fixed inset-0 z-50 flex items-center justify-center bg-ground/95 p-6"
    >
      <div className="w-full max-w-3xl border border-alarm bg-surface p-6">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h2 className="flex items-center gap-2 text-title uppercase tracking-widest text-alarm">
            {/* A steady mark, not a pulse. A blinking light on a fireground
                display is an alarm state, and this is an ordinary dispatch. */}
            <span aria-hidden="true">■</span>
            {label}
          </h2>
          <span className="font-mono text-body text-ink">{addressId}</span>
        </div>

        {audioSrc && (
          <audio
            ref={audio}
            src={audioSrc}
            controls
            preload="auto"
            aria-label={`${label}, synthetic recording`}
            data-testid="incoming-call-audio"
            className="mt-4 w-full"
            onTimeUpdate={(event) => {
              const el = event.currentTarget;
              if (el.duration > 0) setSpoken(el.currentTime / el.duration);
            }}
            onEnded={() => {
              setSpoken(1);
              onDismiss();
            }}
            onError={() => setSpoken(1)}
          />
        )}

        {/* The transcript. Revealed at the pace of the recording, and present
            in full for anything that reads the page rather than watches it. */}
        <p className="mt-4 text-body leading-7 text-ink" data-testid="incoming-call-transcript">
          <span aria-hidden="true">
            {transcript.slice(0, shown)}
            <span className="text-muted">{transcript.slice(shown)}</span>
          </span>
          <span className="sr-only">{transcript}</span>
        </p>

        {blocked && (
          <p className="mt-2 text-micro text-muted" data-testid="incoming-call-blocked">
            The browser held the audio until someone presses play. The transcript is shown in full.
          </p>
        )}

        <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
          <p className="text-micro leading-5 text-muted">
            {/* Said plainly, because it is the product. */}
            The brief is already on screen behind this and the fleet is already running. Nothing
            here is waiting on you.{' '}
            <span className="text-disputed">synthetic — not a real call</span>
          </p>
          <button
            type="button"
            onClick={onDismiss}
            data-testid="incoming-call-dismiss"
            className="shrink-0 border border-line px-3 py-1.5 text-micro uppercase tracking-widest text-ink hover:border-live focus-visible:outline focus-visible:outline-2 focus-visible:outline-live"
          >
            Go to the brief
          </button>
        </div>
      </div>
    </div>
  );
}
