"""A PDF writer, hand-rolled, for the same reason the PNG encoder is.

Fake mode is the default and the whole test suite, so downloading a brief has to
work with no credentials and no wheel nobody audited. A PDF that carries text in
one of the fourteen fonts every reader already has is a header, a handful of
objects, a cross-reference table and a trailer -- about the same size as the
scanline encoder in ``adapters/fake/tiles.py`` and with the same argument behind
it: a dependency added to the credential-free path is a dependency the whole
system now has.

**What this writes, and what it does not.** Text in Helvetica and
Helvetica-Bold, laid out in a single column with automatic page breaks. No
images, no embedded fonts, no compression, no encryption, no forms. The output
is uncompressed on purpose: a brief is a few kilobytes either way, and a
readable object stream is a file an investigator can open in a text editor and
check against the incident log.

**Line breaking is estimated, and the estimate is deliberately generous.**
Helvetica is proportional and the real advance widths live in a table this
module does not carry, so wrapping uses a per-character average biased *upward*
-- lines come out slightly short of the margin rather than slightly over it. A
brief with a ragged right edge is fine; one whose last word is off the page is
not.

**Only Latin-1 survives.** WinAnsiEncoding is what the base fonts carry, so a
character outside it is written as ``?`` rather than as a byte the reader will
render as something else. Nothing in a brief this system produces is outside it;
the substitution exists so a stray character cannot corrupt a whole file.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: US Letter in PostScript points, which is the unit a PDF page is measured in.
PAGE_WIDTH: Final[float] = 612.0
PAGE_HEIGHT: Final[float] = 792.0
MARGIN: Final[float] = 54.0

#: Point sizes and the leading each one prints at.
TITLE_SIZE: Final[float] = 16.0
HEADING_SIZE: Final[float] = 11.0
BODY_SIZE: Final[float] = 9.0
LEADING_RATIO: Final[float] = 1.35

#: Average glyph advance as a fraction of the point size, biased high. Helvetica
#: lowercase runs nearer 0.50; 0.56 buys a margin of error so a long line stops
#: short of the edge instead of past it.
AVERAGE_ADVANCE: Final[float] = 0.56

_FONT_REGULAR: Final[str] = "F1"
_FONT_BOLD: Final[str] = "F2"


class PdfBlock(BaseModel):
    """One paragraph, and which of the three styles it prints in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``title``, ``heading`` or ``body``.
    style: str = Field(default="body", max_length=10)
    text: str = Field(default="", max_length=20_000)
    #: Extra indent in points. Used for the bulleted claim lines.
    indent: float = Field(default=0.0, ge=0.0, le=200.0)


def _latin1(text: str) -> str:
    """Everything the base fonts can render; anything else becomes a question mark."""
    return text.encode("latin-1", "replace").decode("latin-1")


def _escape(text: str) -> bytes:
    """PDF string literal escaping: backslash first, then the parentheses.

    Order matters. Escaping the parentheses first would then have their own
    backslashes escaped by the second pass, and the reader would print them.
    """
    escaped = _latin1(text).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1")


def _wrap(text: str, *, size: float, width: float) -> list[str]:
    """Greedy word wrap against the estimated advance width.

    A word longer than the whole line -- a node id, a content hash -- is broken
    at the character rather than allowed to run off the page, because a hash
    that cannot be read back is not evidence of anything.
    """
    limit = max(1, int(width / (size * AVERAGE_ADVANCE)))
    lines: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > limit:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[: limit - 1] + "-")
            word = word[limit - 1 :]
        candidate = f"{current} {word}".strip()
        if len(candidate) > limit and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _style(style: str) -> tuple[str, float]:
    if style == "title":
        return _FONT_BOLD, TITLE_SIZE
    if style == "heading":
        return _FONT_BOLD, HEADING_SIZE
    return _FONT_REGULAR, BODY_SIZE


