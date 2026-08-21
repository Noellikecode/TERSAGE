"""The incident loop.

Seconds, not months. Everything here reads what the slow loop already built and
puts it in front of a commander -- it derives no new structural knowledge, and
it recommends nothing.
"""

from __future__ import annotations

from firstdue.incident.controller import IncidentController, OpenIncidentResult
from firstdue.incident.fusion import SensorFusion, ThermalFrame
from firstdue.incident.reconciler import Reconciler
from firstdue.incident.recorder import IncidentRecorder
from firstdue.incident.resources import ResourceAgent
from firstdue.incident.session import IncidentSession, get_session
from firstdue.incident.timer import MaterialTimeWindow, truss_time_window

__all__ = [
    "IncidentController",
    "IncidentRecorder",
    "IncidentSession",
    "MaterialTimeWindow",
    "OpenIncidentResult",
    "Reconciler",
    "ResourceAgent",
    "SensorFusion",
    "ThermalFrame",
    "get_session",
    "truss_time_window",
]
