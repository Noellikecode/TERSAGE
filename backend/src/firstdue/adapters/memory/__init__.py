"""Deterministic in-memory adapters.

These are the credential-free path. They are not mocks: they enforce the same
optimistic concurrency, append-only sequencing, idempotency dedupe, and event
ordering that the Firestore and Pub/Sub adapters will.
"""
