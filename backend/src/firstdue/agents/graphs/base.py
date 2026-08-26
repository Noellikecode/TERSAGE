"""The loop both slow-loop reasoning graphs run, written once.

``hazard-watcher`` and ``records-watcher`` do the same thing to different data:
read what they already have, notice what is missing or ambiguous, decide what to
ask next, ask it, and stop when the picture closes or the budget runs out. That
loop written twice is a loop that drifts -- one copy grows a step bound and the
other does not, and the one that does not is the outage. So the loop lives here
and each graph supplies only its nodes, its router, and its state.

Four properties hold for every graph built on this module.

**The model may not author a fact.** This is the project's central rule and it
survives the graph unchanged. What the graph decides is *what to look up, what
to cross-check, and when it is done*; every value that reaches a building
profile still travels the existing deterministic path -- the source record, the
extractor, the span binding, the provenance, the confidence -- and a node that
returned a ``structure.stories`` of its own would break the product rather than
extend it. A :class:`NodeResult` therefore carries a *decision* and *counts*,
and there is no field on it a value could ride in. The planner
(:class:`ReasoningPlanner`) is bounded the same way: it is handed a closed list
of option ids and may only return one of them, so a wrong answer costs a lookup
and can never cost a fact. That is also why the planner does not go through
:class:`~firstdue.ports.model.ModelClient`: the four verbs there deliberately
exclude ``decide``, and adding one would be exactly the capability that must not
exist.

**LangGraph is the executor, not the loop.** The nodes and the router are
ordinary async functions in this repository, and they are the same objects
whichever driver runs them. LangGraph compiles them into a ``StateGraph`` when
it is installed and asked for; :func:`_drive_builtin` runs the identical set in
a while loop when it is not. Fake mode takes the second path and never imports
the package -- the same lazy-import discipline ``container.py`` and
``adapters/vertex/*`` use for the Google clients -- which is what keeps ``make
demo`` credential-free and package-free. It also means the tests exercise the
real nodes rather than a stand-in for them.

**The budget is a ceiling in two dimensions, and both are hard.** Wall clock
comes from :func:`~firstdue.reliability.budget.budget_seconds` against the
agent's own catalogued ``latency_target_ms``, so the number the registry
publishes is the number the graph obeys. The step count is bounded
independently, because a graph that loops in microseconds exhausts no clock and
still never finishes. Both are checked in the *router*, so the bound is a
property of the graph rather than of whichever driver happens to be running it.

**Running out is a state, not a failure.** A graph that exhausts either ceiling
routes to its ``park`` node, which records the reason and what it was waiting
on; the agent then calls :func:`park` to open a question naming that and
everything already ruled out, and to checkpoint the position against it. That is
the whole "weeks of asynchronous operations" story: the next pass starts from
what this one learned instead of repeating the lookups this one already proved
useless. Nothing here raises on exhaustion and nothing here guesses.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Generic, Protocol, TypeAlias, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import Classification
from firstdue.domain.memory import MAX_MEMORY_TEXT
from firstdue.errors import ConfigurationError
from firstdue.observability.logging import get_logger
from firstdue.observability.tracing import agent_span, model_invoke_span
from firstdue.registry.descriptors import descriptor_for
from firstdue.reliability.budget import budget_seconds
from firstdue.services.memory_bank import MemoryBank

logger = get_logger(__name__)

#: LangGraph's own terminal node name. Spelled here as a constant so one router
#: serves both drivers: returning it means "stop" to the built-in loop and maps
#: straight onto ``langgraph.graph.END`` in the compiled graph.
STOP: Final[str] = "__end__"

#: Every graph ends here when it cannot close: checkpoint, open a question.
NODE_PARK: Final[str] = "park"

#: Steps a graph may take before the step bound stops it. Deliberately small.
#: These agents have minutes and months, not depth -- a slow-loop watcher that
#: needs two dozen lookups to settle one district has found something a person
#: should look at, which is what the open question is for.
DEFAULT_MAX_STEPS: Final[int] = 24

#: Where recorded graph traces live, beside ``model-responses``.
GRAPH_CASSETTE_DIR: Final[str] = "graph-traces"

#: How long the planner may take to choose one option. A planner that costs
#: more than the lookup it is choosing between has saved nothing.
PLANNER_DEADLINE_MS: Final[int] = 2_000

#: How long one grounding lookup may take. Longer than a planner choice --
#: it is a retrieval, not a pick from a list -- and short enough that a
#: handful of them cannot eat a two-minute pass.
GROUNDING_DEADLINE_MS: Final[int] = 6_000


class GraphStop(StrEnum):
    """Why a graph stopped. Every run ends on exactly one of these.

    ``CLOSED`` is the only one that means the work finished. The other three
    are the reasons a question gets opened, and they are kept apart because
    they call for different responses: more time, a smaller problem, or a human.
    """

    #: The picture closed. Nothing further to ask.
    CLOSED = "CLOSED"
    #: The wall-clock budget ran out mid-loop.
    OUT_OF_TIME = "OUT_OF_TIME"
    #: The step bound stopped it. Usually a loop that is not converging.
    OUT_OF_STEPS = "OUT_OF_STEPS"
    #: Every avenue was tried and the ambiguity survived all of them.
    UNRESOLVED = "UNRESOLVED"


# ------------------------------------------------------------------- state


class GraphState(BaseModel):
    """What one graph run knows, and the part of it that may be persisted.

    Mutable by design -- a node returns updates and the driver applies them --
    but ``extra="forbid"`` so a node cannot smuggle a field past the schema.
    """

    model_config = ConfigDict(extra="forbid")

    district_id: str = Field(min_length=1, max_length=120)
    correlation_id: str = Field(default="", max_length=120)

    #: Set by the router when it decides to stop, read by :meth:`park`.
    stop: GraphStop | None = None
    #: What the graph is waiting on, in the vocabulary of its own sources.
    waiting_on: str = Field(default="", max_length=200)
    #: Lookups already proved useless. Recorded faithfully, because this is the
    #: part that stops the next pass repeating this pass's failed work.
    ruled_out: tuple[str, ...] = ()

    def checkpoint_payload(self) -> dict[str, Any]:
        """The part of this state that may be written to durable memory.

        Identifiers, counts, and decisions -- never a source record and never a
        document. A checkpoint outlives the run by weeks and is read by a later
        pass, so a checkpoint carrying a citizen's inspection narrative would
        put that narrative somewhere nobody screened it into. Subclasses extend
        this; none of them widen it to records.
        """
        return {
            "district_id": self.district_id,
            "waiting_on": self.waiting_on,
            "ruled_out": bounded(self.ruled_out),
            "stop": str(self.stop) if self.stop is not None else None,
        }


StateT = TypeVar("StateT", bound=GraphState)


#: The most entries any one list on a checkpoint may carry.
#:
#: A checkpoint is a *resume hint*, not a ledger. Every list on one grows with
#: the district -- addresses settled, registries queried, dead ends ruled out --
#: and `MemoryCheckpoint` refuses a payload over
#: :data:`~firstdue.domain.memory.MAX_CHECKPOINT_STATE_BYTES`, correctly: a
#: memory must never become somewhere document contents accumulate.
#:
#: Unbounded, those two facts collide the moment a district is real. At nine
#: addresses the payload fit; at 386 `hazard-watcher` exhausted its budget,
#: tried to park, and the park *raised* -- so the pass that ran out of time also
#: lost the record of where it got to, which is the one thing parking exists to
#: keep.
#:
#: Truncating costs a repeated lookup on the next pass. Failing to park costs
#: the pass. The most recent entries are kept because they are the frontier the
#: next pass resumes from; the older ones have already done their work.
MAX_CHECKPOINT_ENTRIES: Final[int] = 200


def bounded(entries: Sequence[str]) -> list[str]:
    """The tail of a list, short enough that a checkpoint can be written."""
    return list(entries[-MAX_CHECKPOINT_ENTRIES:])


@dataclass(frozen=True, slots=True)
class NodeResult:
    """What one node did, in the only two currencies a node deals in.

    A decision and some counts. There is deliberately no field here a value
    could travel in: what a node produces is a choice about the *next lookup*,
    and the records it gathered reach the deterministic path through ``updates``
    -- which the driver applies to the state and which never reaches a span, a
    log line, or a checkpoint.
    """

    #: A short token from the node's own closed vocabulary. It reaches a span
    #: and a recorded trace, so it names an option, never a finding.
    decision: str
    #: Partial state, applied by the driver. LangGraph and the built-in loop
    #: apply it identically.
    updates: Mapping[str, Any] = field(default_factory=dict)
    #: Numbers for the trace: records seen, candidates left, sources tried.
    counts: Mapping[str, int] = field(default_factory=dict)


Node: TypeAlias = Callable[[StateT], Awaitable[NodeResult]]
Router: TypeAlias = Callable[[StateT], str]


@dataclass(frozen=True, slots=True)
class GraphSpec(Generic[StateT]):
    """A graph, as its module declares it.

    The router is a *pure function of the state* and is the only thing that
    decides where the loop goes next -- including whether the budget has run
    out. Putting that check anywhere else would make the bound depend on which
    driver was running, which is precisely the divergence this module exists to
    prevent.
    """

    state_type: type[StateT]
    entry: str
    nodes: Mapping[str, Node[StateT]]
    router: Router[StateT]


# ------------------------------------------------------------------ budget


@dataclass(slots=True)
class BudgetGuard:
    """The two ceilings, and the count of what has been spent against them.

    Elapsed time is read from a monotonic source rather than from the injected
    :class:`~firstdue.ports.clock.Clock`. The clock is deterministic on purpose
    -- a replay has to reproduce timestamps byte for byte -- and a fixed clock
    never advances, so a budget measured against it would never expire. Time
    spent is a real-world quantity here, not a recorded one. ``monotonic`` is
    injectable so a test can exhaust a budget without spending one.
    """

    seconds: float
    max_steps: int = DEFAULT_MAX_STEPS
    monotonic: Callable[[], float] = time.monotonic
    steps: int = 0
    started_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.started_at = self.monotonic()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.monotonic() - self.started_at)

    def spend(self) -> None:
        """Charge one node execution. Called by the driver, once per node."""
        self.steps += 1

    def exhausted(self) -> GraphStop | None:
        """Which ceiling has been reached, if either.

        Time first: a graph that is out of both is out of time, and that is the
        one an operator can do something about.
        """
        if self.elapsed_seconds >= self.seconds:
            return GraphStop.OUT_OF_TIME
        if self.steps >= self.max_steps:
            return GraphStop.OUT_OF_STEPS
        return None


def graph_budget(
    agent_id: str,
    *,
    deadline: datetime | None,
    started: datetime,
    max_steps: int = DEFAULT_MAX_STEPS,
    monotonic: Callable[[], float] = time.monotonic,
) -> BudgetGuard:
    """The budget one graph run gets, from the catalog rather than from a literal.

    ``latency_target_ms`` on the descriptor is a promise the registry makes
    about the agent, and :func:`~firstdue.reliability.budget.budget_seconds`
    is already the one place that arithmetic lives. A graph with a deadline of
    its own would be a second promise nobody published.
    """
    return BudgetGuard(
        seconds=budget_seconds(descriptor_for(agent_id), deadline, started),
        max_steps=max_steps,
        monotonic=monotonic,
    )


# ------------------------------------------------------------------- trace


class NodeRecord(BaseModel):
    """One node execution, as the trace remembers it.

    Node, decision, counts. The bounded ``decision`` is not politeness: this
    record is written to a cassette and to a span, and a decision long enough to
    hold a sentence of a document would be a way for one to get there.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: int = Field(ge=1)
    node: str = Field(min_length=1, max_length=60)
    decision: str = Field(min_length=1, max_length=120)
    counts: dict[str, int] = Field(default_factory=dict)


