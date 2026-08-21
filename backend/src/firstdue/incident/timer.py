"""The lightweight-truss time window.

A lightweight parallel-chord truss floor is a published material property with a
published behaviour under fire: unprotected steel gusset plates lose capacity
quickly, and testing bodies have measured that. UL and NIST have run the tests;
the numbers below cite them.

**This is not a collapse prediction, and the distinction is the entire module.**
A prediction would say *this floor will fail at 13 minutes*. What this states is
*the published test window for this assembly is 6 to 13 minutes of direct fire
exposure, and the elapsed time since dispatch is 9 minutes*. The first is a claim
about this fire, which nobody can make. The second is two facts side by side, and
the commander does the arithmetic they were already doing.

Every rendering carries the disclaimer, in the model rather than in the template,
so a renderer cannot show the number without it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.keys import Keys

#: The published range for unprotected lightweight parallel-chord truss under
#: direct fire exposure. Cited, not modelled.
TRUSS_WINDOW_MIN: Final[timedelta] = timedelta(minutes=6)
TRUSS_WINDOW_MAX: Final[timedelta] = timedelta(minutes=13)
TRUSS_CITATION: Final[str] = (
    "UL/NIST fire-resistance testing of unprotected lightweight parallel-chord " "truss assemblies"
)

#: Printed with every window, in the model rather than the template.
DISCLAIMER: Final[str] = (
    "This is a published material property and elapsed time since dispatch. "
    "It is not a prediction that this structure will fail, and it does not "
    "account for this fire's exposure, load, or suppression."
)


class MaterialTimeWindow(BaseModel):
    """A published test window, and the clock, presented side by side."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: What assembly this window is about.
    assembly: str = Field(min_length=1, max_length=120)
    canonical_key: str = Field(min_length=1, max_length=120)
    #: The fact that established the assembly is present. Never inferred.
    fact_id: str | None = Field(default=None, max_length=120)

    window_min_seconds: int = Field(ge=0)
    window_max_seconds: int = Field(ge=0)
    citation: str = Field(min_length=1, max_length=200)

    #: Seconds since dispatch. A measurement, not an estimate.
    elapsed_seconds: float = Field(ge=0.0)
    disclaimer: str = DISCLAIMER

    @property
    def elapsed_exceeds_window_start(self) -> bool:
        """Whether the clock has passed the low end of the published range."""
        return self.elapsed_seconds >= self.window_min_seconds

    @property
    def render(self) -> str:
        """One line, with the disclaimer attached.

        There is no rendering that omits it: the property builds the string, so
        a template cannot show the numbers alone.
        """
        minutes = self.elapsed_seconds / 60.0
        return (
            f"{self.assembly}: published test window "
            f"{self.window_min_seconds // 60}-{self.window_max_seconds // 60} min "
            f"of direct fire exposure ({self.citation}). "
            f"Elapsed since dispatch: {minutes:.1f} min. {self.disclaimer}"
        )


def truss_time_window(
    *, dispatched_at: datetime, now: datetime, fact_id: str | None = None
) -> MaterialTimeWindow:
    """Build the window for a lightweight truss floor.

    Called only when a fact says the assembly is present. There is no inference
    from construction type or year built -- an assembly nobody recorded is not
    an assembly this timer speaks about.
    """
    elapsed = max(0.0, (now - dispatched_at).total_seconds())
    return MaterialTimeWindow(
        assembly="Lightweight parallel-chord truss floor",
        canonical_key=Keys.LIGHTWEIGHT_TRUSS,
        fact_id=fact_id,
        window_min_seconds=int(TRUSS_WINDOW_MIN.total_seconds()),
        window_max_seconds=int(TRUSS_WINDOW_MAX.total_seconds()),
        citation=TRUSS_CITATION,
        elapsed_seconds=elapsed,
    )
