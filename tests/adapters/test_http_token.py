import stat
import sys
from pathlib import Path

import pytest

from telegram_mcp.adapters import http_token
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.domain.models import ToolError
from tests.support.windows_acl import assert_windows_token_acl


def test_http_token_persists_and_has_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "private" / "token"
    first = load_token(path, create=True)
    assert len(first) == 43
    assert load_token(path, create=True) == load_token(path) == first
    if sys.platform == "win32":
        assert_windows_token_acl(path)
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.chmod(0o644)
        assert load_token(path, create=True) == first
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows security APIs")
def test_windows_acl_can_be_applied_before_writing_the_token(tmp_path: Path) -> None:
    path = tmp_path / "token"
    with path.open("w", encoding="ascii") as stream:
        # Exercise the raw API so a binding/API mismatch is visible in Windows CI.
        http_token._protect_windows_token(path)
        assert_windows_token_acl(path)
        stream.write("a" * 43)
    assert load_token(path) == "a" * 43


@pytest.mark.parametrize("contents", [None, "", "synthetic-secret", "a" * 1025, "non-ascii: é"])
def test_invalid_or_missing_token_is_redacted(tmp_path: Path, contents: str | None) -> None:
    path = tmp_path / "token"
    if contents is not None:
        path.write_text(contents)
    with pytest.raises(ToolError, match="HTTP_TOKEN_ERROR") as caught:
        load_token(path)
    assert contents is None or not contents or contents not in str(caught.value)
    assert "serve --transport http" in str(caught.value)


def test_token_permission_failure_removes_empty_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(_path: Path) -> None:
        raise PermissionError("synthetic-private-path")

    monkeypatch.setattr(http_token, "protect_token", denied)
    path = tmp_path / "token"
    with pytest.raises(ToolError, match="HTTP_TOKEN_ERROR") as caught:
        load_token(path, create=True)
    assert not path.exists()
    assert "synthetic-private-path" not in str(caught.value)
