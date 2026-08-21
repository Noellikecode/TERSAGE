"""The gateway: a deterministic, versioned, default-deny policy engine.

Every read and write the fleet performs is a call to :meth:`PolicyEngine.decide`,
and every call produces a :class:`~firstdue.domain.policy.PolicyDecision` that
records what was asked, what was decided, which rule decided it, and at which
policy version. A decision with no rule is not expressible.

Four properties, each load-bearing:

**Default deny.** The rule list is ordered and finite. An access that matches no
rule is denied by :data:`FALLBACK_RULE_ID`, not allowed by omission. The default
is the safe one, so a rule someone forgets to write costs a refusal rather than
a leak.

**Deterministic.** No clock read, no randomness, no I/O, and -- checked by a
test -- no import that reaches model code. ``now`` arrives as an argument. The
same request at the same policy version produces the same decision forever,
which is what makes a NIOSH replay reconstruct what an officer was shown.

**Versioned.** :data:`POLICY_VERSION` is stamped on every decision. Replaying an
incident from two years ago means replaying it against the policy that was in
force, and the decision record says which one that was.

**No model participates.** ``PolicyDecision.decided_by`` is a constant literal.
A model can be asked to *explain* a decision after the fact; there is no code
path by which one can make it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import (
    ApprovalThreshold,
    Classification,
    Operation,
    PolicyAction,
    Scope,
)
from firstdue.domain.identity import IncidentGrant, StandingGrant, is_write_scope
from firstdue.domain.policy import PolicyDecision
from firstdue.gateway.jurisdiction import MutualAidAgreement, aid_agreement_for
from firstdue.observability.metrics import METRICS
from firstdue.observability.tracing import policy_span
from firstdue.ports.clock import IdGenerator

#: Bumped whenever a rule changes meaning. Stamped on every decision, so a
#: replay knows which policy produced the answer it is reconstructing.
POLICY_VERSION: Final[str] = "1.0.0"

#: The decision an unmatched request gets. Default deny, named so it is
#: greppable in the audit log.
FALLBACK_RULE_ID: Final[str] = "policy.default-deny"

Grant = IncidentGrant | StandingGrant

#: Writes that commit another agency's resources or affect a citizen. Each needs
#: a human tap; the threshold says whose.
APPROVAL_THRESHOLDS: Final[dict[Scope, ApprovalThreshold]] = {
    Scope.WRITE_REFERRAL: ApprovalThreshold.SUPERVISOR,
    Scope.WRITE_WORK_ORDER: ApprovalThreshold.SUPERVISOR,
    Scope.REQUEST_UTILITY_SHUTOFF: ApprovalThreshold.CHIEF,
    Scope.REQUEST_ROAD_CLOSURE: ApprovalThreshold.CHIEF,
}


@dataclass(frozen=True, slots=True)
class AccessRequest:
    """One thing an agent wants to do. Deliberately small and explicit."""

    agent_id: str
    agent_version: str
    grant: Grant
    target: str
    operation: Operation
    classification: Classification
    scope: Scope
    now: datetime
    #: Present for incident-loop access. Checked against the grant's binding.
    incident_id: str | None = None
    address_id: str | None = None
    #: The jurisdiction the records belong to, which may not be the grant's.
    record_jurisdiction_id: str | None = None
    responding_agency_id: str | None = None
    #: Set by the caller when a human has already approved this exact write.
    approval_id: str | None = None
    #: An incident commander's declared emergency exception, if any.
    emergency_exception: bool = False

    @property
    def is_incident_grant(self) -> bool:
        return isinstance(self.grant, IncidentGrant)


class Outcome(BaseModel):
    """A rule's answer, before it becomes a recorded decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: PolicyAction
    rule_id: str = Field(min_length=1, max_length=120)
    justification: str = Field(min_length=1, max_length=500)
    derivation_function: str | None = Field(default=None, max_length=120)
    mutual_aid_agreement_id: str | None = Field(default=None, max_length=120)
    approval_threshold: ApprovalThreshold | None = None


#: A rule looks at a request and either answers or abstains. Abstaining is
#: ``None``: only an explicit answer decides anything, and if every rule
#: abstains the fallback denies.
Rule = Callable[[AccessRequest], Outcome | None]


# ----------------------------------------------------------------- the rules
#
# Order matters and is fixed. Each rule is a small named function so a decision
# can cite it, and so a reader can check the whole policy in one screen.


def rule_expired_grant(request: AccessRequest) -> Outcome | None:
    """A grant that has expired or been revoked authorizes nothing."""
    if request.grant.is_expired(request.now):
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="grant.expired-or-revoked",
            justification="the grant has expired or was revoked at incident close",
        )
    return None


def rule_scope_missing(request: AccessRequest) -> Outcome | None:
    """The grant must carry the exact scope asked for.

    Not an adjacent one, and not a read scope standing in for a write. This is
    where "read never implies write" is enforced, and it is a single membership
    test precisely so there is no room for a widening rule.
    """
    if not request.grant.authorizes(request.scope):
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="grant.scope-not-held",
            justification=f"the grant does not carry {request.scope}",
        )
    return None


