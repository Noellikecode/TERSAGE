"""The entry package: an assessment, a path, a brief, two approvals, one send.

Three artifacts travel together because they are only meaningful together. The
readiness assessment says what the record could not settle; the path is computed
over exactly the data that assessment describes; the brief is prose about both.
Approving the path without having read the brief, or the brief without the path
it describes, would be approving half a document.

**Two approvals, and both of them are human.** The path and the brief are
separate :class:`~firstdue.domain.work.ApprovalRequest` records -- the same type,
the same repository and the same console surface the resource agent stages a
gas shutoff on -- because they are different judgements. One is "this is a route
I would send a crew down"; the other is "this is an accurate account of what we
know". A single tap covering both would collapse them, and the second is the one
an officer can check line by line against the citations.

**Sent is a state, not a side effect.** ``sent_at`` is set by one method, it
refuses unless both approvals are ``GRANTED``, and it records the gateway
decision that allowed it. Nothing else in this module writes it, so "was this
package handed to a crew" has one answer with one place to look.

**Not ready does not block a send, and does not go quiet either.** The
assessment is carried on the package and printed on the PDF whichever way it
went. A commander may knowingly dispatch a package with three criteria
outstanding; what this refuses to allow is dispatching one whose gaps nobody
stated. Distributing the authority the other way -- letting an assessment veto a
chief -- would be this system making a tactical decision, which is the line it
does not cross.

**The log is the store.** Every state change appends a fresh
:data:`~firstdue.domain.enums.LogEntryType.ENTRY_PACKAGE` entry carrying the
whole document, and reading a package means taking the last entry that names it.
So the approval history is the entry sequence, it is gapless, it is sealed at
incident close, and no version of a package can overwrite an earlier one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import ConfigDict, Field, computed_field

from firstdue.domain.enums import LogEntryType
from firstdue.domain.logentries import IncidentLogEntry
from firstdue.incident.crewbrief import SECTION_ORDER, CrewBrief
from firstdue.incident.documents import RecordedDocument
from firstdue.incident.entrypath import EntryPathPlan
from firstdue.incident.pdf import PdfBlock, render_pdf
from firstdue.incident.readiness import ReadinessAssessment
from firstdue.observability.logging import get_logger
from firstdue.ports.repositories import IncidentLogRepository

logger = get_logger(__name__)

#: Printed on the artifact, and required to survive any wording change. A page
#: that lost this sentence would read as an order.
PACKAGE_DISCLAIMER: Final[str] = (
    "Decision support. This package reports what the record holds and the cheapest "
    "traverse of a graph priced from it. It is not a tactical recommendation; every "
    "tactical decision belongs to the incident commander."
)

#: The two halves, named once. Used to build the approval ids and read back.
PATH_HALF: Final[str] = "entry-path"
BRIEF_HALF: Final[str] = "crew-brief"


class PackageStatus(StrEnum):
    """Derived from the approvals and the send, never set independently."""

    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    #: Both halves approved and nobody has sent it yet.
    READY_TO_SEND = "READY_TO_SEND"
    SENT = "SENT"


def approval_id_for(package_id: str, half: str) -> str:
    """The approval record's id.

    Shaped like the resource agent's ``apr_{incident}_{kind}`` so one console
    list can show both, and deliberately *not* resolvable by
    :meth:`IncidentSession.approve` -- that method reads the trailing segment as
    a resource kind, and neither of these halves is one. A package half is
    granted through its own endpoint, which is the one that knows a package has
    two of them.
    """
    return f"apr_{package_id}_{half}"


class EntryPackage(RecordedDocument):
    """One assessment, one path, one brief, and what humans did about them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=120)
    created_by_version: str = Field(default="1.0.0", max_length=40)

    assessment: ReadinessAssessment
    path: EntryPathPlan
    brief: CrewBrief

    path_approval_id: str = Field(min_length=1, max_length=120)
    brief_approval_id: str = Field(min_length=1, max_length=120)
    path_approved_by: str | None = Field(default=None, max_length=120)
    path_approved_at: datetime | None = None
    brief_approved_by: str | None = Field(default=None, max_length=120)
    brief_approved_at: datetime | None = None

    sent_at: datetime | None = None
    sent_by: str | None = Field(default=None, max_length=120)
    #: The gateway decision that permitted the send. Present only once it was.
    dispatch_decision_id: str = Field(default="", max_length=120)

    disclaimer: str = PACKAGE_DISCLAIMER

    @computed_field  # type: ignore[prop-decorator]
    @property
    def path_approved(self) -> bool:
        return self.path_approved_at is not None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def brief_approved(self) -> bool:
        return self.brief_approved_at is not None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> PackageStatus:
        if self.sent_at is not None:
            return PackageStatus.SENT
        if self.path_approved and self.brief_approved:
            return PackageStatus.READY_TO_SEND
        return PackageStatus.AWAITING_APPROVAL

    @computed_field  # type: ignore[prop-decorator]
    @property
    def outstanding_halves(self) -> tuple[str, ...]:
        """Which halves are still waiting. Empty once both are granted."""
        return tuple(
            half
            for half, approved in (
                (PATH_HALF, self.path_approved),
                (BRIEF_HALF, self.brief_approved),
            )
            if not approved
        )

    def with_approval(self, half: str, *, decided_by: str, at: datetime) -> EntryPackage:
        """Record one human tap. Unknown halves are refused by the caller."""
        if half == PATH_HALF:
            return self.model_copy(update={"path_approved_by": decided_by, "path_approved_at": at})
        return self.model_copy(update={"brief_approved_by": decided_by, "brief_approved_at": at})

    def sent(self, *, by: str, at: datetime, decision_id: str) -> EntryPackage:
        return self.model_copy(
            update={"sent_at": at, "sent_by": by, "dispatch_decision_id": decision_id}
        )