class GraphTrace(BaseModel):
    """The reasoning chain of one graph run, and the unit that replays.

    ``RecordedModelClient`` pins one model response per call, which works
    because the extractor makes a fixed number of calls in a fixed order. A
    graph does not: it makes as many lookups as the data needs, and which ones
    it makes is the interesting part. So the replayable unit here is the *whole
    chain* -- the node sequence and each node's decision -- rather than any
    single call inside it. Re-running the graph against the same sources
    re-derives this trace; a difference is a genuine behaviour change and shows
    up as :attr:`diverged_at` rather than as a quietly different demo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(min_length=1, max_length=120)
    agent_version: str = Field(min_length=1, max_length=32)
    #: Digest of the *request* -- district and sources -- so a trace can be
    #: looked up before the run that would produce it. Same construction as
    #: ``extraction.recorded.request_digest``, which it is derived with.
    request_digest: str = Field(min_length=1, max_length=64)
    records: tuple[NodeRecord, ...] = ()
    stop: GraphStop = GraphStop.CLOSED
    #: The step at which a replay stopped matching what was recorded. ``None``
    #: means the run reproduced the recorded chain exactly.
    diverged_at: int | None = Field(default=None, ge=1)

    @property
    def node_sequence(self) -> tuple[str, ...]:
        return tuple(record.node for record in self.records)

    @property
    def decisions(self) -> tuple[tuple[str, str], ...]:
        return tuple((record.node, record.decision) for record in self.records)

    def divergence_from(self, recorded: GraphTrace) -> int | None:
        """The first step where this run and a recorded one disagree.

        Compares node *and* decision, because a graph that visited the same
        nodes for different reasons has not reproduced the recorded run -- and
        that is exactly the change a cassette exists to surface.
        """
        paired = zip(self.records, recorded.records, strict=False)
        for index, (mine, theirs) in enumerate(paired, start=1):
            if (mine.node, mine.decision) != (theirs.node, theirs.decision):
                return index
        if len(self.records) != len(recorded.records):
            return min(len(self.records), len(recorded.records)) + 1
        return None


class GraphRun(BaseModel, Generic[StateT]):
    """A finished graph run: what it ended up knowing, and how it got there."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    state: StateT
    trace: GraphTrace


