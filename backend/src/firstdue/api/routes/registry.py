"""The agent registry API.

Departments publish agents; other departments subscribe to a **pinned version**.
Pinning is not devops hygiene here -- a NIOSH line-of-duty-death investigation
has to reconstruct what an incident commander knew two years ago, so a
subscription names an exact version and publishing a newer one does not move it.
Upgrading is a decision a department makes, not something that happens to them.

Three properties this API guarantees, each with a test that asserts the failure:

* **A published version is immutable.** Republishing the identical descriptor is
  a no-op; republishing a *different* descriptor at the same version is a 409.
* **Subscriptions pin.** ``resolve`` returns the pinned version, whatever newer
  versions exist.
* **Ordering is deterministic.** Capability, scope, and classification sets are
  serialised sorted, so two processes return byte-identical catalog responses.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from firstdue.api.dependencies import Caller, require_profile_write, require_read
from firstdue.api.routes.health import get_container
from firstdue.container import Container
from firstdue.domain.enums import ApprovalThreshold, Capability, Department, Loop
from firstdue.domain.registry import AgentDescriptor, SemVer, Subscription
from firstdue.errors import NotFoundError
from firstdue.registry.seed import subscription_id_for

router = APIRouter(prefix="/registry", tags=["registry"])


class AgentDescriptorView(BaseModel):
    """A catalog entry, with every set serialised in sorted order."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    version: str
    ref: str
    publisher_department: Department
    loop: Loop
    role_summary: str

    capabilities: list[Capability]
    required_scopes: list[str]
    classifications_accessed: list[str]
    write_targets: list[str]
    approval_threshold: ApprovalThreshold

    input_schema_ref: str
    output_schema_ref: str
    latency_target_ms: int
    published_at: str
    deprecated_at: str | None = None

    @classmethod
    def of(cls, descriptor: AgentDescriptor) -> AgentDescriptorView:
        return cls(
            agent_id=descriptor.agent_id,
            version=descriptor.version,
            ref=descriptor.ref,
            publisher_department=descriptor.publisher_department,
            loop=descriptor.loop,
            role_summary=descriptor.role_summary,
            capabilities=sorted(descriptor.capabilities),
            required_scopes=sorted(str(s) for s in descriptor.required_scopes),
            classifications_accessed=sorted(str(c) for c in descriptor.classifications_accessed),
            write_targets=sorted(descriptor.write_targets),
            approval_threshold=descriptor.approval_threshold,
            input_schema_ref=descriptor.input_schema_ref,
            output_schema_ref=descriptor.output_schema_ref,
            latency_target_ms=descriptor.latency_target_ms,
            published_at=descriptor.published_at.isoformat(),
            deprecated_at=(
                descriptor.deprecated_at.isoformat() if descriptor.deprecated_at else None
            ),
        )


class SubscriptionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    subscriber_department: Department
    agent_id: str
    pinned_version: str
    ref: str
    subscribed_at: str
    unsubscribed_at: str | None = None

    @classmethod
    def of(cls, subscription: Subscription) -> SubscriptionView:
        return cls(
            subscription_id=subscription.subscription_id,
            subscriber_department=subscription.subscriber_department,
            agent_id=subscription.agent_id,
            pinned_version=subscription.pinned_version,
            ref=subscription.ref,
            subscribed_at=subscription.subscribed_at.isoformat(),
            unsubscribed_at=(
                subscription.unsubscribed_at.isoformat() if subscription.unsubscribed_at else None
            ),
        )


class AgentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: list[AgentDescriptorView]
    count: int


class SubscriptionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscriptions: list[SubscriptionView]
    count: int


class SubscribeRequest(BaseModel):
    """Bind a department to one exact version of an agent."""

    model_config = ConfigDict(extra="forbid")

    subscriber_department: Department
    agent_id: str = Field(min_length=1, max_length=120)
    #: Pinned. Never a range, never "latest" -- the type refuses both.
    pinned_version: SemVer
    #: Optional. Derived from department and agent when omitted, so re-binding
    #: replaces the subscription instead of accumulating another one.
    subscription_id: str | None = Field(default=None, max_length=120)


# ------------------------------------------------------------------- agents


