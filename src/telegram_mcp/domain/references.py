"""Parse exact chat selectors without resolving or granting access to a source."""

import re
from urllib.parse import urlparse

from telegram_mcp.domain.models import ToolError


def chat_target(ref: str) -> int | str:
    value = ref.strip()
    if value.startswith(("t.me/", "telegram.me/", "www.t.me/")):
        value = "https://" + value
    if value.startswith(("https://", "http://")):
        url = urlparse(value)
        if url.hostname not in ("t.me", "telegram.me", "www.t.me") or url.username:
            raise ToolError("INVALID_REFERENCE", "Use a Telegram chat ID, username or t.me link.")
        value = url.path.strip("/")
        if value.startswith("s/"):
            value = value[2:]
        if "/" in value or value.startswith("+"):
            raise ToolError(
                "INVALID_REFERENCE", "Invite links and message links are not chat references."
            )
    if re.fullmatch(r"-?[1-9]\d*", value):
        return int(value)
    if not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}|me", value):
        raise ToolError("INVALID_REFERENCE", "Use a numeric chat ID or exact username.")
    return value