class GraphCassette:
    """Recorded graph traces on disk, keyed by request digest.

    The same shape as :class:`~firstdue.extraction.recorded.RecordedModelClient`
    and for the same reason: a hit means this exact request has been reasoned
    about before and the chain it produced is on file, so a change in the
    routing shows up as a diff rather than as a demo that behaves differently
    on Tuesday. A miss records, when recording is on.
    """

    def __init__(self, *, fixtures_dir: Path, record: bool = False) -> None:
        self._dir = fixtures_dir / GRAPH_CASSETTE_DIR
        self._record = record
        self.replays = 0
        self.misses = 0

    def _path(self, digest: str) -> Path:
        return self._dir / f"{digest}.json"

    def load(self, digest: str) -> GraphTrace | None:
        path = self._path(digest)
        if not path.is_file():
            self.misses += 1
            return None
        self.replays += 1
        return GraphTrace.model_validate_json(path.read_text(encoding="utf-8"))

    def store(self, trace: GraphTrace) -> None:
        if not self._record:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path(trace.request_digest).write_text(
            trace.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )


# ----------------------------------------------------------------- planner


@runtime_checkable
class ReasoningPlanner(Protocol):
    """Chooses the next lookup from a closed list of them.

    Every argument is an identifier or a count and every return value is one of
    the options it was handed, so the widest thing a planner can do wrong is
    order the work badly. It cannot invent a lookup, cannot read a document, and
    cannot produce a value -- which is why a model is allowed to be one.
    """

    async def choose(
        self,
        *,
        node: str,
        options: tuple[str, ...],
        counts: Mapping[str, int],
        deadline_ms: int,
    ) -> str | None:
        """Return one of ``options``, or ``None`` to accept the default order."""
        ...