@router.post(
    "/agents",
    response_model=AgentDescriptorView,
    summary="Publish an agent descriptor",
    responses={409: {"description": "This version is already published, differently."}},
)
async def publish_agent(
    descriptor: AgentDescriptor,
    response: Response,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> AgentDescriptorView:
    """Publish a descriptor.

    Idempotent for an identical descriptor and a 409 for a changed one: a
    version somebody has pinned must not turn into different code underneath
    them.
    """
    existing = await container.registry.get_agent(descriptor.agent_id, descriptor.version)
    published = await container.registry.publish(descriptor)
    response.status_code = status.HTTP_200_OK if existing else status.HTTP_201_CREATED
    return AgentDescriptorView.of(published)


@router.get("/agents", response_model=AgentListResponse, summary="Discover agents")
async def list_agents(
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
    publisher_department: Annotated[Department | None, Query()] = None,
    loop: Annotated[Loop | None, Query()] = None,
    capability: Annotated[Capability | None, Query()] = None,
    agent_id: Annotated[str | None, Query(max_length=120)] = None,
) -> AgentListResponse:
    """List the catalog, filtered. Every filter is an ``AND``."""
    agents = await container.registry.list_agents(
        publisher_department=str(publisher_department) if publisher_department else None
    )
    selected = [
        agent
        for agent in agents
        if (loop is None or agent.loop is loop)
        and (capability is None or capability in agent.capabilities)
        and (agent_id is None or agent.agent_id == agent_id)
    ]
    return AgentListResponse(
        agents=[AgentDescriptorView.of(a) for a in selected], count=len(selected)
    )


@router.get(
    "/agents/{agent_id}/{version}",
    response_model=AgentDescriptorView,
    summary="Fetch one published version",
)
async def get_agent(
    agent_id: str,
    version: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> AgentDescriptorView:
    descriptor = await container.registry.get_agent(agent_id, version)
    if descriptor is None:
        raise NotFoundError(
            "no such published agent version",
            details={"agent_id": agent_id, "version": version},
        )
    return AgentDescriptorView.of(descriptor)


# ------------------------------------------------------------ subscriptions


@router.post(
    "/subscriptions",
    response_model=SubscriptionView,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe a department to a pinned version",
    responses={404: {"description": "That agent version has not been published."}},
)
async def subscribe(
    request: SubscribeRequest,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> SubscriptionView:
    """Pin a department to one version.

    Subscribing to an unpublished version is a 404 rather than a promise to
    resolve later: a pin to something that does not exist is not a pin.
    """
    subscription = Subscription(
        subscription_id=(
            request.subscription_id
            or subscription_id_for(request.subscriber_department, request.agent_id)
        ),
        subscriber_department=request.subscriber_department,
        agent_id=request.agent_id,
        pinned_version=request.pinned_version,
        subscribed_at=container.clock.now(),
    )
    stored = await container.registry.subscribe(subscription)
    return SubscriptionView.of(stored)


@router.get(
    "/subscriptions",
    response_model=SubscriptionListResponse,
    summary="List subscriptions",
)
async def list_subscriptions(
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
    subscriber_department: Annotated[Department | None, Query()] = None,
) -> SubscriptionListResponse:
    subscriptions = await container.registry.list_subscriptions(
        subscriber_department=str(subscriber_department) if subscriber_department else None
    )
    return SubscriptionListResponse(
        subscriptions=[SubscriptionView.of(s) for s in subscriptions],
        count=len(subscriptions),
    )


@router.get(
    "/subscriptions/{subscriber_department}/{agent_id}/resolved",
    response_model=AgentDescriptorView,
    summary="Resolve the pinned version a department runs",
)
async def resolve_pinned(
    subscriber_department: Department,
    agent_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> AgentDescriptorView:
    """The exact descriptor this department runs.

    Publishing a newer version does not change this answer. That is the point.
    """
    descriptor = await container.registry.resolve_pinned(str(subscriber_department), agent_id)
    if descriptor is None:
        raise NotFoundError(
            "no subscription for this department and agent",
            details={"department": str(subscriber_department), "agent_id": agent_id},
        )
    return AgentDescriptorView.of(descriptor)
