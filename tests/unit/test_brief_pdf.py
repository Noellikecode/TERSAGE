"""The hand-rolled PDF writer.

No reader library here on purpose: these tests check the bytes against the
format's own rules -- the header, the object table, the cross-reference offsets
and the trailer -- which is the same thing a reader does and does not add a
dependency to check a module that exists to avoid one.
"""

from __future__ import annotations

import re

import pytest

from firstdue.incident.pdf import PAGE_HEIGHT, PAGE_WIDTH, PdfBlock, render_pdf


def _xref_offsets(pdf: bytes) -> list[int]:
    """Every offset in the cross-reference table, in object order."""
    # ``\nxref\n`` rather than ``xref\n``: the trailer's own ``startxref`` line
    # contains the shorter string and comes after the table.
    start = pdf.rindex(b"\nxref\n")
    table = pdf[start : pdf.index(b"trailer", start)]
    return [
        int(match.group(1))
        for match in re.finditer(rb"^(\d{10}) 00000 n $", table, flags=re.MULTILINE)
    ]


def _startxref(pdf: bytes) -> int:
    return int(re.search(rb"startxref\n(\d+)\n%%EOF", pdf).group(1))  # type: ignore[union-attr]


@pytest.mark.invariant
def test_the_file_has_a_header_a_trailer_and_a_working_cross_reference_table() -> None:
    """A reader locates every object through this table. Offsets have to be real."""
    pdf = render_pdf([PdfBlock(style="title", text="Crew brief"), PdfBlock(text="One line.")])
    assert pdf.startswith(b"%PDF-1.4\n")
    assert pdf.rstrip().endswith(b"%%EOF")

    # startxref points at the table itself.
    assert pdf[_startxref(pdf) :].startswith(b"xref\n")

    # And every offset in the table points at the object that claims that number.
    offsets = _xref_offsets(pdf)
    assert offsets
    for number, offset in enumerate(offsets, start=1):
        assert pdf[offset:].startswith(f"{number} 0 obj".encode())


def test_the_object_graph_is_a_catalog_a_page_tree_and_two_fonts() -> None:
    pdf = render_pdf([PdfBlock(text="One line.")])
    assert b"/Type /Catalog" in pdf
    assert b"/Type /Pages" in pdf
    assert b"/BaseFont /Helvetica" in pdf
    assert b"/BaseFont /Helvetica-Bold" in pdf
    assert f"/MediaBox [0 0 {PAGE_WIDTH:.0f} {PAGE_HEIGHT:.0f}]".encode() in pdf
    assert b"/Count 1" in pdf


def test_a_long_document_breaks_onto_further_pages() -> None:
    pdf = render_pdf([PdfBlock(text=f"Line {index} of the brief.") for index in range(400)])
    count = int(re.search(rb"/Count (\d+)", pdf).group(1))  # type: ignore[union-attr]
    assert count > 1
    assert pdf.count(b"/Type /Page\n") == 0  # pages are written on one line
    assert pdf.count(b"/Type /Page ") == count
    # The cross-reference table grew with them, and still resolves.
    for number, offset in enumerate(_xref_offsets(pdf), start=1):
        assert pdf[offset:].startswith(f"{number} 0 obj".encode())


@pytest.mark.invariant
def test_parentheses_and_backslashes_are_escaped_rather_than_closing_a_string() -> None:
    """An unescaped bracket ends the string early and corrupts the rest of the page.

    Node ids and cost explanations are full of them -- ``(24.4 m x 7.91)`` is an
    ordinary line on this artifact -- so this is the failure mode that would
    actually happen.
    """
    pdf = render_pdf([PdfBlock(text=r"cost (24.4 m x 7.91) via C:\face\ALPHA")])
    assert rb"\(24.4" in pdf
    assert rb"7.91\)" in pdf
    assert rb"C:\\face\\ALPHA" in pdf


def test_a_character_the_base_fonts_cannot_render_becomes_a_question_mark() -> None:
    """Never a raw byte the reader would draw as something else."""
    pdf = render_pdf([PdfBlock(text="peak 42 \u2103 on \u30a2")])
    assert b"?" in pdf
    assert "\u30a2".encode() not in pdf


def test_a_word_longer_than_a_line_is_broken_rather_than_run_off_the_page() -> None:
    """A content hash that cannot be read back is not evidence of anything."""
    pdf = render_pdf([PdfBlock(text="a" * 400)])
    assert b"-) Tj" in pdf


def test_the_title_becomes_the_document_info_entry() -> None:
    pdf = render_pdf([PdfBlock(text="body")], title="Crew brief pkg-1")
    assert b"/Title (Crew brief pkg-1)" in pdf
    assert b"/Info " in pdf


def test_the_same_blocks_render_the_same_bytes() -> None:
    blocks = [PdfBlock(style="heading", text="Readiness"), PdfBlock(text="NOT READY", indent=12.0)]
    assert render_pdf(blocks, title="x") == render_pdf(blocks, title="x")


def test_an_indent_moves_the_pen_and_nothing_else() -> None:
    plain = render_pdf([PdfBlock(text="line")])
    indented = render_pdf([PdfBlock(text="line", indent=18.0)])
    assert b"1 0 0 1 54.0 " in plain
    assert b"1 0 0 1 72.0 " in indented
