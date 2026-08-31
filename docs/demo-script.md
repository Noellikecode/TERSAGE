# TERSAGE — demo video script

Read by a human, not a synthetic voice. Stage directions in *italics*.

Everything here is true of the built system. Model ids are the ones this build
is configured with (`GEMINI_MODEL`, `GEMMA_MODEL` in settings), so a judge
checking the repo finds what the video claims.

---

## 0:00 – 0:53 · COLD OPEN
*Footage: working structure fire. No UI, no product name yet.*

> A crew arriving at a structure fire has about **ninety seconds** of usable
> decision time before entry.
>
> Ninety seconds to work out how this building was built. What's been altered
> since. What's stored inside it.
>
> All of it already exists — in a permit portal, an assessor's roll, an
> inspections database, a federal hazard registry.
>
> None of those systems talk to each other. And none of them can be queried in
> ninety seconds.
>
> So the crew goes in without it.

*Beat. Cut to black, then to the standby console.*

---

## 0:53 – 1:55 · THE SLOW LOOP
*Zoom each agent as it is named. Its terminal types out what it just did.*

> **[0:53]** It begins with **records-watcher**, which polls five municipal
> feeds and screens every document through **Model Armor** before a model is
> allowed to see it. **Gemma 4 26B** triages what's worth reading, and **Gemini
> 3.5 Flash** extracts each value bound to the exact sentence that supports it.
> *(Not all of these agents are ours — buildings publishes this one, and fire
> subscribes to a pinned version of it.)*

> **[1:07]** **Geometry-watcher** then measures the same building from above,
> subtracting **USGS 3DEP** ground elevation from the roof planes returned by
> **Google's Solar API** to derive a true height — and a collapse zone at one
> and a half times it.

> **[1:20]** **Hazard-watcher**, powered by **Gemini 3.5 Flash**, queries one
> federal registry against another until it can prove which chemical facility
> is really at an address — and leaves the question open when it can't — so the
> department knows what's stored inside before anyone is standing outside it.

> **[1:33]** **Structure-watch** reads the entire district at a single instant
> and ranks every structure by how badly its records disagree, how stale
> they've become, and how much has changed. That ranking becomes the week's
> inspection list.

> **[1:45]** And **referral-clerk** turns the worst conflict at a structure
> into a letter to the building department — **Gemini** wording it, the
> supporting document ids attached — which a captain reviews and files.

---

## 1:55 – 2:20 · THE DISAGREEMENT
*Records Disagree panel. A card appears live, both storey counts visible.*

> **[1:55]** And no model in this system is permitted to author a fact. Models
> read documents and compose prose, while deterministic code decides what is
> true and what conflicts.

> **[2:05]** Which produces moments like this one. The permit and aerial measurement disagree. TERSAGE doesn't average them and
> doesn't pick a winner — it holds both on the record, flagged as disputed for the admin to review.

---

## 2:20 – 2:38 · THE CALL
*911 alert. The page reorganises into the incident view.*

> **[2:20]** Then a call comes in.

> **[2:23 – 2:38]** ***LET THE RECORDING PLAY. Say nothing. Drop the music.***

---

## 2:38 – 2:50 · THE HANDOFF
*Brief v1 lands instantly. Incident agents take both flanking columns.*

> **[2:38]** And this is the handoff. Everything the slow loop has analyzed and filed over
> months — the permits, the measured height, the chemical filings, the
> disagreements it never resolved — is handed straight to the incident agents.
> **Nothing is being looked up. It's already known.**

---

## 2:50 – 3:26 · THE INCIDENT FLEET  *(all four)*
*Activity stream flowing. Zoom each agent as it is named.*

> **[2:50]** Watch the stream on the right. That's the fleet working, live —
> and every line names the agent that produced it **and which slow loop agent it got the
> information from**. The incident agents aren't guessing; they're reading
> months of the slow loop's work.

