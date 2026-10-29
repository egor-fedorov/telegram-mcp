"""The real extraction flow delivers bounded text over every MCP transport."""

import hashlib
import json
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from tests.support.extraction import damaged_pdf_bytes, extraction_backend
from tests.support.transport import connection, running_server

pytestmark = pytest.mark.slow


async def read_document(client: Client, *, recovered_pdf: bool = False) -> None:
    parameters = {"attachment_id": "1:10:document", "text_limit": 5}
    parts, identity, digest = [], None, None
    for _ in range(10):
        response = await client.call_tool("extract_attachment_text", parameters)
        assert not response.is_error
        data = response.structured_content
        assert data is not None
        assert len(response.content) == 1 and response.content[0].type == "text"
        assert json.loads(response.content[0].text) == data
        assert len(response.model_dump_json()) < 4000
        assert "local_path" not in data and data["message_ref"] == "1:10"
        if recovered_pdf:
            assert data["format"] == "pdf" and data["page_count"] == 3
            assert data["source_sha256"] == hashlib.sha256(damaged_pdf_bytes("length")).hexdigest()
            assert [issue["code"] for issue in data["issues"]] == [
                "DOCUMENT_PAGES_WITHOUT_TEXT",
                "DOCUMENT_RECOVERED",
            ]
            assert all(1 <= span["page_number"] <= 3 for span in data["page_spans"])
        else:
            assert "page_count" not in data and data["format"] == "docx"
        if identity is not None:
            assert (data["extraction_id"], data["source_sha256"]) == (identity, digest)
        identity, digest = data["extraction_id"], data["source_sha256"]
        parts.append(data["text"])
        if data["next_text_offset"] is None:
            break
        parameters = {
            "extraction_id": identity,
            "text_offset": data["next_text_offset"],
            "text_limit": 5,
        }
    assert "".join(parts) == ("First\n\n\n\nLast" if recovered_pdf else "Résumé 😀\nПривет 世界\n")
    invalid = await client.call_tool(
        "extract_attachment_text", {"extraction_id": identity, "text_offset": 10000}
    )
    assert invalid.is_error and "INVALID_ARGUMENT" in str(invalid.content)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("schema_mode", ["compact", "full"])
@pytest.mark.parametrize("transport", ["stdio", "http", "connect"])
@pytest.mark.parametrize("recovered_pdf", [False, True])
async def test_extraction_transport_parity(
    tmp_path: Path, mode, schema_mode, transport, recovered_pdf: bool
) -> None:
    if transport == "stdio":
        script = (
            "from pathlib import Path; import sys; "
            "from tests.support.extraction import extraction_stdio_service; "
            "from telegram_mcp.adapters.mcp import create_server; "
            "create_server(extraction_stdio_service(Path(sys.argv[1]), "
            "recovered_pdf=sys.argv[3] == 'True'), "
            "schema_mode=sys.argv[2]).run()"
        )
        command = StdioServerParameters(
            command=sys.executable,
            args=["-c", script, str(tmp_path), schema_mode, str(recovered_pdf)],
        )
        async with Client(command, mode=mode, cache=None) as client:
            await read_document(client, recovered_pdf=recovered_pdf)
    else:
        backend = extraction_backend(recovered_pdf=recovered_pdf)
        async with running_server(
            tmp_path, backend, schema_mode=schema_mode, allow_attachment_downloads=True
        ) as (settings, _):
            async with connection(transport, tmp_path, mode, schema_mode, settings) as client:
                await read_document(client, recovered_pdf=recovered_pdf)
            assert backend.downloads == 1
