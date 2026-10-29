"""Real parser behavior on synthetic files, including unsupported content."""

import io
import zipfile
from types import SimpleNamespace

import pytest

from telegram_mcp.adapters.extraction import parsers
from telegram_mcp.domain.models import ToolError
from tests.support.extraction import (
    damaged_pdf_bytes,
    docx_bytes,
    pdf_bytes,
    relocated_docx_bytes,
)


def test_pdf_text_and_physical_pages_survive_blank_pages() -> None:
    result = parsers.extract_document(pdf_bytes(["Café", None, "Last page"]))
    assert result.page_count == 3 and result.text == "Café\n\n\n\nLast page"
    assert [span.page_number for span in result.page_spans] == [1, 2, 3]
    assert result.issues[0].code == "DOCUMENT_PAGES_WITHOUT_TEXT"
    assert "preview: 2" in result.issues[0].message


@pytest.mark.parametrize("pages", [[], [None], [None, None]])
def test_no_text_does_not_claim_the_file_is_a_scan(pages: list) -> None:
    with pytest.raises(ToolError, match="DOCUMENT_NO_TEXT"):
        parsers.extract_document(pdf_bytes(pages))


def test_encrypted_pdf_does_not_accept_a_password() -> None:
    with pytest.raises(ToolError, match="DOCUMENT_ENCRYPTED"):
        parsers.extract_document(pdf_bytes(["secret"], encrypted=True))


def test_page_and_text_budgets_publish_no_partial_document(monkeypatch) -> None:
    monkeypatch.setattr(parsers, "MAX_PDF_PAGES", 1)
    with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED"):
        parsers.extract_document(pdf_bytes(["one", "two"]))
    monkeypatch.setattr(parsers, "MAX_TEXT_CHARACTERS", 3)
    with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED"):
        parsers.extract_document(docx_bytes("four"))


def test_docx_unicode_is_read_without_page_numbers() -> None:
    result = parsers.extract_document(docx_bytes())
    assert result.text == "Résumé 😀\nПривет 世界\n"
    assert result.format == "docx" and result.page_count is None and result.page_spans == []


def test_docx_table_cells_use_tabs_and_rows_use_newlines() -> None:
    from docx import Document

    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    for cell, text in zip((cell for row in table.rows for cell in row.cells), "ABCD", strict=True):
        cell.text = text
    output = io.BytesIO()
    doc.save(output)
    assert parsers.extract_document(output.getvalue()).text == "A\tB\nC\tD\n"


def test_docx_preserves_tables_links_and_omission_notice() -> None:
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = Document()
    paragraph = doc.add_paragraph("Before ")
    link = OxmlElement("w:hyperlink")
    link.set(
        qn("r:id"),
        doc.part.relate_to("https://example.invalid/no-fetch", RT.HYPERLINK, is_external=True),
    )
    run, text = OxmlElement("w:r"), OxmlElement("w:t")
    text.text = "visible link"
    run.append(text)
    link.append(run)
    paragraph._p.append(link)
    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Horizontal merge"
    table.cell(1, 0).merge(table.cell(2, 0)).text = "Vertical merge"
    nested = table.cell(1, 1).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "Nested"
    doc.add_paragraph("After")
    doc.sections[0].header.paragraphs[0].text = "Omitted header"
    output = io.BytesIO()
    doc.save(output)
    result = parsers.extract_document(output.getvalue())
    assert (
        result.text.index("Before visible link")
        < result.text.index("Nested")
        < result.text.index("After")
    )
    assert result.text.count("Horizontal merge") == result.text.count("Vertical merge") == 1
    assert "Omitted header" not in result.text
    assert [issue.code for issue in result.issues] == ["DOCUMENT_CONTENT_OMITTED"]


def test_docx_expansion_budget_is_checked_before_loading(monkeypatch) -> None:
    data = docx_bytes("a" * 1000)
    monkeypatch.setattr(parsers, "MAX_DOCX_BYTES", 100)
    with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED"):
        parsers.extract_document(data)


def test_damaged_docx_xml_returns_a_sanitized_document_error() -> None:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(docx_bytes())) as original,
        zipfile.ZipFile(output, "w") as damaged,
    ):
        for name in original.namelist():
            data = (
                b"<unclosed>private-canary" if name == "word/document.xml" else original.read(name)
            )
            damaged.writestr(name, data)
    with pytest.raises(ToolError, match="DOCUMENT_INVALID") as caught:
        parsers.extract_document(output.getvalue())
    assert "private-canary" not in caught.value.detail


def test_an_arbitrary_zip_is_not_a_docx() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("notes.txt", "text")
    with pytest.raises(ToolError, match="DOCUMENT_UNSUPPORTED"):
        parsers.extract_document(output.getvalue())


@pytest.mark.parametrize("data", [b"plain text", bytes.fromhex("d0cf11e0a1b11ae1") + b"Office"])
def test_unsupported_content_is_distinct_from_damage(data: bytes) -> None:
    with pytest.raises(ToolError, match="DOCUMENT_UNSUPPORTED"):
        parsers.extract_document(data)


@pytest.mark.parametrize("defect", ["xref", "length", "prefix"])
def test_pdf_recovery_preserves_text_pages_and_existing_issues(defect: str) -> None:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    data = damaged_pdf_bytes(defect)
    with pytest.raises(PdfReadError):
        reader = PdfReader(io.BytesIO(data), strict=True)
        for page in reader.pages:
            page.extract_text()
    expected = parsers.extract_document(pdf_bytes(["First", None, "Last"]))
    recovered = parsers.extract_document(data)
    assert recovered.text == expected.text
    assert recovered.page_count == expected.page_count
    assert recovered.page_spans == expected.page_spans
    assert recovered.issues[:-1] == expected.issues
    assert recovered.issues[-1].code == "DOCUMENT_RECOVERED"
    assert "DOCUMENT_RECOVERED" not in {issue.code for issue in expected.issues}


