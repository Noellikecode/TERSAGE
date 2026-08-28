"""Closed vocabularies for the FIRST DUE domain.

Every enum here is a closed set on purpose. An open string where a decision is
made -- a policy action, a classification, a source tier -- is how "not checked"
silently becomes "safe".
"""

from __future__ import annotations

from enum import StrEnum


class Classification(StrEnum):
    """Statutory handling class for a fact.

    ``PHI`` and ``TIER_II_CONFIDENTIAL`` are the two classes that can never be
    serialized into a vector payload (see :mod:`firstdue.domain.vectors`).
    """

    PUBLIC = "PUBLIC"
    RESTRICTED = "RESTRICTED"
    PHI = "PHI"
    TIER_II_CONFIDENTIAL = "TIER_II_CONFIDENTIAL"


#: Classifications that may never leave the durable store into an embedding.
VECTOR_FORBIDDEN_CLASSIFICATIONS: frozenset[Classification] = frozenset(
    {Classification.PHI, Classification.TIER_II_CONFIDENTIAL}
)


class Department(StrEnum):
    """Municipal departments that publish agents or receive writes."""

    FIRE = "fire"
    BUILDING = "building"
    PLANNING = "planning"
    COUNTY_OEM = "county-oem"
    WATER = "water"
    PUBLIC_WORKS = "public-works"
    POLICE = "police"
    UTILITY = "utility"
    EMS = "ems"


class SourceType(StrEnum):
    """Where a fact came from. Drives :class:`SourceTier` and therefore merge order."""

    # Filed municipal record
    PERMIT = "PERMIT"
    ASSESSOR = "ASSESSOR"
    FIRE_INSPECTION = "FIRE_INSPECTION"
    VIOLATION = "VIOLATION"
    PARCEL = "PARCEL"
    HYDRANT = "HYDRANT"
    # Federal / state record
    EPA_RMP = "EPA_RMP"
    EPA_TRI = "EPA_TRI"
    EPA_FRS = "EPA_FRS"
    PHMSA_PIPELINE = "PHMSA_PIPELINE"
    NREL_EV = "NREL_EV"
    TIER_II = "TIER_II"
    # Remote measurement
    SOLAR_API = "SOLAR_API"
    LIDAR_DSM = "LIDAR_DSM"
    STREET_VIEW = "STREET_VIEW"
    # Live observation
    THERMAL_SENSOR = "THERMAL_SENSOR"
    IC_RESOLUTION = "IC_RESOLUTION"
    NWS_OBSERVATION = "NWS_OBSERVATION"
    # Human
    HUMAN_SURVEY = "HUMAN_SURVEY"
    # Simulated-receiving systems (real write semantics, synthetic contents)
    CAD = "CAD"
    RMS = "RMS"
    EMS_DERIVED = "EMS_DERIVED"
    # Machine derivation over other facts
    DERIVED = "DERIVED"


class SourceTier(StrEnum):
    """Merge precedence tier.

    Ordering is absolute and is checked before recency or confidence:
    a live observation always outranks a remembered fact.
    """

    LIVE_OBSERVATION = "LIVE_OBSERVATION"
    HUMAN_VERIFIED = "HUMAN_VERIFIED"
    AUTHORITATIVE_RECORD = "AUTHORITATIVE_RECORD"
    REMOTE_MEASUREMENT = "REMOTE_MEASUREMENT"
    DERIVED_INFERENCE = "DERIVED_INFERENCE"


#: Lower rank wins. Explicit table rather than enum order so the intent is greppable.
TIER_RANK: dict[SourceTier, int] = {
    SourceTier.LIVE_OBSERVATION: 0,
    SourceTier.HUMAN_VERIFIED: 1,
    SourceTier.AUTHORITATIVE_RECORD: 2,
    SourceTier.REMOTE_MEASUREMENT: 3,
    SourceTier.DERIVED_INFERENCE: 4,
}

_TIER_BY_SOURCE: dict[SourceType, SourceTier] = {
    SourceType.THERMAL_SENSOR: SourceTier.LIVE_OBSERVATION,
    SourceType.IC_RESOLUTION: SourceTier.LIVE_OBSERVATION,
    SourceType.NWS_OBSERVATION: SourceTier.LIVE_OBSERVATION,
    SourceType.HUMAN_SURVEY: SourceTier.HUMAN_VERIFIED,
    SourceType.PERMIT: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.ASSESSOR: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.FIRE_INSPECTION: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.VIOLATION: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.PARCEL: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.HYDRANT: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.EPA_RMP: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.EPA_TRI: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.EPA_FRS: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.PHMSA_PIPELINE: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.NREL_EV: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.TIER_II: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.CAD: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.RMS: SourceTier.AUTHORITATIVE_RECORD,
    SourceType.SOLAR_API: SourceTier.REMOTE_MEASUREMENT,
    SourceType.LIDAR_DSM: SourceTier.REMOTE_MEASUREMENT,
    SourceType.STREET_VIEW: SourceTier.REMOTE_MEASUREMENT,
    SourceType.EMS_DERIVED: SourceTier.DERIVED_INFERENCE,
    SourceType.DERIVED: SourceTier.DERIVED_INFERENCE,
}


def tier_for(source_type: SourceType) -> SourceTier:
    """Return the merge tier for a source type.

    Raises:
        KeyError: if a new ``SourceType`` was added without a tier. That is a
            deliberate hard failure -- an untiered source would merge
            unpredictably, and unpredictable merge order is a safety defect.
    """
    return _TIER_BY_SOURCE[source_type]


