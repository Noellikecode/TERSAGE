"""Screening untrusted documents.

An inspection narrative is a citizen-authored document that arrives over a
public API and is handed to a model. That is the exact shape of a prompt
injection, and the fixture set contains one on purpose so the demo exercises
this path rather than only the tests.

Two defences, because either alone fails:

* **Detection.** Known injection shapes are recognised, recorded, and the
  document is marked. A recognised injection does not stop extraction -- the
  rest of the narrative is still evidence -- but it is stripped from what the
  model sees and reported to the audit log.
* **Framing.** Screened text is passed as *data*, never as instruction. The
  model contract has no verb that could act on an instruction anyway: it may
  extract, compose, and explain, and nothing else.

The screen is deliberately not clever. It looks for the handful of shapes that
actually appear, reports what it found by pattern id, and never tries to decide
whether the author *meant* it -- that would be a judgement, and a judgement here
would be a model deciding whether to trust its own input.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: Named patterns, so an audit record can say which one fired without quoting
#: the document. Order is fixed for deterministic reporting.
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "instruction-override",
        re.compile(
            r"\b(ignore|disregard|forget)\b[^.]{0,40}\b(previous|prior|above|all)\b"
            r"[^.]{0,40}\b(instruction|prompt|rule)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role-reassignment",
        re.compile(r"\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be)\b", re.IGNORECASE),
    ),
    (
        "directive-to-assert",
        re.compile(
            r"\b(mark|record|report|set)\b[^.]{0,30}\b(as|to)\b[^.]{0,30}"
            r"\b(sprinklered|safe|compliant|clear|no\s+hazard)\b",
            re.IGNORECASE,
        ),
    ),
    ("system-prompt-mimicry", re.compile(r"(^|\n)\s*(system|assistant|user)\s*:", re.IGNORECASE)),
    ("fenced-directive", re.compile(r"<\s*/?\s*(system|instructions?|prompt)\s*>", re.IGNORECASE)),
)

REDACTION: Final[str] = "[SCREENED]"


class ScreenResult(BaseModel):
    """What the screen found, and the text that is safe to extract from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The document with recognised injection spans replaced. Never empty when
    #: the input was non-empty -- the rest of the narrative is still evidence.
    safe_text: str = Field(max_length=200_000)
    #: Pattern ids that fired. Names only; never the offending text.
    findings: tuple[str, ...] = ()
    #: Character count removed, so the audit record can show the scale.
    removed_chars: int = Field(default=0, ge=0)

    @property
    def blocked(self) -> bool:
        """True when the document contained something that tried to instruct."""
        return bool(self.findings)


def screen_document(text: str | None) -> ScreenResult:
    """Screen one untrusted document.

    Returns a result whose ``safe_text`` is what may be shown to a model. The
    original is never mutated and never discarded -- it stays in the source
    record, which is where an investigator would look.
    """
    if not text:
        return ScreenResult(safe_text="")

    findings: list[str] = []
    screened = text
    for name, pattern in _PATTERNS:
        screened, count = pattern.subn(REDACTION, screened)
        if count:
            findings.append(name)

    removed = max(0, len(text) - len(screened) + len(REDACTION) * len(findings))
    return ScreenResult(
        safe_text=screened,
        findings=tuple(findings),
        removed_chars=removed if findings else 0,
    )