# ------------------------------------------------------------------ the store


def package_content(package: EntryPackage, *, note: str, trigger: str = "") -> dict[str, Any]:
    """The log entry's content: the whole package at one point in its life.

    The document goes in verbatim, like the focus does, so a reader
    reconstructs exactly what was approved rather than a summary of it. The
    flattened fields beside it are for the console and the NERIS draft, which
    count entries and never parse them.

    ``profile_snapshot_id`` is named at the top level because
    :meth:`~firstdue.incident.recorder.IncidentRecorder._append` reads it from
    there to stamp the entry -- without it the entry would carry the ``pending``
    placeholder while the snapshot id sat two levels down inside the package.

    **The flattened set is the console's contract**, and it grew when the loop
    started composing on its own. A tablet watching ``/log/stream`` has to be
    able to raise an approval card off one frame: ``status`` says a package is
    awaiting one, ``outstanding`` says which halves, and the two approval ids
    are the endpoints it posts to. Making it descend into ``package`` for those
    would mean every console re-deriving a status the document already computes,
    and two of them disagreeing about what "ready to sign" means.

    ``trigger`` and ``failed_criteria`` are the other half of that. "The fleet
    judged this ready" and "the fleet ran out of time and staged what it had"
    are different claims, and a console that could not tell them apart would
    render a fallback with the confidence of a ready one -- which is precisely
    the confusion the readiness assessment exists to prevent.
    """
    return {
        "package": package.model_dump(mode="json"),
        "package_id": package.package_id,
        "profile_snapshot_id": package.assessment.profile_snapshot_id,
        "status": str(package.status),
        "note": note[:200],
        "ready": package.assessment.ready,
        "failed_criteria": list(package.assessment.failed_ids),
        "path_refused": package.path.refused,
        "prose_source": package.brief.prose_source,
        "outstanding": list(package.outstanding_halves),
        "path_approval_id": package.path_approval_id,
        "brief_approval_id": package.brief_approval_id,
        # Empty when a human asked for this package. One of
        # :class:`~firstdue.incident.autonomy.AutonomyTrigger` when the loop
        # composed it unprompted.
        "autonomy_trigger": trigger,
    }


