# Call audio

One file per sample call in `components/standby/DispatchPanel.tsx`, named by the
sample's `id`:

    911-call-<id>.wav   or   911-call-<id>.mp3

`SampleCall.audioSrc` points at it. To swap the placeholder for a real
recording, drop the file in here and change that one string — nothing else in
the console knows the filename.

Every recording here is **synthetic**: a read of the transcript beside it, not
a real 911 call, not a real caller, and not a real person's emergency. The
player says so on screen next to whatever is loaded, and that label is not
optional -- a recording of this demo must never be mistakable for real
emergency audio.

**The audio must match its transcript word for word.** The console highlights
the exact phrases the model pulled each fact from, so a recording that says
"propane tanks" against a transcript reading "propane cylinders" points a
highlight at words nobody spoke.

A missing file is not a broken player. `CallAudio` reports that no recording is
attached and the incident runs exactly as it does today -- the transcript is
what the model reads, and the audio is never the input to anything.
