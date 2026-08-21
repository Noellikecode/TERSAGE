"""Services: the thin layer where pure engines meet durable stores.

The deterministic engines in ``domain/`` take state and return state. Something
has to read that state, hold a lock while it works, write the result under
optimistic concurrency, and publish what happened. That is all this package
does, and it does not decide anything -- every decision it acts on came out of a
pure function that can be re-run and checked.
"""

from __future__ import annotations

from firstdue.services.materialization import (
    MaterializationOutcome,
    ProfileMaterializer,
)

__all__ = ["MaterializationOutcome", "ProfileMaterializer"]
