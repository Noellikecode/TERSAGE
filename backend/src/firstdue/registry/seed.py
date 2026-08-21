"""Publishing the fleet and pinning the department's subscriptions.

Seeding is idempotent because publication is: a descriptor republished
identically is the same catalog entry, and a descriptor republished *differently*
under the same version is refused. That is what makes restarting a process safe
and what makes "pinned versions stay pinned" mean something -- a version, once
published, is a fixed thing to pin to.

Subscription ids are derived from ``(department, agent_id)`` rather than minted,
so re-seeding rebinds the same subscription instead of accumulating a new one on
every boot.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from firstdue.domain.enums import Department
from firstdue.domain.registry import AgentDescriptor, Subscription
from firstdue.observability.logging import get_logger
from firstdue.ports.repositories import RegistryRepository
from firstdue.registry.descriptors import FLEET_VERSION, HOME_DEPARTMENT, fleet_descriptors

logger = get_logger(__name__)


def subscription_id_for(department: Department, agent_id: str) -> str:
    """Derived, so re-seeding rebinds rather than accumulates."""
    return f"sub_{department}_{agent_id}"


class RegistrySeedResult(BaseModel):
    """What seeding did. Reported at startup so an empty registry is visible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    published: int = 0
    subscribed: int = 0
    #: Descriptors already present at the same version. The restart case.
    already_published: int = 0


async def seed_registry(
    registry: RegistryRepository,
    *,
    now: datetime,
    department: Department = HOME_DEPARTMENT,
    version: str = FLEET_VERSION,
) -> RegistrySeedResult:
    """Publish every built-in descriptor and pin the department to it."""
    published = 0
    already = 0
    subscribed = 0

    for descriptor in fleet_descriptors():
        existing = await registry.get_agent(descriptor.agent_id, descriptor.version)
        await registry.publish(descriptor)
        if existing is None:
            published += 1
        else:
            already += 1

        await registry.subscribe(
            Subscription(
                subscription_id=subscription_id_for(department, descriptor.agent_id),
                subscriber_department=department,
                agent_id=descriptor.agent_id,
                pinned_version=version,
                subscribed_at=now,
            )
        )
        subscribed += 1

    result = RegistrySeedResult(
        published=published, subscribed=subscribed, already_published=already
    )
    logger.info(
        "registry_seeded",
        extra={
            "published": result.published,
            "already_published": result.already_published,
            "subscribed": result.subscribed,
            "department": str(department),
            "fleet_version": version,
        },
    )
    return result


async def resolve_fleet(
    registry: RegistryRepository, *, department: Department = HOME_DEPARTMENT
) -> dict[str, AgentDescriptor]:
    """The exact descriptor version this department runs for each agent.

    This is what an emission records: not "the records watcher", but the pinned
    version of it that produced the fact.
    """
    resolved: dict[str, AgentDescriptor] = {}
    for descriptor in fleet_descriptors():
        pinned = await registry.resolve_pinned(str(department), descriptor.agent_id)
        if pinned is not None:
            resolved[descriptor.agent_id] = pinned
    return resolved
