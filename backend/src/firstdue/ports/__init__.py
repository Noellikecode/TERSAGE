"""Ports: the protocols every adapter -- fake or live -- must satisfy.

Fake adapters are not mocks. They implement these same protocols with the same
authorization rules, the same idempotency behaviour, the same event ordering,
and the same failure modes, which is what makes credential-free mode a genuine
test of the system rather than a demo of a different system.
"""
