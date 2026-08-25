"""Model Armor boundary, and the local detector that stands in for it.

Ingested documents are untrusted. Google's Model Armor is the live screen; in
fake mode a deterministic local detector does the same job, so the demo
exercises the path rather than skipping it.

Two properties matter more than which screen is running:

**The screen fails closed on the model, not on the fact.** If Armor is
unreachable, the document is not sent to a model at all -- but the structured
columns the source published are still extracted. A hazmat feed's *filing* does
not stop being true because a prose screen is down.

**A blocked document is still evidence.** Blocking removes the injected
instruction and keeps the rest of the narrative. Discarding the document would
lose the inspection that mentioned the truss.

**And the screen is never allowed to become the outage.** Screening is on the
911 path, inside the 90-second countdown, on an instance serving forty
concurrent requests from one event loop. So the interface is async end to end,
the one screen that makes a network call is taken off the loop, that call
carries a deadline, and a screen that cannot run returns a verdict saying so
rather than raising into a caller that is holding an incident.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.errors import ConfigurationError
from firstdue.extraction.screening import ScreenResult, screen_document
from firstdue.observability.logging import get_logger
from firstdue.observability.metrics import METRICS

logger = get_logger(__name__)

#: What a verdict reports when no screen actually ran. One token for both
#: screens on purpose: a caller deciding whether text may reach a model must not
#: have to know which screen the process was configured with.
SCREEN_UNAVAILABLE: Final[str] = "SCREEN_UNAVAILABLE"

#: How long Model Armor gets. The incident loop exists to put a brief on a
#: commander's screen inside a 90-second countdown, and the intake's own model
#: budget is 6 s -- so a screen that hangs is an outage of the exact thing it
#: was added to protect. Sent as the gRPC deadline, so the call is abandoned at
#: the far end rather than left running against a caller who has given up.
SCREEN_DEADLINE_MS: Final[int] = 2_000

#: How much wider the *await* is bounded than the RPC. Building the client --
#: credential discovery, DNS, the TLS handshake -- happens in the worker thread
#: and is not covered by the RPC deadline, so without an outer bound the first
#: document through a process could still wait forever. The margin exists so a
#: genuine RPC timeout surfaces as the SDK's own DeadlineExceeded, which names
#: the endpoint, rather than as a bare asyncio timeout that names nothing.
_WAIT_MARGIN_MS: Final[int] = 500

#: How long the one-off warm-up may take. Generous, because it runs at startup
#: with nothing waiting on it, and because failing it costs nothing -- see
#: :meth:`ModelArmorClient.warm`.
_WARM_TIMEOUT_MS: Final[int] = 15_000

#: How many screens may be in flight at once.
#:
#: Under the default `asyncio.to_thread` executor width, and low enough that a
#: call which gets a slot gets a worker immediately. Higher would just move the
#: queue into the executor, where the deadline can see it again.
_MAX_IN_FLIGHT: Final[int] = 8


class ArmorVerdict(BaseModel):
    """What the screen decided about one document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Text that may be shown to a model. Injected spans already removed.
    safe_text: str = Field(max_length=200_000)
    blocked: bool = False
    #: Pattern or filter names that fired. Never the offending text.
    findings: tuple[str, ...] = ()
    #: Which screen produced this verdict, for the audit record.
    screen: str = Field(min_length=1, max_length=60)
    #: Set when the screen itself was unavailable and the document was withheld
    #: from the model rather than passed through unscreened.
    unavailable_reason: str | None = Field(default=None, max_length=200)

    @property
    def may_reach_model(self) -> bool:
        """A document only reaches a model if a screen actually ran."""
        return self.unavailable_reason is None and bool(self.safe_text)


class LocalInjectionDetector:
    """The deterministic screen. Same detector fake mode and tests both use."""

    screen_name: Final[str] = "local-injection-detector/1"

    def __init__(self, *, unavailable: bool = False) -> None:
        #: Set to exercise the fail-closed path without a network.
        self.unavailable = unavailable
        self.calls = 0

    async def inspect(self, document_text: str | None) -> ArmorVerdict:
        """Screen one document. Async to satisfy the interface; never awaits.

        A handful of regular expressions over at most 200 KB: pure CPU, and
        fast. Handing this to a thread would cost more than the work it moved,
        because the hop *is* the expensive part. Only a screen that goes to the
        network is worth taking off the loop.
        """
        self.calls += 1
        if self.unavailable:
            # Fail closed on the model: no screen, no model call.
            return ArmorVerdict(
                safe_text="",
                blocked=False,
                screen=self.screen_name,
                unavailable_reason=SCREEN_UNAVAILABLE,
            )
        result: ScreenResult = screen_document(document_text)
        if result.blocked:
            METRICS.record_injection_block()
            logger.warning(
                "injection_blocked",
                extra={"screen": self.screen_name, "findings": ",".join(result.findings)},
            )
        return ArmorVerdict(
            safe_text=result.safe_text,
            blocked=result.blocked,
            findings=result.findings,
            screen=self.screen_name,
        )