def rule_operation_matches_scope(request: AccessRequest) -> Outcome | None:
    """A write operation needs a write scope, and a read scope is not one."""
    write_operation = request.operation in (Operation.WRITE, Operation.NOTIFY)
    if write_operation and not is_write_scope(request.scope):
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="grant.read-scope-cannot-write",
            justification=(
                f"{request.operation} requires a write scope; {request.scope} is a read scope"
            ),
        )
    if not write_operation and is_write_scope(request.scope):
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="grant.write-scope-is-not-a-read",
            justification=f"{request.operation} must be authorized by a read scope",
        )
    return None


def rule_standing_grant_cannot_reach_people(request: AccessRequest) -> Outcome | None:
    """Between incidents, the fleet may read buildings and not people.

    The standing grant's own validator already refuses to hold PHI access. This
    rule catches the other direction: a standing grant being used to reach a PHI
    target it was never given.
    """
    if request.is_incident_grant:
        return None
    if request.classification in (Classification.PHI, Classification.RESTRICTED):
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="standing-grant.no-person-level-access",
            justification=(
                "a standing grant may only reach PUBLIC and TIER_II_CONFIDENTIAL records"
            ),
        )
    return None


def rule_incident_binding(request: AccessRequest) -> Outcome | None:
    """An incident grant is bound to one incident, one address, one agency.

    Each binding is checked separately because each is a different leak: a grant
    outliving its incident, a grant used against the building next door, and a
    grant used by an agency that was never dispatched.
    """
    grant = request.grant
    if not isinstance(grant, IncidentGrant):
        return None

    if request.incident_id is not None and not grant.covers_incident(request.incident_id):
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="incident-grant.wrong-incident",
            justification="this grant was minted for a different incident",
        )
    if request.address_id is not None and not grant.covers_address(request.address_id):
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="incident-grant.wrong-address",
            justification="this grant is bound to a different address",
        )
    if request.responding_agency_id is not None and not grant.covers_agency(
        request.responding_agency_id
    ):
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="incident-grant.wrong-agency",
            justification="this grant was issued to a different responding agency",
        )
    return None


def rule_jurisdiction(request: AccessRequest) -> Outcome | None:
    """Out-of-jurisdiction records are withheld, not hidden.

    ``WITHHOLD_JURISDICTION`` is a distinct outcome from ``DENY`` on purpose:
    the officer learns the record exists and that an agreement does not cover
    it, rather than seeing a gap they will read as "nothing there".
    """
    grant = request.grant
    if not isinstance(grant, IncidentGrant):
        return None
    target_jurisdiction = request.record_jurisdiction_id
    if target_jurisdiction is None or target_jurisdiction == grant.jurisdiction_id:
        return None

    agreement: MutualAidAgreement | None = aid_agreement_for(grant.mutual_aid_agreement_id)
    if agreement is not None and agreement.covers(request.classification):
        return Outcome(
            action=PolicyAction.ALLOW,
            rule_id="jurisdiction.covered-by-agreement",
            justification=f"shared under {agreement.agreement_id}",
            mutual_aid_agreement_id=agreement.agreement_id,
        )
    return Outcome(
        action=PolicyAction.WITHHOLD_JURISDICTION,
        rule_id="jurisdiction.not-shared",
        justification=(
            f"{request.classification} records are not shared with the responding "
            "agency under any agreement on file; the record exists and is withheld"
        ),
        # WITHHOLD_JURISDICTION must cite the agreement it applied, so a
        # placeholder id is used when there is none to cite.
        mutual_aid_agreement_id=(agreement.agreement_id if agreement else "no-agreement-on-file"),
    )


def rule_phi_is_derived_never_released(request: AccessRequest) -> Outcome | None:
    """PHI is never released raw. It is derived, or it is not available.

    There is no ``ALLOW`` for a PHI target anywhere in this policy. The most
    permissive outcome is ``DERIVE``, which runs a named function and returns a
    life-safety fact -- the raw record does not leave the adapter.
    """
    if request.classification is not Classification.PHI:
        return None
    if request.operation is not Operation.READ:
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="phi.no-writes",
            justification="person-level records are never written by this system",
        )
    if request.scope is not Scope.READ_EMS_DERIVED:
        return Outcome(
            action=PolicyAction.DENY,
            rule_id="phi.requires-derived-scope",
            justification="PHI is reachable only through the derived-read scope",
        )
    return Outcome(
        action=PolicyAction.DERIVE,
        rule_id="phi.derive-only",
        justification=(
            "a life-safety fact is derived and returned; the underlying record "
            "never leaves the adapter"
        ),
        derivation_function="derive_ems_life_safety",
    )


