"""Exclusive ownership of a local Telegram session shared by login and reads."""

import fcntl
import os
from pathlib import Path

from telegram_mcp.domain.models import ToolError


class SessionLease:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(
        self,
        *,
        conflict_message: str = "Another process owns this session. Stop it before retrying.",
    ) -> None:
        if self.held:
            return
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ToolError("SESSION_IN_USE", conflict_message) from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
