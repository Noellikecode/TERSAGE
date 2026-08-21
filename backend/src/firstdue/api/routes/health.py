"""Liveness and readiness.

``/healthz`` answers "is this process alive". ``/readyz`` answers "should this
process receive an incident right now", which is a different question during
startup and while draining after SIGTERM.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from firstdue.container import Container
from firstdue.lifecycle import Lifecycle

router = APIRouter(tags=["health"])


def get_lifecycle(request: Request) -> Lifecycle:
    lifecycle: Lifecycle = request.app.state.lifecycle
    return lifecycle


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


class LivenessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    app: str
    version: str


class ComponentCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    detail: str


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    ready: bool
    mode: str
    checks: list[ComponentCheck]


@router.get("/healthz", response_model=LivenessResponse, summary="Liveness")
async def healthz(
    container: Annotated[Container, Depends(get_container)],
) -> LivenessResponse:
    from firstdue import __version__

    return LivenessResponse(status="alive", app=container.settings.app_name, version=__version__)


@router.get("/readyz", response_model=ReadinessResponse, summary="Readiness")
async def readyz(
    response: Response,
    container: Annotated[Container, Depends(get_container)],
    lifecycle: Annotated[Lifecycle, Depends(get_lifecycle)],
) -> ReadinessResponse:
    checks: list[ComponentCheck] = [
        ComponentCheck(
            name="lifecycle",
            ok=lifecycle.accepts_traffic,
            detail=lifecycle.state,
        ),
        ComponentCheck(
            name="city-adapter",
            ok=bool(container.city.list_districts()),
            detail=container.city.municipality_id,
        ),
        ComponentCheck(
            name="clock",
            ok=container.clock.now().tzinfo is not None,
            detail="timezone-aware",
        ),
        ComponentCheck(
            name="demo-state",
            # An unseeded process is ready; it just has an empty district.
            ok=True,
            detail=f"{lifecycle.notes.get('seeded_profiles', '0')} profiles loaded",
        ),
    ]
    ready = all(check.ok for check in checks)
    if not ready:
        response.status_code = 503
    return ReadinessResponse(
        status=lifecycle.state,
        ready=ready,
        mode=container.mode,
        checks=checks,
    )
