"""The incident loop.

Seconds, not months. Everything here reads what the slow loop already built and
puts it in front of a commander -- it derives no new structural knowledge, and
it recommends nothing.

One agent runs it since the merge: ``incident-interceptor``, which reads the
911 or CAD intake, opens the incident, streams the three-stage brief, and routes
the incident to the other incident agents by their declared capabilities.
"""

from __future__ import annotations

from firstdue.incident.autonomy import COMPOSE_DEADLINE, AutonomyTrigger
from firstdue.incident.controller import IncidentController, OpenIncidentResult
from firstdue.incident.crewbrief import BriefClaim, CrewBrief
from firstdue.incident.entrypath import EntryPathPlan, GeoOrigin, Route, compute_entry_path
from firstdue.incident.fusion import SensorFusion, ThermalFrame
from firstdue.incident.handoff import WAKE_RULES, Handoff, RoutingPlan, plan_handoffs
from firstdue.incident.intake import (
    IntakeChannel,
    IntakeReader,
    IntakeReading,
    IntakeSignals,
    ReportedItem,
)
from firstdue.incident.interceptor import IncidentInterceptor, InterceptResult
from firstdue.incident.packages import EntryPackage, PackageStatus, package_pdf
from firstdue.incident.readiness import Criterion, ReadinessAssessment
from firstdue.incident.reconciler import Reconciler
from firstdue.incident.recorder import IncidentRecorder
from firstdue.incident.resources import ResourceAgent
from firstdue.incident.session import IncidentSession, get_session
from firstdue.incident.timer import MaterialTimeWindow, truss_time_window

__all__ = [
    "COMPOSE_DEADLINE",
    "WAKE_RULES",
    "AutonomyTrigger",
    "BriefClaim",
    "CrewBrief",
    "Criterion",
    "EntryPackage",
    "EntryPathPlan",
    "GeoOrigin",
    "Handoff",
    "IncidentController",
    "IncidentInterceptor",
    "IntakeChannel",
    "IntakeReader",
    "IntakeReading",
    "IntakeSignals",
    "InterceptResult",
    "IncidentRecorder",
    "IncidentSession",
    "MaterialTimeWindow",
    "OpenIncidentResult",
    "PackageStatus",
    "ReadinessAssessment",
    "Reconciler",
    "ReportedItem",
    "ResourceAgent",
    "Route",
    "RoutingPlan",
    "SensorFusion",
    "ThermalFrame",
    "compute_entry_path",
    "get_session",
    "package_pdf",
    "plan_handoffs",
    "truss_time_window",
]
