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
"""

from __future__ import annotations

from typing import Any, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.errors import ConfigurationError, SourceUnavailableError
from firstdue.extraction.screening import ScreenResult, screen_document
from firstdue.observability.logging import get_logger
from firstdue.observability.metrics import METRICS

logger = get_logger(__name__)


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

    def inspect(self, document_text: str | None) -> ArmorVerdict:
        self.calls += 1
        if self.unavailable:
            # Fail closed on the model: no screen, no model call.
            return ArmorVerdict(
                safe_text="",
                blocked=False,
                screen=self.screen_name,
                unavailable_reason="SCREEN_UNAVAILABLE",
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

    def __init__(self, *, template: str | None, project_id: str | None) -> None:
        if not template or not project_id:
            raise ConfigurationError(
                "Model Armor requires MODEL_ARMOR_TEMPLATE and GCP_PROJECT_ID",
                details={"missing": "model_armor_template"},
            )
        self._template = template
        self._project_id = project_id
        self._local = LocalInjectionDetector()
        self._client: Any | None = None
        self._api_endpoint = template_api_endpoint(template)

    def _service(self) -> Any:  # pragma: no cover - live mode only
        if self._client is None:
            try:
                import importlib

                self._client = importlib.import_module("google.cloud.modelarmor_v1")
            except ImportError as exc:
                raise ConfigurationError(
                    "google-cloud-modelarmor is not installed; install the 'google' extra",
                    details={"package": "google-cloud-modelarmor"},
                ) from exc
        return self._client

    def inspect(self, document_text: str | None) -> ArmorVerdict:  # pragma: no cover - live only
        local = self._local.inspect(document_text)
        if not local.safe_text:
            return local
        # Resolved *outside* the try below on purpose. A missing package is a
        # configuration error and must stay one: swallowing it into
        # SourceUnavailableError told an operator the screen was having an
        # outage, which a circuit breaker retries forever, when the truth was
        # that nobody installed it and no amount of retrying would help.
        module = self._service()
        try:
            # Regional endpoint, derived from the template's own name. The
            # default client talks to `modelarmor.googleapis.com`, which does
            # not host regional templates -- and every Model Armor template is
            # regional, so the default can never resolve one. It answers
            # TEMPLATE_NOT_FOUND, which this method then reported as the screen
            # being unavailable: a permanent misconfiguration wearing the
            # costume of a transient outage.
            client = module.ModelArmorClient(client_options={"api_endpoint": self._api_endpoint})
            response = client.sanitize_user_prompt(
                request={
                    "name": self._template,
                    "user_prompt_data": {"text": local.safe_text},
                }
            )
        except Exception as exc:
            logger.warning("model_armor_unavailable", extra={"error_type": type(exc).__name__})
            raise SourceUnavailableError(
                "the document screen is unavailable; the document was not sent to a model",
                details={"screen": self.screen_name},
            ) from exc

        result = getattr(response, "sanitization_result", None)
        blocked = _matched(module, getattr(result, "filter_match_state", None))
        if blocked or local.blocked:
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
    """

    @property
    def screen_name(self) -> str: ...

    def inspect(self, document_text: str | None) -> ArmorVerdict: ...


def build_screen(
    *, use_fake: bool, template: str | None = None, project_id: str | None = None
) -> LocalInjectionDetector | ModelArmorClient:
    """Choose the screen. Fake mode gets the deterministic one, and says so."""
    if use_fake:
        return LocalInjectionDetector()
    return ModelArmorClient(template=template, project_id=project_id)
