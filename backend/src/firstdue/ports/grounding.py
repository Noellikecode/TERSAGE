"""The grounding port -- two verbs, and one line drawn in the return types.

Three slow-loop watchers arrive at the same question from three directions. The
**Hazard Watcher** has ``ACME PLATING INC`` off an EPA registry and needs to
know which parcel that is. The **Records Watcher** has a permit citing "the rear
structure". The **Occupancy Watcher** has ``Little Sprouts Daycare``. In every
case the question is *which building is this text about*, and in every case the
municipal record itself does not say.

Search can answer that question. What it must never answer is **what is true
about the building**.

That line is drawn here in the types rather than in a convention somebody has
to remember:

* :meth:`GroundingService.resolve_reference` returns an **id**, chosen from ids
  the caller already holds. :class:`Resolution` has no field that could carry a
  construction type, a storey count, or a hazard, so a search result cannot
  become an assertion about a structure no matter how the prompt is written.
* :meth:`GroundingService.local_fire_reports` returns **retrievals**, not facts.
  A :class:`GroundedReport` carries the URI it came from and the instant it was
  fetched. A watcher that turns one into a fact stamps it with that web
  provenance; nothing here lets it be attributed to a municipal record.

**Declining is the outcome that matters.** A wrong binding writes a fire onto
the permanent record of a building that did not burn, and the officer reading
that record two years later has no way to tell. No binding costs a watcher one
enrichment. So ``resolved=False`` is not the failure path -- it is the correct
answer under ambiguity, under a confidence floor, and under a deadline, and
:class:`Resolution` refuses to be constructed as a binding that cannot be
checked.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firstdue.errors import ValidationError


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    """Reject naive datetimes, for the reason every timestamp here is aware.

    A retrieval whose instant cannot be placed on a timeline is a retrieval
    nobody can replay against, and "when was this fetched" is the first
    question asked of a web-sourced fact.
    """
    if value is not None and (value.tzinfo is None or value.tzinfo.utcoffset(value) is None):
        raise ValidationError(f"{field_name} must be timezone-aware", details={"field": field_name})
    return value


class Resolution(BaseModel):
    """What a resolver decided about one fuzzy reference.

    The invariants below are enforced rather than documented because every one
    of them is a way a bad binding could reach a building's record:

    * a binding names an ``address_id``; a decline never does, so a caller that
      forgets to check ``resolved`` cannot read an id out of a refusal;
    * a binding carries ``evidence``, because a binding a reviewer cannot check
      is a guess wearing a confidence score;
    * a decline carries a ``declined_reason``, because "we did not bind this"
      and "we never looked" are different states and an operator has to be able
      to tell them apart.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolved: bool
    #: Always one of the ``candidates`` the caller supplied. The resolver
    #: chooses; it never mints.
    address_id: str | None = Field(default=None, max_length=120)
    #: The resolver's own confidence, and on a decline the best score it saw --
    #: which is what tells an operator whether it was close or nowhere near.
    confidence: float = Field(ge=0.0, le=1.0)
    #: Citation URIs, record refs, or both. Kept on declines too: the pages
    #: that failed to settle the question are what an operator wants first.
    evidence: tuple[str, ...] = ()
    #: The resolution rule and the resolver that ran it, recorded on any fact
    #: derived from this binding so a replay can tell which one bound it.
    method: str = Field(min_length=1, max_length=200)
    declined_reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _check_outcome(self) -> Self:
        if self.resolved:
            if not self.address_id:
                raise ValidationError("a resolved reference must name an address_id")
            if not self.evidence:
                raise ValidationError(
                    "a resolved reference must carry the evidence it was resolved from",
                    details={"address_id": self.address_id},
                )
            if self.declined_reason is not None:
                raise ValidationError("a resolved reference cannot also carry a decline reason")
            return self
        if self.address_id is not None:
            raise ValidationError("a declined reference must not name an address_id")
        if not self.declined_reason:
            raise ValidationError("a declined reference must say why")
        return self


class GroundedReport(BaseModel):
    """One retrieved report about fire activity in an area.

    **A snapshot, not a window onto the live web.** Everything needed to
    reconstruct what was read is on the record: the URI it came from, the
    instant it was fetched, and the screened text as it stood then. A replay two
    years later -- a NIOSH review, a records request, a line-of-duty-death
    investigation -- reads *this stored report*, not the page. The page will
    have been edited, paywalled, or deleted, and a system that re-fetched at
    read time would quietly show the reviewer something the crew never saw.

    ``snippet``, ``headline`` and ``address_hint`` are screened text. Web pages
    are the least trustworthy input this system takes, and a report is dropped
    whole rather than sanitised when the screen objects -- see
    :func:`firstdue.services.grounding.screen_reports`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Derived from the source URI and headline, so the same retrieval always
    #: carries the same id and a re-run cannot duplicate a report.
    report_id: str = Field(min_length=1, max_length=120)
    headline: str = Field(min_length=1, max_length=400)
    #: ``None`` means the retrieval carried no publication date -- never that
    #: the report is undated at the source.
    published_at: datetime | None = None
    retrieved_at: datetime
    source_uri: str = Field(min_length=1, max_length=2000)
    #: Screened text only. Never the raw page.
    snippet: str = Field(max_length=4000)
    #: The area asked about, carried so a stored report says what question it
    #: was an answer to.
    area: str = Field(min_length=1, max_length=200)
    #: What the report says about *where*, verbatim and unresolved. It is a
    #: hint precisely because it is not an ``address_id``: binding it to one is
    #: :meth:`GroundingService.resolve_reference`'s job, under its own floor.
    address_hint: str | None = Field(default=None, max_length=300)

    @field_validator("retrieved_at", "published_at")
    @classmethod
    def _timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "timestamp")


@runtime_checkable
class GroundingService(Protocol):
    """Resolves a reference to an id, or retrieves reports. Never asserts.

    Two implementations, held to one contract: Gemini with native Google Search
    grounding on Vertex, and a deterministic fake that declines for real -- so
    ``make demo`` rehearses the refusals rather than skipping them.
    """

    async def resolve_reference(
        self,
        reference: str,
        *,
        district_id: str,
        candidates: tuple[str, ...],
        deadline_ms: int,
    ) -> Resolution:
        """Decide which of ``candidates`` -- if any -- ``reference`` points at.

        Never raises. Every failure is a decline: an unreachable backend, an
        elapsed deadline, a malformed answer, two candidates too close to
        separate. A raise here would turn one unmatched permit into a failed
        district poll, and worse, would tempt a caller into a bare ``except``
        that swallows the distinction between "declined" and "crashed".

        Args:
            reference: the fuzzy text, exactly as the source wrote it.
            district_id: scopes the question; never widens the candidate set.
            candidates: the ids the caller already knows about. The resolver
                chooses among these or declines -- it cannot invent an id, and
                an empty tuple is an immediate decline rather than a search.
            deadline_ms: the hard budget. Elapsing it is a decline, not an
                exception and never a lower-confidence guess.
        """
        ...

    async def local_fire_reports(
        self,
        *,
        district_id: str,
        area: str,
        deadline_ms: int,
    ) -> tuple[GroundedReport, ...]:
        """Retrieve recent reports of fire activity in ``area``.

        Never raises, and returns only reports that a screen actually cleared.

        An empty tuple means **no report was retrieved and screened** -- it is
        not an assertion that the area has seen no fires. Callers must not read
        it as one. That is tolerable here, unlike for a hazard registry, only
        because nothing downstream turns absence into a fact: a report that is
        not returned is a fact that is never minted, not a negative claim
        recorded against a building.
        """
        ...
