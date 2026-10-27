"""Selected attachment downloads."""

from typing import Annotated

from mcp.server import MCPServer
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import StringConstraints

from telegram_mcp.adapters import wire
from telegram_mcp.adapters.mcp.errors import Expose
from telegram_mcp.application.service import Service


def register(server: MCPServer[None], service: Service, expose: Expose) -> None:
    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        )
    )
    @expose
    async def download_attachment(
        attachment_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)],
        save: bool = False,
    ) -> Annotated[CallToolResult, wire.DownloadResult]:
        """Download one explicitly selected attachments[].id, up to 20 MiB.

        Requires allow_attachment_downloads and ordinary source-read permission. Use
        get_messages or get_album to select an attachment first. The ID selects the current
        attachment at that message, not an archived version. Protected and expiring media
        cannot be downloaded. Returns a local path and a ResourceLink, without inline bytes;
        use resources/read before expires_at to retrieve binary content. Files expire after
        15 minutes or server shutdown. The local path is on the server computer. Temporary
        failures require retrying this call; respect retry_after_seconds. No partial resume.
        save=true retains a permanent copy when archive_directory is configured.
        """
        return wire.encode(await service.download_attachment(attachment_id, save))