> **[3:01]** The **interceptor** reads the caller's own words with **Gemini 3.5
> Flash**, binds what they said to the transcript, and wakes the agents this
> particular fire needs.

> **[3:09]** **Sensor-fusion**, powered by **Gemini 3.5 Flash**, flies the
> building and reads drone imagery and thermal frames multimodally — resolving
> each wall against the footprints and specs **geometry-watcher measured months ago**.

> **[3:17]** **Agency-notifier** matches what the incident needs to who needs to
> hear it — the rooftop solar, the chemical filings — and drafts for each
> case.

> **[3:18]** And the **recorder** writes back what the crew found inside,
> answering questions the paperwork never could. **The fleet is
> self-improving** — every fire leaves the next crew better informed.

---

## 3:26 – 3:38 · CONTROL
*Cut to the audit view, then the Cloud Run dashboard, then the notify actions.*

> **[3:26]** Every one of those actions passed a **default-deny gateway** under
> that agent's own **service-account identity** and left a **Cloud Trace**
> behind it, so no agent can do something its catalog entry doesn't permit.

> **[3:35]** The caller reported chemicals, so we can notify the hazmat team in
> real time. Notifying is autonomous. Closing a gas main is not.

---

## 3:38 – 3:58 · THE PATH, THE BRIEF, THE SEND
*Route draws on the Three.js model. Brief modal raises. Approve. Standby returns.*

> **[3:38]** Now, TERSAGE analyzes the footprint
> geometry-watcher measured, the thermal readings sensor-fusion just took, and
> the disputed storey count nobody could settle, uses an A-star algorithm to compute an optimal path through the building for
the firefighters to enter and attack the crisis.

> **[3:48]** TERSAGE then generates a brief record — hazards first,
> every line naming the agent that found it. We can then approve it...and then dispatch to the crew for their use. After the issue has been resolved, 
we are taken back to the home page where our fleet continues to do the heavy lifting of analyzing municipal enterprise data for fire departments.

> **[3:56]** We used twelve **Cloud Run** services, **Firestore**, **Gemini 3.5 Flash**
> through the **Gen AI SDK**, nine agents in **Google Cloud Agent Registry**.
> Ninety seconds is all a firefighter crew gets — so we spend months making them count.

---

# Production notes

**Do not say "ADK."** This project uses the **Google Gen AI SDK**, which is on
the accepted framework list. `ADKRuntime` is a class name in the codebase and
does not import Google's ADK. Judges are checking the repo.

**The 15 seconds of silence during the 911 recording is the strongest moment in
the video.** Do not fill it.

**Show the Cloud Run dashboard** during CONTROL — that is the "proof it runs on
Google Cloud" the brief asks for. Two seconds is enough.

**Model ids, for reference:** `gemini-3.5-flash` and `gemma-4-26b-a4b-it-maas`,
both reached through Vertex AI. Say "Gemini 3.5 Flash" out loud at least once;
it is a stated entry requirement.

**Timing.** Lands at **3:58**.

**All four incident agents are now in one section**, zoomed together, so a
judge counting agents counts four. `agency-notifier` and `recorder` used to sit
inside CONTROL where they read as asides rather than as members of the fleet.

**The through-line is that the incident agents are *reading the slow loop*.**
It is stated three times on purpose — "referenced from" in the stream,
sensor-fusion resolving walls against the footprint geometry-watcher measured,
and the route solving against both the measured footprint and the live thermal
read. That is the Fortified Enterprise Fleet case and it is the reason this
project is not a chatbot.

**If you run long, cut in this order:**
1. `[1:07]` geometry-watcher's collapse-zone clause — 4s.
2. `[3:18]` the recorder line — 8s, weakest visual.
3. `[1:45]` referral-clerk — 11s, but you lose the external-action beat.

**Do not cut:** the disagreement, the handoff, the activity-stream
"referenced from" beat, the gateway line, or the Agent Registry mention.
