"""A persistent secret for the loopback server, never a Telegram credential."""

import os
import re
import secrets
import sys
from pathlib import Path

from telegram_mcp.domain.models import ToolError


def protect_token(path: Path) -> None:
    if sys.platform == "win32":
        try:
            _protect_windows_token(path)
        except Exception:
            raise OSError("Cannot restrict the token file to the current user") from None
    else:
        path.chmod(0o600)


def _protect_windows_token(path: Path) -> None:
    if sys.platform == "win32":
        import ntsecuritycon
        import win32api
        import win32con
        import win32security

        process_token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            sid = win32security.GetTokenInformation(process_token, win32security.TokenUser)[0]
        finally:
            process_token.Close()
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )


def load_token(path: Path, *, create: bool = False) -> str:
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                protect_token(path)
            else:
                try:
                    with os.fdopen(fd, "w", encoding="ascii") as stream:
                        protect_token(path)
                        stream.write(secrets.token_urlsafe(32) + "\n")
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
        with path.open("r", encoding="ascii") as stream:
            value = stream.read(1024).strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
            raise ValueError("Invalid token format")
        return value
    except (OSError, ValueError, UnicodeError):
        raise ToolError(
            "HTTP_TOKEN_ERROR",
            "Cannot read or create the local HTTP token. Start 'serve --transport http' "
            "with the same configuration and check its token-file permissions.",
        ) from None
