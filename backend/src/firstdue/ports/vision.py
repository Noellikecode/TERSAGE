"""The imagery port -- one verb, for the same reason the model port has four.

``observe`` is the only capability. There is no ``identify_face``, no
``assess``, no ``locate_fire``. The face is resolved from the footprint the
**Geometry Watcher** measured during the slow loop, by
:func:`~firstdue.domain.geometry.resolve_face`, and a verb that could name a
wall would be a second answer to a question the geometry already answers -- one
that could disagree with it silently.

Two implementations, held to one contract: Gemini multimodal on Vertex, and a
deterministic fake so ``make demo`` keeps needing no credentials.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from firstdue.domain.vision import VisionResult


@runtime_checkable
class VisionClient(Protocol):
    """Reads one frame and reports what it can see, bound to image regions."""

    @property
    def model_ref(self) -> str:
        """Model identifier recorded on every observation, for replay."""
        ...

    async def observe(
        self,
        *,
        image: bytes,
        mime_type: str,
        deadline_ms: int,
    ) -> VisionResult:
        """Extract observations from one frame.

        Never raises for a bad frame or a bad response: an unusable result is
        ``accepted=False`` with a reason. A raise here would turn one unreadable
        photograph into a failed thermal pass.
        """
        ...