class AssertionStatus(StrEnum):
    """How the system regards an attribute, rendered distinctly in every surface."""

    CONFIRMED = "CONFIRMED"
    DISPUTED = "DISPUTED"
    UNKNOWN = "UNKNOWN"


class Capability(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    RANK = "RANK"
    NOTIFY = "NOTIFY"


class Scope(StrEnum):
    """Grant scopes. Read scopes and write scopes are deliberately separate:
    an agent authorized to read the permit system is not thereby authorized to
    file with it.
    """

    READ_PUBLIC_RECORDS = "read:public-records"
    READ_TIER_II_METADATA = "read:tier-ii-metadata"
    READ_EMS_DERIVED = "read:ems-derived"
    READ_PROFILE = "read:profile"
    READ_GEOMETRY = "read:geometry"
    READ_AUDIT = "read:audit"
    WRITE_PROFILE = "write:profile"
    WRITE_REFERRAL = "write:referral"
    WRITE_WORK_ORDER = "write:work-order"
    WRITE_PREINCIDENT_PLAN = "write:preincident-plan"
    WRITE_RMS = "write:rms"
    NOTIFY_AGENCY = "notify:agency"
    REQUEST_UTILITY_SHUTOFF = "write:utility-shutoff"
    REQUEST_ROAD_CLOSURE = "write:road-closure"


class Operation(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    NOTIFY = "NOTIFY"


class PolicyAction(StrEnum):
    """The five gateway outcomes. This set is closed; there is no implicit default."""

    ALLOW = "ALLOW"
    DERIVE = "DERIVE"
    WITHHOLD_JURISDICTION = "WITHHOLD_JURISDICTION"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class ApprovalThreshold(StrEnum):
    NONE = "NONE"
    SUPERVISOR = "SUPERVISOR"
    CHIEF = "CHIEF"


class AgentRunStatus(StrEnum):
    """The state of one agent run.

    ``PENDING`` and ``RUNNING`` are the only states a run may leave. Everything
    else is terminal: a run that reached one never transitions again, which is
    what makes "every run reaches a terminal state" checkable rather than hoped
    for. See :mod:`firstdue.domain.runs`.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"


class Loop(StrEnum):
    """Which tempo an agent runs at."""

    SLOW = "SLOW"
    INCIDENT = "INCIDENT"


class BriefStage(StrEnum):
    """Progressive emission stages. ``INSTANT`` is model-free by construction."""

    INSTANT = "INSTANT"
    ENRICHED = "ENRICHED"
    AMENDMENT = "AMENDMENT"


class LogEntryType(StrEnum):
    BRIEF_EMITTED = "BRIEF_EMITTED"
    BENCHMARK = "BENCHMARK"
    IC_RESOLUTION = "IC_RESOLUTION"
    NOTIFICATION_SENT = "NOTIFICATION_SENT"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    POLICY_DECISION = "POLICY_DECISION"
    FACT_OBSERVED = "FACT_OBSERVED"
    #: What the 911 call or CAD narrative was read as. Attribute names and
    #: outcomes, never the transcript: the log is the department's record, and
    #: the caller's words belong to the call recording, not to a copy of it here.
    INTAKE_READ = "INTAKE_READ"
    #: Which agent the interceptor woke, under which rule, with what. Recorded
    #: because "who was told" is the first question after an incident where
    #: somebody was not told.
    AGENT_HANDOFF = "AGENT_HANDOFF"
    #: What one agent concluded, in its own name.
    #:
    #: The log already recorded that an agent was *woken* and what it was
    #: *pointed at*; what it then made of them was visible only as a side
    #: effect -- a brief amendment attributed to whoever emitted it. So
    #: `sensor-fusion` could register four building faces and read as idle,
    #: because nothing in the record carried its name. This is the entry an
    #: agent writes about its own work, and it is what the console's per-agent
    #: cards are built from.
    #:
    #: Ids, keys and one-line summaries, like every other entry type here. An
    #: analysis that carried a source document would be a second copy of it.
    AGENT_ANALYSIS = "AGENT_ANALYSIS"
    #: What each woken agent was pointed at, in ids and canonical keys. The
    #: second question after an incident, once "who was told" is answered:
    #: they were told, and what were they told to look at. Stored in the log
    #: rather than beside it because a focus is exactly the kind of decision the
    #: log exists to make replayable. See :mod:`firstdue.incident.focus`.
    FOCUS_COMPOSED = "FOCUS_COMPOSED"


class BenchmarkType(StrEnum):
    """Operational benchmarks timestamped as they occur. Clerical, not tactical."""

    DISPATCH = "DISPATCH"
    ARRIVAL = "ARRIVAL"
    WATER_ON_FIRE = "WATER_ON_FIRE"
    PRIMARY_SEARCH_COMPLETE = "PRIMARY_SEARCH_COMPLETE"
    PAR = "PAR"
    INCIDENT_CLOSED = "INCIDENT_CLOSED"


class WriteActionStatus(StrEnum):
    DRAFTED = "DRAFTED"
    STAGED_FOR_APPROVAL = "STAGED_FOR_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    COMPENSATED = "COMPENSATED"


class SurveyOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    NO_ACCESS = "NO_ACCESS"
    PARTIAL = "PARTIAL"


class ReferralStatus(StrEnum):
    DRAFTED = "DRAFTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    FILED = "FILED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class FaceLabel(StrEnum):
    """Fireground face labelling."""

    ALPHA = "ALPHA"
    BRAVO = "BRAVO"
    CHARLIE = "CHARLIE"
    DELTA = "DELTA"
    ROOF = "ROOF"