def _decode(entry: IncidentLogEntry) -> EntryPackage | None:
    payload: Any = entry.content.get("package")
    if not isinstance(payload, dict):  # pragma: no cover - package_content always writes one
        logger.warning("entry_package_unreadable", extra={"sequence": entry.sequence})
        return None
    return EntryPackage.model_validate(payload)


async def list_packages(log: IncidentLogRepository, incident_id: str) -> tuple[EntryPackage, ...]:
    """Every package this incident produced, latest version of each, in order.

    First-seen order rather than latest-entry order, so a package that was
    approved twenty minutes after it was staged does not jump the list it has
    been sitting in.
    """
    stored = await log.get_log(incident_id)
    latest: dict[str, EntryPackage] = {}
    order: list[str] = []
    for entry in stored.entries:
        if entry.entry_type is not LogEntryType.ENTRY_PACKAGE:
            continue
        package = _decode(entry)
        if package is None:  # pragma: no cover - see _decode
            continue
        if package.package_id not in latest:
            order.append(package.package_id)
        latest[package.package_id] = package
    return tuple(latest[package_id] for package_id in order)


async def get_package(
    log: IncidentLogRepository, incident_id: str, package_id: str
) -> EntryPackage | None:
    """The current state of one package, or ``None``."""
    for package in await list_packages(log, incident_id):
        if package.package_id == package_id:
            return package
    return None


# -------------------------------------------------------------------- the PDF


def _approval_line(half: str, approved_by: str | None, at: datetime | None) -> str:
    if at is None or not approved_by:
        return f"{half}: not approved. Nobody has signed this half."
    return f"{half}: approved by {approved_by} at {at.isoformat()}."


