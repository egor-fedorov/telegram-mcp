"""Slices retain Unicode text and physical PDF provenance without large metadata."""

import pytest

from telegram_mcp.domain.extraction import ExtractedDocument, PageSpan, text_slice
from telegram_mcp.domain.models import ToolError


def test_unicode_slices_reconstruct_every_character() -> None:
    text = "😀e\u0301Привет世界\n"
    document = ExtractedDocument(format="docx", text=text)
    offset, chunks = 0, []
    while True:
        chunk, following, spans = text_slice(document, offset, 2)
        chunks.append(chunk)
        assert spans == []
        if following is None:
            break
        offset = following
    assert "".join(chunks) == text
    assert text_slice(document, len(text), 10) == ("", None, [])
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        text_slice(document, len(text) + 1, 10)


def test_short_pdf_pages_bound_spans_without_losing_text() -> None:
    document = ExtractedDocument(
        format="pdf",
        text="я" * 120,
        page_count=120,
        page_spans=[PageSpan(page_number=n + 1, start=n, end=n + 1) for n in range(120)],
    )
    first, following, spans = text_slice(document, 0, 24000)
    assert first == "я" * 50 and following == 50 and len(spans) == 50
    assert spans[-1].page_number == 50
    second, following, spans = text_slice(document, 50, 24000)
    assert len(second) == 50 and following == 100 and spans[0].start == 50
    final, following, _ = text_slice(document, 100, 24000)
    assert first + second + final == document.text and following is None


def test_page_spans_clip_to_the_returned_slice() -> None:
    document = ExtractedDocument(
        format="pdf",
        text="one\n\ntwo",
        page_count=2,
        page_spans=[
            PageSpan(page_number=1, start=0, end=5),
            PageSpan(page_number=2, start=5, end=8),
        ],
    )
    text, following, spans = text_slice(document, 2, 4)
    assert text == "e\n\nt" and following == 6
    assert [(s.page_number, s.start, s.end) for s in spans] == [(1, 2, 5), (2, 5, 6)]
