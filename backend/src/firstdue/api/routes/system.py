"""System status for the command center.

The console renders exactly what this reports: which mode the backend is in,
which municipality it is configured for, and which capabilities this build
actually has. Nothing on screen claims a surface that does not exist yet.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from firstdue.api.dependencies import Caller, require_read
from firstdue.api.routes.health import get_container
from firstdue.capabilities import CAPABILITIES, CapabilityInfo
from firstdue.container import Container

router = APIRouter(prefix="/system", tags=["system"])


class SystemStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str
    version: str
    environment: str
    #: "fake" or "live". Displayed prominently -- a hidden simulation is worse
    #: than an admitted one.
    mode: str
    #: Where durable memory lives: "memory" or "firestore".
    storage_backend: str
    #: How events move: "memory" or "pubsub".
    event_backend: str
    #: "fake" or "google": whether the survey calendar and crew mail reach
    #: Google Workspace. A live deployment can legitimately be "fake" here --
    #: Calendar and Gmail need delegated user authority the other integrations
    #: do not -- so a recorded-but-not-sent notification is stated, not implied.
    workspace_writes: str
    municipality_id: str
    districts: list[str]
    instant_brief_budget_ms: int
    #: Profiles loaded from deterministic demo state. Zero is an honest zero.
    seeded_profiles: int
    #: Agent descriptors published in the registry at startup.
    published_agents: int
    capabilities: list[CapabilityInfo]
    disclosure: str


DISCLOSURE = (
    "Decision-support prototype, not a certified public-safety system. "
    "CAD, referral intake, and the records system are simulated receiving APIs "
    "with real write semantics. EMS, mutual-aid, Tier II, and thermal fixtures "
    "are synthetic; no real person's records appear in this project."
)


@router.get("/status", response_model=SystemStatus, summary="Build and mode status")
async def system_status(
    request: Request,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> SystemStatus:
    from firstdue import __version__

    settings = container.settings
    notes = request.app.state.lifecycle.notes
    seeded = int(notes.get("seeded_profiles", "0"))
    published_agents = int(notes.get("published_agents", "0"))
    return SystemStatus(
        app=settings.app_name,
        version=__version__,
        environment=str(settings.app_env),
        mode=container.mode,
        storage_backend=container.storage_label,
        event_backend=container.event_label,
        workspace_writes=container.workspace_label,
        municipality_id=container.city.municipality_id,
        districts=list(container.city.list_districts()),
        instant_brief_budget_ms=settings.instant_brief_budget_ms,
        seeded_profiles=seeded,
        published_agents=published_agents,
        capabilities=list(CAPABILITIES),
        disclosure=DISCLOSURE,
    )
