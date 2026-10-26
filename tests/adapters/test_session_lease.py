"""Cross-process locks are the same on Windows, Linux, and macOS."""

import subprocess
import sys
from pathlib import Path

import portalocker
import pytest

from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.domain.models import ToolError

WORKER = """
import os, sys
from pathlib import Path
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.domain.models import ToolError
lease = SessionLease(Path(sys.argv[1]))
try:
    lease.acquire()
except ToolError as exc:
    assert exc.code == 'SESSION_IN_USE'
    sys.exit(7)
if sys.argv[2] == 'crash':
    os._exit(0)
lease.close()
"""


def test_lease_is_exclusive_across_processes_and_reusable_after_close(tmp_path: Path) -> None:
    path = tmp_path / "account.session"
    lease = SessionLease(path)
    lease.acquire()
    lease.acquire()
    try:
        result = subprocess.run(
            [sys.executable, "-c", WORKER, str(path), "close"], capture_output=True, timeout=10
        )
        assert result.returncode == 7, result.stderr.decode()
        with pytest.raises(ToolError, match="SESSION_IN_USE"):
            SessionLease(path).acquire()
    finally:
        lease.close()
        lease.close()
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(path), "close"], capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr.decode()
    assert Path(str(path) + ".lock").exists()


def test_process_exit_releases_lease_without_removing_lock_file(tmp_path: Path) -> None:
    path = tmp_path / "account.session"
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(path), "crash"], capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr.decode()
    lease = SessionLease(path)
    lease.acquire()
    lease.close()
    assert Path(str(path) + ".lock").exists()


def test_unavailable_file_lock_is_not_reported_as_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handles = []

    def fail_lock(stream, _flags):
        handles.append(stream)
        raise portalocker.exceptions.LockException("synthetic-private-path")

    monkeypatch.setattr(portalocker, "lock", fail_lock)
    lease = SessionLease(tmp_path / "account.session")
    with pytest.raises(ToolError, match="SESSION_LOCK_ERROR") as caught:
        lease.acquire()
    assert "synthetic-private-path" not in str(caught.value)
    assert not lease.held and handles[0].closed
