"""The two grounding decisions that must not differ between implementations.

:class:`~firstdue.ports.grounding.GroundingService` has two implementations and
will acquire more. Two of the things they do are not adapter concerns at all,
and letting each one write its own version is how they drift:

**Which candidate becomes a binding.** :func:`arbitrate` and :func:`bind` are
the only places in the system where an ``address_id`` is chosen. The membership
check, the confidence floor, the ambiguity margin, and the refusal to bind
without evidence live here once. An adapter physically cannot bind an id the
caller did not offer, because the only function that constructs a resolved
:class:`~firstdue.ports.grounding.Resolution` takes the candidate tuple and
checks against it.

**Whether retrieved web text is allowed out.** :func:`screen_reports` is the
gate. Search results are the least trustworthy input this system takes -- any
page can contain "disregard previous instructions" -- and the screen runs on
every one before it is returned or stored.

The screening rule here is deliberately *stricter* than the one
:mod:`firstdue.security.armor` applies to ingested municipal documents. There,
a blocked document is sanitised and kept, because a filed inspection narrative
is evidence the department is obliged to hold and the rest of the sentence
still matters. Here a blocked report is dropped whole. A web page is one of
thousands, nobody filed it, and it is not evidence of anything the department
must retain -- so the cost of discarding it is zero and the cost of keeping a
partially-sanitised copy of a page that just tried to instruct us is not.

``method`` is versioned because it lands on every fact derived from a binding.
When the floor or the margin below changes, bindings made under the old rule
stay legible: they say which rule made them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from firstdue.observability.logging import get_logger
from firstdue.ports.grounding import GroundedReport, Resolution

if TYPE_CHECKING:  # pragma: no cover - a type, not a dependency
    # Annotation only. ``security.armor`` reaches ``extraction.screening``,
    # whose package init reaches back into ``security.armor``; a runtime import
    # here would make this module the first one in that loop, and the loop then
    # fails. The same reason :mod:`firstdue.incident.intake` does it this way.
    from firstdue.security.armor import DocumentScreen

logger = get_logger(__name__)

#: The arbitration rule's version, recorded on every resolution. Bumped when
#: the floor, the margin, or the membership rule below changes -- a fact that
#: names the rule that bound it can be re-judged; one that does not cannot.
GROUNDING_METHOD: Final[str] = "grounding/1.0.0"

#: Below this a candidate is not bound, it is declined. Set where it is because
#: the asymmetry is brutal: a declined reference costs one un-enriched profile,
#: and a wrong one writes a fire onto the permanent record of a building that
#: did not burn. There is no confidence at which guessing beats abstaining.
MIN_CONFIDENCE: Final[float] = 0.62

#: How far the best candidate must lead the runner-up. Two plausible buildings
#: is the *common* failure on a parcel with a rear cottage or a subdivided
#: storefront, and it is exactly the case where a resolver sounds confident.
#: Closer than this and neither is bound.
AMBIGUITY_MARGIN: Final[float] = 0.08

#: Most candidates any resolver considers for one reference. A caller handing
#: over a whole district is asking a question no evidence can settle, and in
#: live mode it is also a prompt nobody budgeted for.
MAX_CANDIDATES: Final[int] = 50

#: Most reports returned for one area, and the most text kept from each. The
#: cap is ours, not the model's or the web's.
MAX_REPORTS: Final[int] = 8
MAX_SNIPPET_CHARS: Final[int] = 1_200
MAX_HEADLINE_CHARS: Final[int] = 400
MAX_HINT_CHARS: Final[int] = 300

# Decline reasons. Fixed strings, never interpolated with the reference or with
# anything a page said: a reason travels into logs, spans, and audit records,
# and none of those is a place to put a citizen's address or a web page's prose.
DECLINED_NO_CANDIDATES: Final[str] = "the caller offered no candidate ids to choose among"
DECLINED_EMPTY_REFERENCE: Final[str] = "the reference carries nothing to resolve"
DECLINED_NOT_A_CANDIDATE: Final[str] = "the resolver named an id the caller did not offer"
DECLINED_LOW_CONFIDENCE: Final[str] = "no candidate cleared the confidence floor"
DECLINED_AMBIGUOUS: Final[str] = "two candidates were too close together to separate"
DECLINED_NO_EVIDENCE: Final[str] = "nothing was retrieved that a reviewer could check"
DECLINED_DEADLINE: Final[str] = "the deadline elapsed before a candidate was chosen"
DECLINED_UNAVAILABLE: Final[str] = "the grounding backend is unavailable"
DECLINED_UNPARSEABLE: Final[str] = "the resolver did not answer in the required form"
DECLINED_BY_RESOLVER: Final[str] = "the resolver found no candidate the evidence supports"


def method_for(resolver_ref: str) -> str:
    """``grounding/1.0.0/vertex/gemini-3.5-flash``, and so on.

    The rule and the resolver, in one string, because an audit asking why a
    building was bound needs both: the version says which floor applied, and
    the resolver says whether a model or arithmetic applied it.
    """
    return f"{GROUNDING_METHOD}/{resolver_ref}"


def declined(
    reason: str,
    *,
    resolver_ref: str,
    confidence: float = 0.0,
    evidence: tuple[str, ...] = (),
) -> Resolution:
    """A decline, which is a value and never an exception.

    ``confidence`` carries the best score the resolver actually saw. An
    operator asking why a reference did not bind is asking whether it was close,
    and a decline that always reported zero could not answer.
    """
    return Resolution(
        resolved=False,
        confidence=max(0.0, min(1.0, confidence)),
        evidence=evidence,
        method=method_for(resolver_ref),
        declined_reason=reason[:300],
    )


def bind(
    *,
    address_id: str,
    confidence: float,
    evidence: tuple[str, ...],
    candidates: Sequence[str],
    resolver_ref: str,
) -> Resolution:
    """Turn one chosen id into a binding, or explain why it is not one.

    The membership check is the reason this function exists and the reason no
    adapter constructs a resolved :class:`Resolution` itself. A resolver that
    hallucinated a plausible-looking id -- a model asked for one of fifty
    strings will occasionally return a fifty-first -- would otherwise bind a
    fire to a parcel that does not exist in this district, and the id looks
    right until somebody drives to it.
    """
    if address_id not in candidates:
        # Worth a warning, not a debug line: a resolver answering outside the
        # closed set is either drifting off contract or being steered by a page.
        logger.warning(
            "grounding_answer_outside_candidates",
            extra={"resolver_ref": resolver_ref, "candidate_count": len(candidates)},
        )
        return declined(DECLINED_NOT_A_CANDIDATE, resolver_ref=resolver_ref, evidence=evidence)
    if not evidence:
        return declined(
            DECLINED_NO_EVIDENCE,
            resolver_ref=resolver_ref,
            confidence=confidence,
        )
    if confidence < MIN_CONFIDENCE:
        return declined(
            DECLINED_LOW_CONFIDENCE,
            resolver_ref=resolver_ref,
            confidence=confidence,
            evidence=evidence,
        )
    return Resolution(
        resolved=True,
        address_id=address_id,
        confidence=max(0.0, min(1.0, confidence)),
        evidence=evidence,
        method=method_for(resolver_ref),
    )


def arbitrate(
    scores: Mapping[str, float],
    *,
    candidates: Sequence[str],
    evidence: tuple[str, ...],
    resolver_ref: str,
) -> Resolution:
    """Choose between scored candidates, or decline because they are too close.

    Ordering breaks ties by id rather than by whatever order a dict happened to
    have, so two runs over the same evidence cannot bind different buildings.
    That determinism is not a nicety here: a replay that resolved differently
    from the original run would make the audit record unreadable.
    """
    ranked = sorted(
        ((score, address_id) for address_id, score in scores.items() if address_id in candidates),
        key=lambda pair: (-pair[0], pair[1]),
    )
    if not ranked:
        return declined(DECLINED_NO_CANDIDATES, resolver_ref=resolver_ref, evidence=evidence)

    best_score, best_id = ranked[0]
    if len(ranked) > 1 and best_score - ranked[1][0] < AMBIGUITY_MARGIN:
        # Deliberately checked before the floor: "two plausible buildings" is a
        # more useful thing to tell an operator than "nothing scored well", and
        # it is the case a human can actually settle by going to look.
        return declined(
            DECLINED_AMBIGUOUS,
            resolver_ref=resolver_ref,
            confidence=best_score,
            evidence=evidence,
        )
    return bind(
        address_id=best_id,
        confidence=best_score,
        evidence=evidence,
        candidates=candidates,
        resolver_ref=resolver_ref,
    )


@dataclass(frozen=True, slots=True)
class RetrievedReport:
    """One report as a retrieval backend produced it. **Unscreened.**

    Deliberately not a :class:`~firstdue.ports.grounding.GroundedReport`: the
    two are different states, and the type system is what stops the unscreened
    one leaving the module. Nothing constructs a ``GroundedReport`` except
    :func:`screen_reports`.
    """

    headline: str
    snippet: str
    source_uri: str
    published_at: datetime | None = None
    address_hint: str | None = None


@dataclass(frozen=True, slots=True)
class ScreenedReports:
    """What screening one batch produced, in counts a span can carry."""

    reports: tuple[GroundedReport, ...] = ()
    #: Reports the screen objected to and that were therefore dropped whole.
    blocked: int = 0
    #: True when the screen itself could not run, so nothing was returned. The
    #: caller has no reports *and* no negative finding.
    degraded: bool = False


def report_id_for(source_uri: str, headline: str) -> str:
    """A stable id for one retrieval.

    Derived rather than generated so that re-running a retrieval that found the
    same page produces the same id, and a store keyed on it cannot accumulate
    duplicates of one article across a week of polls.
    """
    digest = hashlib.sha256(f"{source_uri}|{headline}".encode()).hexdigest()[:16]
    return f"report_{digest}"


async def screen_reports(
    *,
    screen: DocumentScreen,
    retrieved: Sequence[RetrievedReport],
    area: str,
    retrieved_at: datetime,
) -> ScreenedReports:
    """The gate every retrieved report passes before it can be returned.

    Three outcomes, and the middle one is the whole point:

    * **clean** -- the report is returned;
    * **blocked** -- the report is dropped whole and counted, because a page
      that tried to instruct us has forfeited its place in a fire record;
    * **screen unavailable** -- *nothing* is returned. Not the batch minus the
      unscreenable ones, and certainly not the raw text: a screen outage that
      let web prose through would turn the one control standing between a
      hostile page and a model into a control that fails open.

    The returned fields are the originals rather than the verdict's
    ``safe_text``, and that is safe by the screen's own contract: a verdict is
    ``blocked`` whenever anything was removed, so a verdict that is not blocked
    is one whose ``safe_text`` is the input. Taking the originals keeps the
    stored report byte-identical to what was retrieved, which is what makes the
    snapshot replayable.
    """
    kept: list[GroundedReport] = []
    blocked = 0

    for report in retrieved[:MAX_REPORTS]:
        headline = report.headline.strip()[:MAX_HEADLINE_CHARS]
        snippet = report.snippet.strip()[:MAX_SNIPPET_CHARS]
        hint = (report.address_hint or "").strip()[:MAX_HINT_CHARS] or None
        if not headline or not report.source_uri:
            continue

        # One document, so one screen call per report rather than three. The
        # headline and the address hint are web text exactly as the snippet is,
        # and screening only the snippet would leave the two fields a rendered
        # brief is most likely to show unguarded.
        document = "\n\n".join(part for part in (headline, hint or "", snippet) if part)
        verdict = await screen.inspect(document)

        if verdict.unavailable_reason is not None:
            logger.warning(
                "grounding_screen_unavailable",
                extra={
                    "screen": verdict.screen,
                    "reason": verdict.unavailable_reason,
                    "retrieved": len(retrieved),
                },
            )
            return ScreenedReports(degraded=True)

        if verdict.blocked:
            blocked += 1
            logger.warning(
                "grounding_report_blocked",
                extra={"screen": verdict.screen, "findings": ",".join(verdict.findings)},
            )
            continue

        kept.append(
            GroundedReport(
                report_id=report_id_for(report.source_uri, headline),
                headline=headline,
                published_at=report.published_at,
                retrieved_at=retrieved_at,
                source_uri=report.source_uri[:2000],
                snippet=snippet,
                area=area,
                address_hint=hint,
            )
        )

    return ScreenedReports(reports=tuple(kept), blocked=blocked)