class FixedOrderPlanner:
    """The default: take the options in the order the node listed them.

    Every graph here orders its options by cost and by how likely they are to
    settle the question, so this is a real policy rather than a stub -- and it
    is the policy fake mode runs, which is what makes the demo deterministic.
    """

    async def choose(
        self,
        *,
        node: str,
        options: tuple[str, ...],
        counts: Mapping[str, int],
        deadline_ms: int,
    ) -> str | None:
        return options[0] if options else None


class VertexReasoningPlanner:
    """Gemini, asked which of a closed list of lookups is worth doing next.

    The prompt is assembled here from the node name, the option ids, and
    integer counts, and from nothing else -- no record, no narrative, no field
    value. That is not caution about token spend: these graphs run over permit
    narratives and Tier II filings, and a planner prompt is the one place in
    this design where such text would have no screen in front of it.

    An answer that is not one of the options is discarded and the caller falls
    back to the declared order. The model is choosing which door to open, and
    the doors are the only ones there are.
    """

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        model: str,
        client: Any | None = None,
    ) -> None:
        if not project_id:
            raise ConfigurationError("the reasoning planner requires GCP_PROJECT_ID")
        if not model:
            raise ConfigurationError("the reasoning planner requires a Gemini model name")
        self._project_id = project_id
        self._location = location
        self._model_name = model
        self._client = client

    def _chat(self) -> Any:
        """Build the chat model lazily, so fake mode never imports LangChain."""
        if self._client is None:
            try:
                from langchain_google_vertexai import ChatVertexAI
            except ImportError as exc:
                raise ConfigurationError(
                    "langchain-google-vertexai is not installed; install the 'google' "
                    "extra or run with USE_FAKE_AGENTS=true",
                    details={"package": "langchain-google-vertexai"},
                ) from exc
            self._client = ChatVertexAI(
                model=self._model_name,
                project=self._project_id,
                location=self._location,
                temperature=0.0,
                max_output_tokens=16,
            )
        return self._client

    async def choose(
        self,
        *,
        node: str,
        options: tuple[str, ...],
        counts: Mapping[str, int],
        deadline_ms: int,
    ) -> str | None:
        if not options:
            return None
        prompt = (
            f"A municipal records agent is at step '{node}'. "
            f"Counts so far: {dict(sorted(counts.items()))}. "
            f"Choose exactly one of these lookups to perform next and reply with "
            f"its identifier only: {', '.join(options)}."
        )
        with model_invoke_span(
            model_ref=self._model_name,
            verb="plan",
            schema_ref="GraphChoice",
            option_count=len(options),
            deadline_ms=deadline_ms,
        ) as active:
            try:
                from langchain_core.messages import HumanMessage

                reply = await self._chat().ainvoke([HumanMessage(content=prompt)])
            except ConfigurationError:
                raise
            except Exception as exc:
                # A planner that cannot answer costs the declared order, which
                # is a complete policy. It must never cost the pass.
                active.set_rejected("planner_unavailable")
                logger.warning(
                    "graph_planner_unavailable", extra={"error_type": type(exc).__name__}
                )
                return None

            chosen = str(getattr(reply, "content", "")).strip().splitlines()[0].strip()
            for option in options:
                if chosen.lower() == option.lower():
                    active.set("graph.planner_choice", option)
                    return option
            active.set_rejected("planner_choice_not_offered")
            logger.info("graph_planner_choice_rejected", extra={"node": node})
            return None