#: Model Armor serves regional templates from a regional host. The global host
#: does not know them, so the endpoint has to be derived from the template.
_ENDPOINT_TEMPLATE: Final[str] = "modelarmor.{location}.rep.googleapis.com"


def template_api_endpoint(template: str) -> str:
    """The host that serves ``template``.

    Templates are named ``projects/{p}/locations/{loc}/templates/{t}``. A name
    that does not carry a location cannot be resolved anywhere, so this raises
    at construction rather than letting the first ingested document fail.
    """
    parts = template.split("/")
    if len(parts) < 4 or parts[2] != "locations" or not parts[3]:
        raise ConfigurationError(
            "MODEL_ARMOR_TEMPLATE must be "
            "projects/{project}/locations/{location}/templates/{template}",
            details={"template": template},
        )
    return _ENDPOINT_TEMPLATE.format(location=parts[3])


def _matched(module: Any, state: Any) -> bool:
    """True only for ``MATCH_FOUND``.

    The enum is ``UNSPECIFIED=0, NO_MATCH_FOUND=1, MATCH_FOUND=2``, so the
    obvious ``bool(state)`` is not merely imprecise -- it is inverted for the
    common case. A clean document reports ``NO_MATCH_FOUND``, which is truthy,
    so every ingested document was blocked and the slow loop would have
    recorded zero facts while reporting a screen working perfectly.

    Compared by name rather than by the literal ``2``, so a renumbering in the
    SDK cannot quietly restore the bug.
    """
    if state is None:
        return False
    return bool(state == module.FilterMatchState.MATCH_FOUND)


#: The per-filter result attributes Model Armor returns. Each filter key in
#: ``filter_results`` carries all of them; only the one it owns is populated.
_FILTER_RESULT_ATTRS: Final[tuple[str, ...]] = (
    "pi_and_jailbreak_filter_result",
    "malicious_uri_filter_result",
    "csam_filter_filter_result",
    "rai_filter_result",
    "sdp_filter_result",
)


def _matched_filters(module: Any, result: Any) -> tuple[str, ...]:
    """The filters that actually matched, not the filters that ran.

    ``filter_results`` has an entry for every configured filter whether or not
    it fired, so listing its keys reported ``csam`` against an ordinary
    building permit -- a finding in the audit log that never happened, about a
    document by a member of the public. An audit record naming a filter that
    did not match is worse than no record at all.
    """
    matched: set[str] = set()
    for name, entry in (getattr(result, "filter_results", None) or {}).items():
        for attr in _FILTER_RESULT_ATTRS:
            sub = getattr(entry, attr, None)
            if sub is not None and _matched(module, getattr(sub, "match_state", None)):
                matched.add(name)
                break
    return tuple(sorted(matched))