def package_blocks(package: EntryPackage) -> tuple[PdfBlock, ...]:
    """The printed brief, laid out. Deterministic, and the same every render.

    The prose comes first because it is what a crew reads under load, and the
    citation table comes after it because it is what somebody checks afterwards.
    Both are on the same sheet: prose whose sources are on another page is prose
    nobody checks.
    """
    blocks: list[PdfBlock] = [
        PdfBlock(style="title", text=f"Crew brief -- {package.address_id}"),
        PdfBlock(
            text=(
                f"Package {package.package_id} | incident {package.incident_id} | "
                f"composed {package.created_at.isoformat()} by {package.created_by} "
                f"v{package.created_by_version} | status {package.status}"
            )
        ),
        PdfBlock(
            text=(
                f"Profile snapshot {package.assessment.profile_snapshot_id} | "
                f"prose source: {package.brief.prose_source}"
                + (
                    f" (composition refused: {package.brief.prose_rejection})"
                    if package.brief.prose_rejection
                    else ""
                )
            )
        ),
        PdfBlock(style="heading", text="Readiness"),
        PdfBlock(text=package.assessment.summary),
    ]
    blocks.extend(
        PdfBlock(
            text=(
                f"[{'PASS' if criterion.passed else 'FAIL'}] {criterion.title} -- "
                f"{criterion.reason}"
                + (f" Checked: {', '.join(criterion.refs)}." if criterion.refs else "")
            ),
            indent=12.0,
        )
        for criterion in package.assessment.criteria
    )

    blocks.append(PdfBlock(style="heading", text="Brief"))
    blocks.extend(PdfBlock(text=line) for line in package.brief.prose.splitlines() if line.strip())

    blocks.append(PdfBlock(style="heading", text="Every claim, and what it rests on"))
    for section in SECTION_ORDER:
        rows = package.brief.section(section)
        if not rows:
            continue
        blocks.append(PdfBlock(text=section, indent=6.0))
        blocks.extend(
            PdfBlock(
                text=f"{claim.text}"
                + (f"  [{', '.join(claim.refs)}]" if claim.refs else "  [no reference]"),
                indent=18.0,
            )
            for claim in rows
        )

    blocks.append(PdfBlock(style="heading", text="Route"))
    if package.path.refused:
        blocks.append(PdfBlock(text=f"Refused: {package.path.refusal_reason}"))
    elif package.path.entry is not None:
        blocks.append(
            PdfBlock(
                text=(
                    f"{package.path.algorithm} with a {package.path.heuristic} heuristic over "
                    f"{package.path.node_count} node(s) and {package.path.edge_count} edge(s); "
                    f"{package.path.entry.expanded_nodes} node(s) expanded."
                )
            )
        )
        blocks.extend(
            PdfBlock(
                text=(
                    f"Leg {index + 1}: {leg.from_id} -> {leg.to_id}. {leg.chose_because}."
                    + ("  Avoided: " + "; ".join(leg.avoided) if leg.avoided else "")
                ),
                indent=12.0,
            )
            for index, leg in enumerate(package.path.entry.legs)
        )
        if package.path.egress is not None:
            blocks.append(
                PdfBlock(
                    text=(
                        "Second way out: "
                        + " -> ".join(w.node_id for w in package.path.egress.waypoints)
                    ),
                    indent=12.0,
                )
            )
        elif package.path.egress_note:
            blocks.append(PdfBlock(text=package.path.egress_note, indent=12.0))
    blocks.extend(
        PdfBlock(text=f"Barrier: {barrier.reason}.", indent=12.0)
        for barrier in package.path.barriers
    )

    blocks.append(PdfBlock(style="heading", text="Approvals"))
    blocks.append(
        PdfBlock(
            text=_approval_line(PATH_HALF, package.path_approved_by, package.path_approved_at),
            indent=12.0,
        )
    )
    blocks.append(
        PdfBlock(
            text=_approval_line(BRIEF_HALF, package.brief_approved_by, package.brief_approved_at),
            indent=12.0,
        )
    )
    blocks.append(
        PdfBlock(
            text=(
                f"Sent to the crew by {package.sent_by} at {package.sent_at.isoformat()} "
                f"under gateway decision {package.dispatch_decision_id}."
                if package.sent_at is not None and package.sent_by
                else "Not sent. A package is sent only once both halves are approved."
            ),
            indent=12.0,
        )
    )

    blocks.append(PdfBlock(style="heading", text="Standing caveats"))
    blocks.append(PdfBlock(text=package.disclaimer))
    return tuple(blocks)


def package_pdf(package: EntryPackage) -> bytes:
    """The whole package as one downloadable sheet."""
    return render_pdf(
        package_blocks(package),
        title=f"Crew brief {package.package_id} - {package.address_id}",
    )


def package_pdf_filename(package: EntryPackage) -> str:
    """A filename a browser will accept and a records clerk can file."""
    return f"crew-brief-{package.package_id}.pdf"


def summarise(packages: Sequence[EntryPackage]) -> tuple[dict[str, str], ...]:
    """The list view: ids, statuses and counts. Never a claim or a value."""
    return tuple(
        {
            "package_id": package.package_id,
            "status": str(package.status),
            "created_at": package.created_at.isoformat(),
            "ready": str(package.assessment.ready).lower(),
            "path_refused": str(package.path.refused).lower(),
            "outstanding": ",".join(package.outstanding_halves),
        }
        for package in packages
    )


__all__ = [
    "BRIEF_HALF",
    "PACKAGE_DISCLAIMER",
    "PATH_HALF",
    "EntryPackage",
    "PackageStatus",
    "approval_id_for",
    "get_package",
    "list_packages",
    "package_blocks",
    "package_content",
    "package_pdf",
    "package_pdf_filename",
    "summarise",
]