# ------------------------------------------------------------ durable memory


async def park(
    memory: MemoryBank | None,
    *,
    agent_id: str,
    agent_version: str,
    question: str,
    classification: Classification,
    state: GraphState,
    address_id: str | None = None,
    evidence_fact_ids: tuple[str, ...] = (),
) -> str | None:
    """Open the question a stuck graph is stuck on, and checkpoint its position.

    Called by the *agent* once the graph has stopped, not by the ``park`` node
    itself. The node's job is to say why it stopped and what it was waiting on;
    a node has no clock, no memory bank, and no business holding one, and it has
    to remain runnable in a process that wired neither -- which is every process
    running today.

    Two writes, and the order is forced by the bank rather than chosen: a
    checkpoint belongs to a question, so the question is opened first and the
    position is filed against it. They answer different things anyway -- the
    question says *what a person or a later filing has to supply*, and the
    checkpoint says *where this graph was when it stopped*.

    ``ruled_out`` travels on the question, and it is the point of the whole
    exercise: without it the next pass re-walks tonight's dead ends at tonight's
    cost, which is the failure
    :mod:`firstdue.services.memory_bank` was built to end.

    Returns the question's id, or ``None`` when no bank is wired -- the default,
    and the configuration every existing test runs in.
    """
    if memory is None:
        logger.info(
            "graph_parked_without_memory",
            extra={
                "agent_id": agent_id,
                "district_id": state.district_id,
                "stop": str(state.stop),
                "ruled_out": len(state.ruled_out),
            },
        )
        return None

    opened = await memory.open(
        district_id=state.district_id,
        question=question[:MAX_MEMORY_TEXT],
        # A question with nothing to wait on is not a question. The graph
        # always sets one; this is the floor, not a default anyone relies on.
        waiting_on=(state.waiting_on or question)[:MAX_MEMORY_TEXT],
        opened_by=agent_id,
        opened_by_version=agent_version,
        classification=classification,
        address_id=address_id,
        ruled_out=state.ruled_out,
        evidence_fact_ids=evidence_fact_ids,
    )
    await memory.checkpoint(opened.question_id, agent_id=agent_id, state=state.checkpoint_payload())
    logger.info(
        "graph_parked",
        extra={
            "agent_id": agent_id,
            "district_id": state.district_id,
            "stop": str(state.stop),
            "ruled_out": len(state.ruled_out),
            "question_id": opened.question_id,
        },
    )
    return opened.question_id


