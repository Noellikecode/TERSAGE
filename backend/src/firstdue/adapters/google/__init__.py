"""Google-backed office adapters: Calendar, Gmail, Cloud Storage.

Boundaries, not integrations. Each one issues the single call it needs, maps the
response, and lets every failure surface as a domain error. Clients are imported
lazily, so a credential-free process never loads them and a checkout without the
``google`` extra still type-checks and tests.

Missing credentials are a startup failure in live mode, never a silent downgrade
to the fake. A department that thinks it scheduled a survey and did not is worse
off than one that saw the error.
"""

from __future__ import annotations

from firstdue.adapters.google.office import (
    GmailClient,
    GoogleCalendarClient,
    GoogleObjectStore,
)

__all__ = ["GmailClient", "GoogleCalendarClient", "GoogleObjectStore"]
