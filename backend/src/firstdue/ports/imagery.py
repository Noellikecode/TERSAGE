"""The building-imagery port -- a photograph of the structure, or a stated refusal.

Beside the massing model the **Geometry Watcher** measured, a commander wants
the thing itself: the doors, the windows, the security bars, the storeys you can
count from the sidewalk. The model is *derived*; the photograph is *seen*.
Neither substitutes for the other, which is why imagery is its own port rather
than another field on the geometry view -- a photograph that disagreed with the
massing would be a second opinion, and the console has to be able to show both.

One verb, for the same reason :mod:`firstdue.ports.vision` has one. ``fetch``
answers "what does this building look like". Anything else -- "which face is
this", "how many storeys" -- is already answered by measured geometry, and a
second answer that could disagree silently is worse than no second answer.

**A refusal is a value, never an exception.** Every implementation returns a
:class:`BuildingImagery`; one that cannot produce a picture returns one with
``available=False`` and a sentence saying why. A console that renders nothing
where a photograph belongs teaches an officer that the building has no
photograph, which is a different claim -- and a false one -- from "we could not
fetch one". That is the same discipline ``UNCONFIGURED_REASONS`` applies to
sources, applied to pixels.

**The provider's key never crosses this boundary.** The port carries bytes, as
a data URL, and an attribution string. It deliberately cannot carry a provider
URL: a browser handed a signed Street View URL is a browser handed the key.
"""

from __future__ import annotations

from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.errors import ValidationError

#: Provider labels the console may receive. ``""`` accompanies a refusal --
#: naming a provider that produced nothing would suggest one was reached.
PROVIDER_STREET_VIEW: Final[str] = "street-view"
PROVIDER_SATELLITE: Final[str] = "satellite"
PROVIDER_SYNTHETIC: Final[str] = "synthetic"

#: Google's Terms require visible attribution wherever Maps imagery is shown.
#: Street View's metadata carries its own copyright line and that one is
#: preferred; this is the floor when it does not.
GOOGLE_ATTRIBUTION: Final[str] = "Imagery © Google"

#: Why there is no photograph. Rendered verbatim by the console, so each one is
#: a sentence an officer can act on rather than a code they have to look up --
#: the distinction that matters is "nobody configured this", "there is genuinely
#: no coverage here", and "the provider is down right now", because those three
#: call for three different reactions and none of them is "the building is
#: gone". Mirrors ``sources.catalog.UNCONFIGURED_REASONS`` in tone and shape.
UNAVAILABLE_REASONS: Final[dict[str, str]] = {
    "unconfigured": (
        "Street View and Static Maps need a Google Maps key this process was "
        "not given; no imagery provider was contacted"
    ),
    "address_unresolved": (
        "the city adapter holds no coordinate for this address, and imagery is "
        "fetched by coordinate rather than geocoded independently"
    ),
    "no_coverage": (
        "Google has no street-level panorama near this address and the "
        "satellite fallback returned nothing usable"
    ),
    "provider_unreachable": (
        "the imagery provider could not be reached; that is an outage, not an "
        "absence of the building"
    ),
    "deadline": (
        "the imagery provider did not answer inside this request's deadline; "
        "the photograph is dropped rather than the console held open"
    ),
    "rate_limited": (
        "this process has spent its imagery rate budget; Street View Static is "
        "metered, and a later request will carry the photograph"
    ),
    "not_an_image": (
        "the imagery provider answered with something that was not an image; "
        "Google's grey 'no imagery available' placeholder is not a photograph "
        "of this building and is never rendered as one"
    ),
    "simulated_absence": (
        "fake mode is simulating a building with no imagery coverage, so the "
        "console's refusal panel is exercised by the demo rather than only in "
        "production"
    ),
}