# ------------------------------------------------------------------ driver


class _Recorder:
    """Collects node records as the drivers run. One per graph run."""

    __slots__ = ("records",)

    def __init__(self) -> None:
        self.records: list[NodeRecord] = []


def _instrument(
    name: str,
    node: Node[StateT],
    *,
    recorder: _Recorder,
    budget: BudgetGuard,
    agent_id: str,
    agent_version: str,
) -> Callable[[StateT], Awaitable[dict[str, Any]]]:
    """Wrap one node in its span, its step charge, and its trace record.

    The wrapper is what both drivers are handed, so a node is charged, traced,
    and recorded identically whether LangGraph or the built-in loop called it.
    Only ``decision`` and integer counts reach the span -- ``updates`` carries
    the records and never leaves the state, because
    ``observability/tracing.py`` is right that a span which never held a
    document cannot leak one.
    """

    async def run(state: StateT) -> dict[str, Any]:
        budget.spend()
        step = budget.steps
        with agent_span(
            agent_id,
            agent_version=agent_version,
            graph_node=name,
            graph_step=step,
        ) as active:
            result = await node(state)
            active.set("graph.decision", result.decision)
            for key, count in sorted(result.counts.items()):
                active.set(f"graph.{key}", int(count))
        recorder.records.append(
            NodeRecord(
                step=step,
                node=name,
                decision=result.decision,
                counts={key: int(value) for key, value in sorted(result.counts.items())},
            )
        )
        return dict(result.updates)

    return run


async def _drive_builtin(
    spec: GraphSpec[StateT],
    state: StateT,
    wrapped: Mapping[str, Callable[[StateT], Awaitable[dict[str, Any]]]],
    *,
    max_iterations: int,
) -> StateT:
    """Run the node set without LangGraph. The path fake mode takes.

    Identical semantics to the compiled graph: a node returns partial state, the
    driver merges it, the router picks the next node. ``max_iterations`` is a
    backstop against a router bug rather than the budget -- the budget is
    already enforced inside the router, and a second number that could disagree
    with it is how the two drivers would drift apart.
    """
    current = spec.entry
    for _ in range(max_iterations):
        if current == STOP:
            return state
        updates = await wrapped[current](state)
        if updates:
            state = state.model_copy(update=updates)
        current = spec.router(state)
    logger.warning(
        "graph_router_did_not_terminate",
        extra={"entry": spec.entry, "iterations": max_iterations},
    )
    return state


