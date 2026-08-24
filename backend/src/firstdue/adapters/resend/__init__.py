"""Resend-backed mail.

One class, behind the mail port the fleet already has. Workspace mail acts as a
user and needs delegated authority a service account does not carry; Resend
answers to an API key and reaches an address outside the department's own
domain. That is the whole reason both exist: the crew notification goes to a
firefighter's inbox, and the approved referral goes to another agency.

The choice between them is a wiring decision in the container. Nothing above
the port knows which one it has.
"""

from __future__ import annotations

from firstdue.adapters.resend.mail import (
    API_KEY_SETTING,
    RESEND_ENDPOINT,
    SENDER_SETTING,
    ResendMailClient,
)

__all__ = ["API_KEY_SETTING", "RESEND_ENDPOINT", "SENDER_SETTING", "ResendMailClient"]