def test_pdf_offsets_already_accepted_by_strict_reader_keep_their_text() -> None:
    result = parsers.extract_document(damaged_pdf_bytes("offsets"))
    assert result == parsers.extract_document(pdf_bytes(["First", None, "Last"]))


@pytest.mark.parametrize("prefix_size", [1019, 1020, 1024])
def test_pdf_signature_must_fit_in_the_first_1024_bytes(prefix_size: int) -> None:
    data = b" " * prefix_size + pdf_bytes(["Boundary"])
    if prefix_size == 1019:
        result = parsers.extract_document(data)
        assert result.text == "Boundary" and result.issues[0].code == "DOCUMENT_RECOVERED"
    else:
        with pytest.raises(ToolError, match="DOCUMENT_UNSUPPORTED"):
            parsers.extract_document(data)


@pytest.mark.parametrize("failure", ["pages", "text", "empty", "encrypted", "memory", "parser"])
def test_pdf_recovery_does_not_retry_nonstructural_failures(monkeypatch, failure: str) -> None:
    from pypdf.errors import FileNotDecryptedError, LimitReachedError

    calls = []
    error = {
        "pages": parsers.limit_error("Pages"),
        "text": parsers.limit_error("Text"),
        "empty": ToolError("DOCUMENT_NO_TEXT", "Empty"),
        "encrypted": FileNotDecryptedError("Encrypted"),
        "memory": MemoryError(),
        "parser": LimitReachedError("Budget"),
    }[failure]

    def read(data: bytes, *, strict: bool):
        calls.append(strict)
        raise error

    monkeypatch.setattr(parsers, "_read_pdf", read)
    with pytest.raises(type(error)):
        parsers.extract_pdf(b"input")
    assert calls == [True]


@pytest.mark.parametrize("budget", ["MAX_PDF_PAGES", "MAX_TEXT_CHARACTERS"])
def test_recovered_pdf_still_enforces_budgets(monkeypatch, budget: str) -> None:
    monkeypatch.setattr(parsers, budget, 1)
    with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED"):
        parsers.extract_document(damaged_pdf_bytes("xref"))


@pytest.mark.parametrize("main_name", ["word/document2.xml", "word/body.dat"])
def test_docx_main_part_uses_opc_relationships(main_name: str) -> None:
    result = parsers.extract_document(relocated_docx_bytes(main_name))
    assert result.text == "Relocated\n" and result.format == "docx"


def rewrite_package(data: bytes, replacements: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            target.writestr(name, replacements.get(name, source.read(name)))
    return output.getvalue()


def test_docm_main_content_type_is_unsupported() -> None:
    data = docx_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        types = archive.read("[Content_Types].xml").replace(
            b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
            b"application/vnd.ms-word.document.macroEnabled.main+xml",
        )
    with pytest.raises(ToolError, match="DOCUMENT_UNSUPPORTED"):
        parsers.extract_document(rewrite_package(data, {"[Content_Types].xml": types}))


@pytest.mark.parametrize("damage", ["missing_target", "broken_relationships", "broken_body"])
def test_invalid_relocated_docx_is_not_misclassified(damage: str) -> None:
    data = relocated_docx_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        replacements = {
            "missing_target": {
                "_rels/.rels": archive.read("_rels/.rels").replace(b"document2.xml", b"absent.xml")
            },
            "broken_relationships": {"_rels/.rels": b"<private-canary"},
            "broken_body": {"word/document2.xml": b"<private-canary"},
        }[damage]
    with pytest.raises(ToolError, match="DOCUMENT_INVALID") as caught:
        parsers.extract_document(rewrite_package(data, replacements))
    assert "private-canary" not in caught.value.detail


def test_docx_entry_budget_applies_before_loading_regardless_of_part_name(monkeypatch) -> None:
    import docx

    data = relocated_docx_bytes("word/body.dat", text="x" * 600000)
    monkeypatch.setattr(parsers, "MAX_DOCX_ENTRY_BYTES", 500000)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert [e.filename for e in archive.infolist() if e.file_size > 500000] == ["word/body.dat"]

    def unexpected_load(*args):
        pytest.fail("Oversized packages must be rejected before loading XML")

    monkeypatch.setattr(docx, "Document", unexpected_load)
    with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED"):
        parsers.extract_document(data)


@pytest.mark.parametrize("remaining,entry_limit", [(10, 100), (100, 10)])
def test_docx_budgets_count_actual_decompressed_bytes(
    monkeypatch, remaining: int, entry_limit: int
):
    monkeypatch.setattr(parsers, "MAX_DOCX_ENTRY_BYTES", entry_limit)
    with zipfile.ZipFile(io.BytesIO(docx_bytes())) as archive:
        monkeypatch.setattr(archive, "open", lambda _: io.BytesIO(b"x" * 20))
        with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED"):
            parsers._expanded_size(archive, zipfile.ZipInfo("part.xml"), remaining)


def test_xml_memory_error_is_not_reported_as_document_damage(monkeypatch) -> None:
    from lxml import etree  # ty: ignore[unresolved-import]

    error = etree.XMLSyntaxError("private-canary", 0, 0, 0)
    error.error_log = [SimpleNamespace(type=etree.ErrorTypes.ERR_NO_MEMORY)]

    def out_of_memory(data: bytes):
        raise error

    monkeypatch.setattr(parsers, "_detect_and_extract", out_of_memory)
    with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED") as caught:
        parsers.extract_document(b"input")
    assert "private-canary" not in caught.value.detail
