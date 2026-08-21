"""The FIRST DUE domain model.

Every invariant this system depends on is enforced in this package rather than
in the services that use it: provenance, survey-gated verification, first-class
absence, append-only history, optimistic concurrency, idempotent writes,
model-free instant briefs, and the classification gate in front of the vector
layer.
"""
