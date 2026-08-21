"""Deterministic imagery observations, derived from the frame's own bytes.

Not a stub returning a constant. The observations are derived from a digest of
the image, so the same frame always yields the same reading and two different
frames yield different ones -- which is what makes a seeded demo reproducible
and a replay byte-identical, the same property ADR 0003 asks of every fake.

What it deliberately does **not** do is look at the picture. A fake that
guessed from pixels would be a second, worse vision model nobody reviewed; this
one is honest about being arithmetic, and the live adapter is the only thing in
the system that claims to have seen anything.
"""

from __future__ import annotations

import hashlib
from typing import Final

from firstdue.domain.vision import (
    ImageRegion,
    ObservationKind,
    VisionObservation,
    VisionResult,
)

#: Regions across the face, low to high. A real frame is segmented by the
#: model; the fake uses a fixed vertical banding so a "cockloft hotter than the
#: first floor" story is expressible without pretending to segment anything.
_BANDS: Final[tuple[tuple[float, float], ...]] = (
    (0.66, 0.34),  # ground level
    (0.33, 0.33),  # mid
    (0.00, 0.33),  # eaves and cockloft
)

#: Ambient floor for a derived reading, Celsius. Below this a "measurement"
#: would be colder than a winter night and read as sensor failure.
_BASE_C: Final[float] = 18.0


class FakeVisionClient:
    """Derives observations from the image digest. No credentials, no network."""

    model_ref: Final[str] = "fake/vision-1"

    def __init__(self, *, hot: bool = True) -> None:
        #: When false, every band derives near ambient -- the "nothing showing"
        #: frame, which is the one a demo needs to prove UNSCANNED and "no
        #: hazard identified" are different from "cool".
        self._hot = hot

    async def observe(
        self,
        *,
        image: bytes,
        mime_type: str,
        deadline_ms: int,
    ) -> VisionResult:
        if not image:
            return VisionResult(
                accepted=False,
                rejection_reason="empty frame",
                model_ref=self.model_ref,
            )

        digest = hashlib.sha256(image).digest()
        observations: list[VisionObservation] = []

        for index, (y, height) in enumerate(_BANDS):
            # Higher bands derive hotter, so the deterministic void rule in the
            # fusion agent has something to find. The spread comes from the
            # digest, so it is stable per frame and differs between frames.
            spread = digest[index] / 255.0
            if self._hot:
                celsius = _BASE_C + 40.0 * index + 120.0 * spread * (index / 2.0)
            else:
                celsius = _BASE_C + 4.0 * spread
            observations.append(
                VisionObservation(
                    kind=ObservationKind.THERMAL_REGION,
                    region=ImageRegion(x=0.05, y=y, width=0.9, height=height),
                    raw_value=f"{celsius:.1f}C",
                    model_confidence=0.70,
                )
            )

        # One opening band per storey the digest implies, two or three, so the
        # storey count read off the imagery can agree or disagree with the
        # permit -- which is the whole point of putting it on the profile.
        storeys = 2 + (digest[8] % 2)
        for storey in range(storeys):
            observations.append(
                VisionObservation(
                    kind=ObservationKind.STOREY_BAND,
                    region=ImageRegion(
                        x=0.1,
                        y=max(0.0, 0.8 - 0.3 * storey),
                        width=0.8,
                        height=0.18,
                    ),
                    raw_value=f"row of windows, storey {storey + 1}",
                    model_confidence=0.62,
                )
            )

        return VisionResult(
            observations=tuple(observations),
            unknowns=("roof condition not visible from a ground-level frame",),
            model_ref=self.model_ref,
        )
