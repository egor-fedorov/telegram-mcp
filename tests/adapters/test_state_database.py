"""Private database preparation must preserve every byte on Windows."""

import os
import sqlite3
import sys
from contextlib import closing

import pytest

from telegram_mcp.adapters.state_database import open_private_database


def binary_database(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE payload (value BLOB)")
        db.execute("INSERT INTO payload VALUES (?)", (b"synthetic\r\n\x1a",))
    content = path.read_bytes()
    assert content.endswith(b"\x1a")
    return content


def test_private_database_open_preserves_trailing_control_z(tmp_path):
    path = tmp_path / "private.sqlite3"
    content = binary_database(path)
    for _ in range(3):
        with closing(open_private_database(path)) as db:
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert db.execute("SELECT value FROM payload").fetchone()[0] == b"synthetic\r\n\x1a"
        assert path.read_bytes() == content


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows CRT text-mode semantics")
def test_windows_text_mode_reproduces_truncation_without_a_write(tmp_path):
    path = tmp_path / "text-mode-control.sqlite3"
    content = binary_database(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    assert path.read_bytes() == content[:-1]