async def _drive_langgraph(
    spec: GraphSpec[StateT],
    state: StateT,
    wrapped: Mapping[str, Callable[[StateT], Awaitable[dict[str, Any]]]],
    *,
    max_iterations: int,
) -> StateT:
    """Compile the same node set into a LangGraph ``StateGraph`` and run it.

    Imported here and nowhere at module scope, exactly as ``container.py``
    imports the Google clients: a fake-mode process must be able to run this
    file without the package installed, and an absent package must say which
    extra to install rather than crash at import time.
    """
    try:
        from langgraph.graph import StateGraph
    except ImportError as exc:
        raise ConfigurationError(
            "langgraph is not installed; install the 'google' extra or run with "
            "USE_FAKE_AGENTS=true",
            details={"package": "langgraph"},
        ) from exc

    graph: Any = StateGraph(spec.state_type)
    for name, runner in wrapped.items():
        graph.add_node(name, runner)
    graph.set_entry_point(spec.entry)
    # One router, every node. Which node runs next is a function of the state
    # and of nothing else, so there is no static edge here to fall out of step
    # with the built-in loop.
    path_map = {name: name for name in wrapped}
    path_map[STOP] = STOP
    for name in wrapped:
        graph.add_conditional_edges(name, spec.router, path_map)

    try:
        raw = await graph.compile().ainvoke(state, config={"recursion_limit": max_iterations})
    except Exception as exc:
        if type(exc).__name__ != "GraphRecursionError":
            raise
        # The router's own step bound should have stopped this first. Reaching
        # LangGraph's limit means the two disagree, which is a defect worth a
        # log line rather than a silently truncated pass.
        logger.warning("graph_recursion_limit_reached", extra={"limit": max_iterations})
        return state
    return spec.state_type.model_validate(raw)


async def run_graph(
    spec: GraphSpec[StateT],
    state: StateT,
    *,
    agent_id: str,
    agent_version: str,
    budget: BudgetGuard,
    request_digest: str,
    use_langgraph: bool,
    recorded: GraphTrace | None = None,
) -> GraphRun[StateT]:
    """Run one graph to a stop, and return what it knew and how it got there.

    ``recorded`` turns the run into a *checked replay*: the graph runs normally
    against the same sources, and the chain it produces is compared step by step
    with the one on file. A mismatch is reported as
    :attr:`GraphTrace.diverged_at` rather than raised, because a divergence is a
    finding about the code, not a failure of the pass.
    """
    recorder = _Recorder()
    wrapped = {
        name: _instrument(
            name,
            node,
            recorder=recorder,
            budget=budget,
            agent_id=agent_id,
            agent_version=agent_version,
        )
        for name, node in spec.nodes.items()
    }
    # Two above the step bound: one for the park node the router sends an
    # exhausted graph to, and one for the terminal hop out of it.
    max_iterations = budget.max_steps + 2

    if use_langgraph:
        final = await _drive_langgraph(spec, state, wrapped, max_iterations=max_iterations)
    else:
        final = await _drive_builtin(spec, state, wrapped, max_iterations=max_iterations)

    trace = GraphTrace(
        agent_id=agent_id,
        agent_version=agent_version,
        request_digest=request_digest,
        records=tuple(recorder.records),
        stop=final.stop or GraphStop.CLOSED,
    )
    if recorded is not None:
        diverged = trace.divergence_from(recorded)
        if diverged is not None:
            logger.warning(
                "graph_trace_diverged",
                extra={"agent_id": agent_id, "step": diverged, "digest": request_digest},
            )
        trace = trace.model_copy(update={"diverged_at": diverged})

    logger.info(
        "graph_run",
        extra={
            "agent_id": agent_id,
            "district_id": final.district_id,
            "stop": str(trace.stop),
            "steps": len(trace.records),
            "executor": "langgraph" if use_langgraph else "builtin",
        },
    )
    return GraphRun[StateT](state=final, trace=trace)


__all__ = [
    "DEFAULT_MAX_STEPS",
    "GRAPH_CASSETTE_DIR",
    "GROUNDING_DEADLINE_MS",
    "NODE_PARK",
    "PLANNER_DEADLINE_MS",
    "STOP",
    "BudgetGuard",
    "FixedOrderPlanner",
    "GraphCassette",
    "GraphRun",
    "GraphSpec",
    "GraphState",
    "GraphStop",
    "GraphTrace",
    "Node",
    "NodeRecord",
    "NodeResult",
    "ReasoningPlanner",
    "Router",
    "VertexReasoningPlanner",
    "graph_budget",
    "park",
    "run_graph",
]
