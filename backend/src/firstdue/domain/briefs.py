"""Brief emissions.

Three invariants live here and are enforced by the type, not by discipline:

1. **The instant stage contains no model call.** ``model_invoked`` must be
   ``False`` when ``stage is INSTANT``. Speed and safety point the same way: the
   officer has the structural picture before Gemini emits a token, and if Vertex
   AI is down stage one still lands.
2. **Every emission is persisted before it is transmitted.** ``persisted_at`` is
   set by the log writer; :meth:`BriefEmission.require_persisted` is the only
   sanctioned gate in front of a transport, and it raises otherwise.
3. **Reported is not observed.** A line carrying ``reported_note`` -- something
   a 911 caller or a dispatcher said -- can never be ``CONFIRMED``, can never
   cite a fact id, and can never claim a source type. A caller's "three floors"
   and a surveyed storey count must not render alike.

Unknowns are never omitted. An emission always carries its ``unknowns``,
``unavailable`` and ``withheld`` lists, so "we could not check" is on screen
next to what we did check.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.enums import AssertionStatus, BriefStage, SourceType
from firstdue.domain.keys import CanonicalKey
from firstdue.errors import BriefNotPersistedError, ValidationError


class BriefSectionKey(StrEnum):
    """COAL WAS WEALTH, the size-up order a fire officer already reads in."""

    CONSTRUCTION = "CONSTRUCTION"
    OCCUPANCY = "OCCUPANCY"
    APPARATUS = "APPARATUS"
    LIFE_HAZARD = "LIFE_HAZARD"
    WATER_SUPPLY = "WATER_SUPPLY"
    AUXILIARY_APPLIANCES = "AUXILIARY_APPLIANCES"
    STREET_CONDITIONS = "STREET_CONDITIONS"
    WEATHER = "WEATHER"
    EXPOSURES = "EXPOSURES"
    AREA = "AREA"
    LOCATION_EXTENT = "LOCATION_EXTENT"
    TIME = "TIME"
    HEIGHT = "HEIGHT"
    HAZARDS = "HAZARDS"
    CONFLICTS = "CONFLICTS"


class BriefItem(BaseModel):
    """One line of the brief, carrying how it is known as well as what it says."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=120)
    #: Already rendered by the value type -- "UNKNOWN", "WITHHELD - ...", "3".
    value_render: str = Field(min_length=1, max_length=500)
    status: AssertionStatus
    canonical_key: CanonicalKey | None = None
    fact_id: str | None = Field(default=None, max_length=120)
    provenance: SourceType | None = None
    #: Present when the gateway derived this rather than releasing a record.
    derivation_note: str | None = Field(default=None, max_length=300)
    #: Present when a source was withheld by jurisdiction or statute.
    withheld_note: str | None = Field(default=None, max_length=300)
    #: Present when a person *reported* this rather than a record stating it --
    #: a 911 caller, a dispatcher's narrative. Names who said it and what is on
    #: file instead, and its presence is what :meth:`_check_reported` keys on.
    reported_note: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _check_reported(self) -> Self:
        """A reported line can never look like a confirmed one.

        Three clauses, one rule. Somebody under stress on a phone said this:

        * it is not ``CONFIRMED``, because nothing confirmed it;
        * it carries no ``fact_id``, because no fact was written -- the intake
          does not author structural facts (section 6), so a fact id here would
          be a reference to something that does not exist;
        * it carries no ``provenance`` source type, because the source types are
          the merge tiers, and a caller report that had one would sort against
          filed records instead of standing beside them.

        Without this rule the failure is silent and specific: a caller's "three
        floors" renders identically to a surveyed storey count, and the officer
        reading the brief has no way to tell which one they are looking at.
        """
        if self.reported_note is None:
            return self
        if self.status is AssertionStatus.CONFIRMED:
            raise ValidationError(
                "a reported brief item may not be rendered as confirmed",
                details={"label": self.label},
            )
        if self.fact_id is not None or self.provenance is not None:
            raise ValidationError(
                "a reported brief item carries no fact id and no source type",
                details={"label": self.label},
            )
        return self


class BriefSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: BriefSectionKey
    items: tuple[BriefItem, ...] = ()


class BriefEmission(BaseModel):
    """One version of the brief, as the commander saw it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    emission_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    #: Monotonic within an incident. Version 1 is always the instant stage.
    version: int = Field(ge=1)
    stage: BriefStage

    sections: tuple[BriefSection, ...] = ()
    #: Attributes with no record. Always present, never elided.
    unknowns: tuple[CanonicalKey, ...] = ()
    #: Sources that could not be reached. Distinct from unknowns.
    unavailable: tuple[str, ...] = ()
    #: Sources withheld by statute or aid agreement.
    withheld: tuple[str, ...] = ()
    #: Open conflicts, by id -- the deterministic engine found these.
    conflict_ids: tuple[str, ...] = ()

    #: Model-composed prose. Absent on the instant stage, by construction.
    narrative: str | None = Field(default=None, max_length=20_000)
    #: False when the model was unavailable; the brief says so and still lands.
    narrative_available: bool = False
    model_invoked: bool = False

    profile_snapshot_id: str = Field(min_length=1, max_length=120)
    agent_versions: dict[str, str] = Field(default_factory=dict)
    policy_decision_ids: tuple[str, ...] = ()

    produced_at: datetime
    #: Set only by the incident log writer. Nothing transmits without it.
    persisted_at: datetime | None = None
    content_hash: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _check_stage_rules(self) -> Self:
        if self.stage is BriefStage.INSTANT:
            if self.model_invoked:
                raise ValidationError(
                    "the instant brief stage must not invoke a model",
                    details={"emission_id": self.emission_id},
                )
            if self.narrative is not None:
                raise ValidationError(
                    "the instant brief stage carries no model-composed narrative",
                    details={"emission_id": self.emission_id},
                )
            if self.version != 1:
                raise ValidationError(
                    "the instant brief stage is always version 1",
                    details={"version": self.version},
                )
        if self.narrative is not None and not self.narrative_available:
            raise ValidationError(
                "narrative_available must be true when a narrative is present",
                details={"emission_id": self.emission_id},
            )
        if self.narrative is None and self.narrative_available:
            raise ValidationError(
                "narrative_available must be false when no narrative is present",
                details={"emission_id": self.emission_id},
            )
        return self

    # ------------------------------------------------------------- behaviour

    def compute_content_hash(self) -> str:
        """Deterministic hash of everything the commander saw.

        Excludes ``persisted_at`` and ``content_hash`` so the hash is stable
        across the persist step and identical on replay.
        """
        payload = self.model_dump(
            mode="json", exclude={"persisted_at", "content_hash"}, exclude_none=False
        )
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def sealed(self) -> BriefEmission:
        """Return a copy with its content hash filled in."""
        return self.model_copy(update={"content_hash": self.compute_content_hash()})

    def mark_persisted(self, *, at: datetime) -> BriefEmission:
        """Record that the incident log has durably stored this emission."""
        sealed = self if self.content_hash else self.sealed()
        return sealed.model_copy(update={"persisted_at": at})

    def require_persisted(self) -> Self:
        """Gate in front of any transport.

        Raises:
            BriefNotPersistedError: if the emission has not been written to the
                incident log yet. Nothing reaches the commander that is not in
                the record.
        """
        if self.persisted_at is None:
            raise BriefNotPersistedError(
                "brief emission must be persisted before transmission",
                details={"emission_id": self.emission_id, "incident_id": self.incident_id},
            )
        return self

    @property
    def has_gaps(self) -> bool:
        return bool(self.unknowns or self.unavailable or self.withheld)