class _PlacedLine(BaseModel):
    """One line of text with the page position it prints at, in points."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    font: str
    size: float
    x: float
    y: float
    text: str


def _paginate(blocks: Sequence[PdfBlock]) -> list[list[_PlacedLine]]:
    """Lay every block out into pages of positioned lines.

    Position is resolved here and only here, so the drawing step below has no
    arithmetic of its own to get subtly out of step with. One pass, and no
    widows-and-orphans handling: a heading that lands at the foot of a page
    stays there. A rule that moved a heading would have to move the lines under
    it too, and that is where a layout engine starts.
    """
    usable = PAGE_WIDTH - 2 * MARGIN
    bottom = MARGIN + BODY_SIZE
    pages: list[list[_PlacedLine]] = []
    current: list[_PlacedLine] = []
    cursor = PAGE_HEIGHT - MARGIN

    for block in blocks:
        font, size = _style(block.style)
        leading = size * LEADING_RATIO
        text = block.text.strip()
        lines = _wrap(text, size=size, width=usable - block.indent) if text else [""]
        for line in lines:
            if cursor - leading < bottom:
                pages.append(current)
                current = []
                cursor = PAGE_HEIGHT - MARGIN
            cursor -= leading
            if line:
                current.append(
                    _PlacedLine(font=font, size=size, x=MARGIN + block.indent, y=cursor, text=line)
                )
        # Breathing room after every block. Absorbed silently at a page break,
        # so a page never opens with an empty first row.
        cursor -= leading * 0.4

    pages.append(current)
    return pages


def _content_stream(lines: Sequence[_PlacedLine]) -> bytes:
    """One page's drawing operations.

    ``BT``/``ET`` bracket a text object; ``Tf`` selects a font and size, ``Tm``
    sets the text matrix -- an absolute pen position -- and ``Tj`` shows a
    string. Absolute positioning rather than ``TD``'s relative moves, because
    the leading changes with the style and a relative pen can only drift.
    """
    parts: list[bytes] = [b"BT"]
    for line in lines:
        parts.append(f"/{line.font} {line.size:.1f} Tf".encode("latin-1"))
        parts.append(f"1 0 0 1 {line.x:.1f} {line.y:.1f} Tm".encode("latin-1"))
        parts.append(b"(" + _escape(line.text) + b") Tj")
    parts.append(b"ET")
    return b"\n".join(parts)


def render_pdf(blocks: Sequence[PdfBlock], *, title: str = "") -> bytes:
    """One PDF, complete with its cross-reference table.

    The object graph is the smallest one a conforming reader accepts: a catalog
    pointing at a page tree, two font dictionaries shared by every page, and a
    page plus a content stream for each sheet. Offsets for the xref table are
    measured as the file is assembled, because they are byte positions into the
    finished file and there is no way to know them in advance.
    """
    pages = _paginate(blocks)
    page_count = len(pages)

    #: 1 catalog, 2 page tree, 3 and 4 the fonts, then a page and a content
    #: object per sheet -- interleaved, so page N is object 5 + 2(N-1).
    first_page_obj = 5
    kids = " ".join(f"{first_page_obj + 2 * i} 0 R" for i in range(page_count))

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("latin-1"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>",
    ]

    for index, page in enumerate(pages):
        content_obj = first_page_obj + 2 * index + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R "
                f"/MediaBox [0 0 {PAGE_WIDTH:.0f} {PAGE_HEIGHT:.0f}] "
                f"/Resources << /Font << /{_FONT_REGULAR} 3 0 R /{_FONT_BOLD} 4 0 R >> >> "
                f"/Contents {content_obj} 0 R >>"
            ).encode("latin-1")
        )
        stream = _content_stream(page)
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
        )

    # The document title, as the reader shows it in its window bar. One more
    # object rather than a field on the page, because that is where the format
    # keeps it and a title only on page one is a title nothing can index.
    info_ref = ""
    if title:
        objects.append(b"<< /Title (" + _escape(title) + b") >>")
        info_ref = f" /Info {len(objects)} 0 R"

    out = bytearray(b"%PDF-1.4\n")
    # A comment of high bytes, which is what tells a transport that reads the
    # first line that this file is binary rather than text.
    out += b"%\xe2\xe3\xcf\xd3\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    # Entry zero is the head of the free list and is fixed by the specification.
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")

    trailer = f"<< /Size {len(objects) + 1} /Root 1 0 R{info_ref} >>"
    out += f"trailer\n{trailer}\nstartxref\n{xref_at}\n%%EOF\n".encode("latin-1")
    return bytes(out)


__all__ = [
    "AVERAGE_ADVANCE",
    "MARGIN",
    "PAGE_HEIGHT",
    "PAGE_WIDTH",
    "PdfBlock",
    "render_pdf",
]