class ModelArmorClient:
    """The live boundary.

    Calls Model Armor's sanitize endpoint and maps the response. The local
    detector runs **as well**, not instead: two screens with different failure
    modes, and a document has to get past both. The client is imported lazily,
    so a fake-mode process never loads it.
    """

    screen_name: Final[str] = "model-armor"

    def __init__(
        self,
        *,
        template: str | None,
        project_id: str | None,
        module: Any | None = None,
        deadline_ms: int = SCREEN_DEADLINE_MS,
    ) -> None:
        """
        Args:
            template: the regional Model Armor template to sanitize against.
            project_id: the project the template lives in.
            module: the SDK module, injected. The same seam
                :class:`~firstdue.adapters.google.secrets.SecretResolver` uses,
                and for the same reason: the response mapping and the
                degradation path are the parts that were wrong in production,
                and they have to be testable without credentials.
            deadline_ms: the screen's own budget, per document.
        """
        if not template or not project_id:
            raise ConfigurationError(
                "Model Armor requires MODEL_ARMOR_TEMPLATE and GCP_PROJECT_ID",
                details={"missing": "model_armor_template"},
            )
        self._template = template
        self._project_id = project_id
        self._local = LocalInjectionDetector()
        self._module = module
        self._client: Any | None = None
        # The client is built lazily, inside the worker thread, and on a warm
        # instance every in-flight document races for it. Without the lock a
        # cold start under concurrency builds one gRPC channel per request --
        # which is the cost caching it exists to remove.
        self._client_lock = threading.Lock()
        self._deadline_ms = deadline_ms
        self._gate_semaphore: asyncio.Semaphore | None = None
        self._gate_loop: asyncio.AbstractEventLoop | None = None
        self._api_endpoint = template_api_endpoint(template)

    def _service(self) -> Any:
        """The SDK module. A missing package is a ConfigurationError, always."""
        if self._module is None:  # pragma: no cover - live mode only
            try:
                import importlib

                self._module = importlib.import_module("google.cloud.modelarmor_v1")
            except ImportError as exc:
                raise ConfigurationError(
                    "google-cloud-modelarmor is not installed; install the 'google' extra",
                    details={"package": "google-cloud-modelarmor"},
                ) from exc
        return self._module

    def _armor(self, module: Any) -> Any:
        """The gRPC client, built once per process and reused after that.

        It used to be constructed inside every ``inspect`` call, so a slow-loop
        pass over a few hundred permits opened a few hundred channels -- each
        one a credential lookup, a DNS resolution and a TLS handshake in front
        of a request that takes milliseconds.

        Lazy rather than built in ``__init__`` for two reasons: a fake-mode
        process must never touch credentials, and credential discovery blocks,
        so construction belongs in the same worker thread as the call.
        """
        with self._client_lock:
            if self._client is None:
                # Regional endpoint, derived from the template's own name. The
                # default client talks to `modelarmor.googleapis.com`, which
                # does not host regional templates -- and every Model Armor
                # template is regional, so the default can never resolve one.
                # It answers TEMPLATE_NOT_FOUND, which this screen then
                # reported as being unavailable: a permanent misconfiguration
                # wearing the costume of a transient outage.
                self._client = module.ModelArmorClient(
                    client_options={"api_endpoint": self._api_endpoint}
                )
            return self._client

    def _sanitize(self, module: Any, text: str) -> Any:
        """Everything that blocks, in one place, so what runs off-loop is plain.

        The deadline is on the RPC itself and not only on the await: cancelling
        the await releases the caller but not the thread, and a screen call left
        running against an instance serving forty requests is still an outage,
        just a quieter one.
        """
        return self._armor(module).sanitize_user_prompt(
            request={
                "name": self._template,
                "user_prompt_data": {"text": text},
            },
            timeout=self._deadline_ms / 1000,
        )

    def _unavailable(self, local: ArmorVerdict) -> ArmorVerdict:
        """An outage is a verdict here, not an exception.

        This path used to raise ``SourceUnavailableError``, and neither caller
        caught it -- so a Model Armor outage did not degrade the 911 intake, it
        took the request down in the middle of an active incident. The verdict
        is the shape the local detector's fail-closed path produces, so both
        callers reach the degradation branch they already document: no model
        call, nothing reported, and the brief already on the commander's screen
        left exactly as it was.

        A :class:`~firstdue.errors.ConfigurationError` is deliberately *not*
        routed through here. A missing package or an unregioned template is
        permanent, and an operator told it was an outage retries it forever.
        """
        return ArmorVerdict(
            safe_text="",
            # What the local screen found still stands: it ran. Carried so an
            # injection attempt does not fall out of the audit record merely
            # because the second screen happened to be down when it arrived.
            blocked=local.blocked,
            findings=local.findings,
            screen=self.screen_name,
            unavailable_reason=SCREEN_UNAVAILABLE,
        )

    def _gate(self) -> asyncio.Semaphore:
        """In-flight screens, bounded, and bound to the running loop.

        Rebuilt when the loop changes rather than cached once: this client is
        deliberately loop-agnostic -- the class docstring explains why it uses
        the synchronous SDK -- and a semaphore created on the CLI's loop and
        reused on the server's would raise on first use.

        Sized to sit under the default thread-pool width, so a permitted call
        finds a worker rather than queueing behind one.
        """
        loop = asyncio.get_running_loop()
        if self._gate_loop is not loop or self._gate_semaphore is None:
            self._gate_semaphore = asyncio.Semaphore(_MAX_IN_FLIGHT)
            self._gate_loop = loop
        return self._gate_semaphore

    async def warm(self) -> bool:
        """Establish the gRPC channel before any document depends on it.

        Measured against the live service: a cold ``sanitize`` costs ~1.1 s, of
        which almost all is channel setup and TLS, while a warm one costs
        ~170 ms. Charged to the first document, that cold start eats more than
        half a 2-second budget -- and under the slow loop's concurrency it was
        pushing calls past the deadline, which fail-closes and withholds the
        document from the model. A district ingested nothing and every log line
        said the screen was unavailable, when the screen was fine and merely
        cold.

        So the cost is paid once, at startup, off any document's clock. This is
        deliberately generous with time and deliberately cannot fail: a warm-up
        that raised would turn a slow network at boot into a dead process, and
        the screen is still perfectly capable of doing its job cold.

        Returns whether the channel came up, for the log line. Never raises.
        """
        try:
            module = self._service()
            await asyncio.wait_for(
                asyncio.to_thread(self._sanitize, module, "warm"),
                timeout=_WARM_TIMEOUT_MS / 1000,
            )
        except Exception as exc:
            logger.info("model_armor_warm_skipped", extra={"error_type": type(exc).__name__})
            return False
        logger.info("model_armor_warm", extra={"endpoint": self._api_endpoint})
        return True

    async def inspect(self, document_text: str | None) -> ArmorVerdict:
        """Screen one document through both screens, without holding the loop.

        The sanitize call is a blocking gRPC round trip. Awaited directly it
        stalled the event loop for *every* concurrent request on the instance,
        during the countdown the whole incident loop is built to protect, so it
        runs in a worker thread.

        A thread and the synchronous client rather than the SDK's
        ``ModelArmorAsyncClient``: a grpc.aio channel binds to the loop that
        created it, and a client cached for the life of the process outlives
        any loop that is not the server's -- the CLI's, a test's -- after which
        every call fails. Failing now means silently degrading, which is the
        one failure mode a security screen must not have. The synchronous
        client is thread-safe and loop-agnostic, and it is the same shape the
        Vertex adapters already use.
        """
        # The gate wraps *both* screens, not just the remote one.
        #
        # The local detector is pure CPU and deliberately runs on the loop, which
        # is right per document and wrong in aggregate: a district's worth of
        # documents screened at once saturates the loop with regex work, so the
        # `wait_for` timers below fire late and cancel Model Armor calls that
        # would have returned. Measured -- 200 concurrent documents lost 22 to a
        # service answering in 170 ms. Bounding admission to the whole screen
        # keeps the loop responsive enough for its own timers to mean anything.
        async with self._gate():
            return await self._screen(document_text)

    async def _screen(self, document_text: str | None) -> ArmorVerdict:
        local = await self._local.inspect(document_text)
        if not local.safe_text:
            return local
        # Resolved *outside* the try below on purpose. A missing package is a
        # configuration error and must stay one: swallowing it into an
        # unavailable verdict tells an operator the screen is having an outage,
        # which a circuit breaker retries forever, when the truth is that
        # nobody installed it and no amount of retrying will help.
        module = self._service()
        # The deadline bounds the *call*, not the wait for a thread to make it
        # in. `asyncio.to_thread` shares one bounded executor, so screening a
        # district's worth of documents at once left most of them queued -- and
        # the timer, started before the queue, counted that wait as the screen
        # taking too long. Documents that had not begun to be screened were
        # recorded as screen timeouts and withheld, which fail-closes: an ingest
        # of several hundred permits wrote nothing and every log line blamed an
        # outage at a service that was answering in 170 ms.
        #
        # So admission is gated first and timed second. Queueing is now bounded
        # by how many screens may be in flight, and a slow loop with more
        # documents than slots waits instead of failing.
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._sanitize, module, local.safe_text),
                timeout=(self._deadline_ms + _WAIT_MARGIN_MS) / 1000,
            )
        except Exception as exc:
            logger.warning("model_armor_unavailable", extra={"error_type": type(exc).__name__})
            return self._unavailable(local)

        result = getattr(response, "sanitization_result", None)
        blocked = _matched(module, getattr(result, "filter_match_state", None))
        if blocked and not local.blocked:
            # Only the blocks the local detector did not already count.
            # :meth:`LocalInjectionDetector.inspect` records its own, and it has
            # already run by this point -- so counting ``blocked or
            # local.blocked`` here reported two blocks for one document whenever
            # both screens fired, which is every document the local detector
            # catches while Model Armor is configured. The metric answers "how
            # many documents were blocked", not "how many screens objected".
            METRICS.record_injection_block()
        findings = _matched_filters(module, result)
        return ArmorVerdict(
            safe_text="" if blocked else local.safe_text,
            blocked=blocked or local.blocked,
            findings=tuple(sorted({*local.findings, *findings})),
            screen=self.screen_name,
        )


@runtime_checkable
class DocumentScreen(Protocol):
    """Anything that can screen an ingested document.

    Two implementations, and which one a process holds is a deployment
    decision: :class:`LocalInjectionDetector` in fake mode,
    :class:`ModelArmorClient` in live mode -- and the latter runs the local one
    *as well*, because two screens with different failure modes are what a
    document has to get past.

    ``inspect`` is async for the sake of the implementation that talks to the
    network, not the one that does not. Both callers are already coroutines on
    the incident loop; a synchronous screen there means one of them will
    eventually block it, and the interface is the only place that can rule that
    out for every screen written later.
    """

    @property
    def screen_name(self) -> str: ...

    async def inspect(self, document_text: str | None) -> ArmorVerdict: ...


def build_screen(
    *, use_fake: bool, template: str | None = None, project_id: str | None = None
) -> LocalInjectionDetector | ModelArmorClient:
    """Choose the screen. Fake mode gets the deterministic one, and says so."""
    if use_fake:
        return LocalInjectionDetector()
    return ModelArmorClient(template=template, project_id=project_id)