def rule_write_needs_approval(request: AccessRequest) -> Outcome | None:
    """Committing another agency, or cutting a utility, needs a human.

    An approval already granted is passed in as ``approval_id`` and the write
    proceeds. Absent one, the outcome is ``REQUIRE_APPROVAL`` -- which is not a
    refusal: the action is staged, prefilled, and waiting for one tap.
    """
    threshold = APPROVAL_THRESHOLDS.get(request.scope)
    if threshold is None:
        return None
    if request.approval_id is not None:
        return Outcome(
            action=PolicyAction.ALLOW,
            rule_id="approval.granted",
            justification=f"a human approved this write ({request.approval_id})",
        )
    return Outcome(
        action=PolicyAction.REQUIRE_APPROVAL,
        rule_id="approval.required",
        justification=(
            f"{request.scope} commits resources outside this agent's authority "
            "and is staged for a human decision"
        ),
        approval_threshold=threshold,
    )


def rule_allow_scoped_read(request: AccessRequest) -> Outcome | None:
    """The ordinary case: a live grant, the right scope, a permitted class."""
    if request.operation is Operation.READ:
        return Outcome(
            action=PolicyAction.ALLOW,
            rule_id="read.scope-held",
            justification=f"the grant carries {request.scope} for this classification",
        )
    return None


def rule_allow_scoped_write(request: AccessRequest) -> Outcome | None:
    """A write with a write scope that needs no approval."""
    if request.operation in (Operation.WRITE, Operation.NOTIFY):
        return Outcome(
            action=PolicyAction.ALLOW,
            rule_id="write.scope-held",
            justification=f"the grant carries {request.scope} for this target",
        )
    return None


def default_rules() -> tuple[Rule, ...]:
    """The policy, in evaluation order.

    Refusals first, then the outcomes that are not refusals, then the ordinary
    allow. Reading top to bottom is reading the policy.
    """
    return (
        rule_expired_grant,
        rule_scope_missing,
        rule_operation_matches_scope,
        rule_standing_grant_cannot_reach_people,
        rule_incident_binding,
        rule_phi_is_derived_never_released,
        rule_jurisdiction,
        rule_write_needs_approval,
        rule_allow_scoped_read,
        rule_allow_scoped_write,
    )


class PolicyEngine:
    """Evaluates access requests. Deterministic, versioned, default-deny."""

    def __init__(
        self,
        *,
        ids: IdGenerator,
        rules: Sequence[Rule] | None = None,
        policy_version: str = POLICY_VERSION,
    ) -> None:
        self._ids = ids
        self._rules = tuple(rules) if rules is not None else default_rules()
        self._policy_version = policy_version

    @property
    def policy_version(self) -> str:
        return self._policy_version

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def evaluate(self, request: AccessRequest) -> Outcome:
        """Run the rules in order. The first explicit answer wins.

        An emergency exception can promote a jurisdictional withholding to an
        allow -- an incident commander declaring a life-safety emergency. It
        cannot promote anything else, and it is audited as its own event.

        The span carries the *decision*, never the data: agent, target,
        operation, classification, action, rule. Nothing here is a field value,
        so a trace can be read by an operator who is not cleared for the
        records the decision was about.
        """
        with policy_span(
            agent_id=request.agent_id,
            target=request.target,
            operation=request.operation.value,
            classification=request.classification.value,
            policy_version=self._policy_version,
        ) as active:
            outcome = self._evaluate(request)
            active.set("policy.action", outcome.action.value)
            active.set("policy.rule_id", outcome.rule_id)
            if outcome.action is PolicyAction.DENY:
                METRICS.record_policy_denial()
            return outcome

    def _evaluate(self, request: AccessRequest) -> Outcome:
        for rule in self._rules:
            outcome = rule(request)
            if outcome is None:
                continue
            if outcome.action is PolicyAction.WITHHOLD_JURISDICTION and request.emergency_exception:
                return Outcome(
                    action=PolicyAction.ALLOW,
                    rule_id="emergency.exception-declared",
                    justification=(
                        "an incident commander declared a life-safety emergency "
                        "exception; this access is audited as an exception"
                    ),
                    mutual_aid_agreement_id=outcome.mutual_aid_agreement_id,
                )
            return outcome

        # Nothing matched. That is a policy gap, and a gap denies.
        return Outcome(
            action=PolicyAction.DENY,
            rule_id=FALLBACK_RULE_ID,
            justification="no policy rule permits this access",
        )

    def decide(self, request: AccessRequest) -> PolicyDecision:
        """Evaluate and produce the recorded decision.

        The returned record is what goes to the audit sink. It names the agent,
        the target, the operation, the classification, the action, the rule, the
        justification, the policy version, and the time -- everything an
        investigator needs to ask "why was this allowed" two years later.
        """
        outcome = self.evaluate(request)
        return PolicyDecision(
            decision_id=self._ids.new_id("decision"),
            incident_id=request.incident_id,
            agent_id=request.agent_id,
            agent_version=request.agent_version,
            grant_id=request.grant.grant_id,
            target=request.target,
            operation=request.operation,
            classification=request.classification,
            action=outcome.action,
            rule_id=outcome.rule_id,
            justification=outcome.justification,
            policy_version=self._policy_version,
            decided_at=request.now,
            derivation_function=outcome.derivation_function,
            mutual_aid_agreement_id=outcome.mutual_aid_agreement_id,
            approval_threshold=outcome.approval_threshold,
            approval_id=request.approval_id,
        )
