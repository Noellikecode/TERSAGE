"""Jurisdiction filtering and mutual-aid agreements.

When a San Francisco company responds into Daly City under mutual aid, the
question is not "can they see the building" -- they are standing in front of it.
The question is which *records* the responding agency is entitled to under the
agreement between the two jurisdictions.

Two rules, and the second is the one that matters:

1. An agreement names the classifications it covers. Anything outside it is
   withheld.
2. **Withheld is rendered, never omitted.** The officer sees
   ``WITHHELD - Daly City mutual aid does not cover Tier II filings``, learns
   that such a filing exists, and can ask for it by radio. Silently dropping the
   row would tell them the building has no hazardous materials.

That distinction is the whole module. A system that quietly returns less under
mutual aid is more dangerous than one that returns nothing, because the officer
cannot tell the difference between "nothing there" and "not shown to you".
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import Classification


class MutualAidAgreement(BaseModel):
    """What one jurisdiction shares with another, and under what authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agreement_id: str = Field(min_length=1, max_length=120)
    home_jurisdiction_id: str = Field(min_length=1, max_length=120)
    partner_jurisdiction_id: str = Field(min_length=1, max_length=120)
    #: Classifications the agreement covers. Everything else is withheld.
    shared_classifications: frozenset[Classification]
    #: Cited verbatim in the withheld line an officer reads.
    authority: str = Field(min_length=1, max_length=200)

    def covers(self, classification: Classification) -> bool:
        return classification in self.shared_classifications


#: The agreements this build knows. Synthetic, like every agreement in the demo.
MUTUAL_AID_AGREEMENTS: Final[tuple[MutualAidAgreement, ...]] = (
    MutualAidAgreement(
        agreement_id="aid-sf-dalycity-2024",
        home_jurisdiction_id="sf-city-county",
        partner_jurisdiction_id="daly-city",
        # Public records travel; confidential filings and person-level data
        # do not, because the agreement does not extend the statute that
        # protects them.
        shared_classifications=frozenset({Classification.PUBLIC}),
        authority="SF / Daly City automatic aid agreement, 2024, section 4",
    ),
    MutualAidAgreement(
        agreement_id="aid-sf-county-oem-2023",
        home_jurisdiction_id="sf-city-county",
        partner_jurisdiction_id="county-oem",
        shared_classifications=frozenset(
            {Classification.PUBLIC, Classification.TIER_II_CONFIDENTIAL}
        ),
        authority="County emergency management data-sharing memorandum, 2023",
    ),
)


def aid_agreement_for(agreement_id: str | None) -> MutualAidAgreement | None:
    """Look up an agreement by the id a grant carries."""
    if agreement_id is None:
        return None
    for agreement in MUTUAL_AID_AGREEMENTS:
        if agreement.agreement_id == agreement_id:
            return agreement
    return None


class WithheldSource(BaseModel):
    """A row an officer can see the shape of but not the contents of.

    Rendered on the brief. Carries the rule and the authority, so the answer to
    "why can't I see it" is on the screen rather than in a support ticket.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1, max_length=120)
    classification: Classification
    rule_id: str = Field(min_length=1, max_length=120)
    authority: str = Field(min_length=1, max_length=200)
    #: What the officer reads. Never empty, never silently dropped.
    reason: str = Field(min_length=1, max_length=300)

    @property
    def render(self) -> str:
        return f"WITHHELD - {self.reason}"


def withhold(
    *, source_id: str, classification: Classification, agreement: MutualAidAgreement | None
) -> WithheldSource:
    """Build the withheld row for a source an aid agreement does not cover."""
    if agreement is None:
        return WithheldSource(
            source_id=source_id,
            classification=classification,
            rule_id="jurisdiction.no-agreement",
            authority="no mutual-aid agreement on file",
            reason=(
                f"{source_id} is outside the responding agency's jurisdiction and no "
                "mutual-aid agreement covers it"
            ),
        )
    return WithheldSource(
        source_id=source_id,
        classification=classification,
        rule_id="jurisdiction.outside-agreement",
        authority=agreement.authority,
        reason=(
            f"{agreement.agreement_id} does not cover {classification} records; "
            f"{source_id} exists but is not shared under it"
        ),
    )