class ImageryUnavailable(BaseModel):
    """Why no photograph is being shown.

    A type rather than a bare string so that "there is no imagery" has to be
    *constructed* -- an adapter cannot arrive at it by forgetting to set a
    field, and every path that reaches it names one of
    :data:`UNAVAILABLE_REASONS`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The stable code, for a console that wants to style an outage differently
    #: from a policy. Kept out of the wire shape; ``reason`` is what renders.
    code: str = Field(min_length=1, max_length=60)
    reason: str = Field(min_length=1, max_length=400)


def unavailable(code: str) -> ImageryUnavailable:
    """The refusal for a known code.

    An unknown code still produces a sentence rather than a KeyError: a
    mis-typed code should degrade to a vaguer honest answer, never to a 500 on
    the one path whose whole job is reporting that something went wrong.
    """
    return ImageryUnavailable(
        code=code,
        reason=UNAVAILABLE_REASONS.get(
            code, "no photograph of this building could be produced for this request"
        ),
    )


class BuildingImagery(BaseModel):
    """A photograph of one building as the console renders it, or a refusal.

    ``data_url`` carries the bytes inline. That is deliberate and it is the
    security boundary: the server fetches from the provider with the key and
    hands the browser pixels, so ``GOOGLE_MAPS_API_KEY`` never reaches a client,
    a devtools network tab, a referrer header, or a screenshot of one.

    The console **must render ``attribution``** beside the image whenever it is
    non-empty. Google's Maps Platform Terms require visible attribution on
    Street View and Static Maps imagery; a console that drops it puts the
    department out of compliance with the licence the picture arrived under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str = Field(min_length=1, max_length=120)
    available: bool
    #: ``street-view``, ``satellite``, ``synthetic``, or ``""`` on a refusal.
    provider: str = Field(default="", max_length=40)
    content_type: str = Field(default="", max_length=80)
    #: ``data:image/jpeg;base64,...``. Never a provider URL -- see the class
    #: docstring; a signed URL is the key.
    data_url: str = ""
    attribution: str = Field(default="", max_length=400)
    #: What the provider said about recency (Street View reports a capture
    #: month). Empty when it said nothing -- never a guess, because "current"
    #: is exactly the property a commander would over-trust.
    captured_hint: str = Field(default="", max_length=200)
    unavailable_reason: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _absence_says_why(self) -> BuildingImagery:
        if not self.available:
            if not self.unavailable_reason:
                raise ValidationError("imagery that is unavailable has to say why")
            if self.data_url:
                raise ValidationError("imagery that is unavailable cannot carry an image")
        elif not self.data_url:
            raise ValidationError("imagery reported available has to carry an image")
        return self

    @classmethod
    def refused(cls, address_id: str, refusal: ImageryUnavailable) -> BuildingImagery:
        """The honest empty answer, with the reason attached."""
        return cls(address_id=address_id, available=False, unavailable_reason=refusal.reason)


#: Which way the building is being looked at.
#:
#: ``street`` is the eye-level photograph an officer checks a storey count and a
#: barred window against. ``aerial`` is straight down: the roof, its shape, what
#: is standing on it, and how close the exposures are -- the view a commander
#: wants on the way in and cannot get from the kerb.
#:
#: Two views rather than a second port, because it is one provider, one key, one
#: rate limiter and one cache. A separate client would have its own bucket and
#: bill the department twice for the same building.
ImageryView = Literal["street", "aerial"]


@runtime_checkable
class ImageryClient(Protocol):
    """Produces a photograph of one building, or says why it cannot."""

    @property
    def provider_label(self) -> str:
        """What this implementation is, reported so the console can say so.

        ``synthetic`` here is not decoration: fake mode is the default and the
        whole test suite, and a simulated picture that did not admit to being
        one would be the worst failure this system can have.
        """
        ...

    async def fetch(self, *, address_id: str, view: ImageryView = "street") -> BuildingImagery:
        """Return imagery for one address, from one direction.

        Never raises for a missing address, an unconfigured key, a dead
        provider, or a blown deadline: each of those is a ``BuildingImagery``
        with ``available=False``. A raise here would turn a missing photograph
        into a broken console.

        ``view`` defaults to ``street`` so every existing caller is unchanged.
        An implementation that cannot honour a view must say so through
        ``available=False`` rather than quietly returning the other one -- a
        commander told they are looking down at a roof, and shown a kerb, is
        worse served than one told there is no aerial.
        """
        ...
