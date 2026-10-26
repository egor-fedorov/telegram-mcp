"""Open a private, non-redirected SQLite file owned by the session process."""

import os
import sqlite3
from pathlib import Path

from telegram_mcp.adapters.private_paths import prepare_private_directory, protect_private_path


def open_private_database(path: Path) -> sqlite3.Connection:
    prepare_private_directory(path.parent)
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.is_symlink() or candidate.is_junction():
            raise OSError("Redirected database file")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    protect_private_path(path)
    return sqlite3.connect(path, timeout=1)
