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
        try:
            module = self._service()
            client = module.ModelArmorClient()
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
        blocked = bool(getattr(result, "filter_match_state", 0)) if result else False
        if blocked or local.blocked:
            METRICS.record_injection_block()
        findings = tuple(sorted(getattr(result, "filter_results", {}).keys())) if result else ()
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
