"""Check optional parsers in a clean installed package, outside the checkout."""

import asyncio
import io

from docx import Document
from pypdf import PdfWriter

from telegram_mcp.adapters.extraction.process import DocumentParser
from telegram_mcp.domain.models import ToolError


async def main() -> None:
    parser = DocumentParser()
    assert parser.available
    try:
        document = Document()
        document.add_paragraph("Installed extraction: Résumé 😀 世界")
        output = io.BytesIO()
        document.save(output)
        result = await parser.extract(output.getvalue())
        assert result.format == "docx" and "😀 世界" in result.text
        pdf = PdfWriter()
        pdf.add_blank_page(width=100, height=100)
        output = io.BytesIO()
        pdf.write(output)
        try:
            await parser.extract(output.getvalue())
        except ToolError as exc:
            assert exc.code == "DOCUMENT_NO_TEXT"
        else:
            raise AssertionError("Blank PDF unexpectedly produced text")
        assert not parser._processes
    finally:
        await parser.close()
    print("Installed extraction extra: DOCX text, PDF parser and worker lifecycle passed.")


if __name__ == "__main__":
    asyncio.run(main())
