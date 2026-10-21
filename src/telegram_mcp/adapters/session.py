"""Portable exclusive ownership of a Telegram session shared by login and reads."""

import os
from pathlib import Path
from typing import BinaryIO

import portalocker

from telegram_mcp.domain.models import ToolError


class SessionLease:
    def __init__(self, path: Path):
        self.path = path
        self._file: BinaryIO | None = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(
        self,
        *,
        conflict_message: str = (
            "Another process owns this session. Use 'connect' for a running shared server, "
            "or stop that process before running a direct command."
        ),
    ) -> None:
        if self.held:
            return
        lock_path = str(self.path.resolve()) + ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        stream = os.fdopen(fd, "r+b")
        try:
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.AlreadyLocked:
            stream.close()
            raise ToolError("SESSION_IN_USE", conflict_message) from None
        except portalocker.exceptions.LockException:
            stream.close()
            raise ToolError(
                "SESSION_LOCK_ERROR",
                "Cannot lock the Telegram session. Check the session directory permissions "
                "and use a local filesystem that supports file locking.",
            ) from None
        except BaseException:
            stream.close()
            raise
        self._file = stream

    def close(self) -> None:
        stream, self._file = self._file, None
        if stream is not None:
            try:
                portalocker.unlock(stream)
            finally:
                stream.close()
