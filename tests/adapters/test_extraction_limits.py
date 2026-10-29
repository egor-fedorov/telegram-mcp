"""Memory limits are set only in Linux workers, preserving stricter inherited caps."""

import sys
from types import SimpleNamespace

import pytest

from telegram_mcp.adapters.extraction import limits
from telegram_mcp.domain.models import ToolError


@pytest.mark.parametrize("infinity", [-1, 2**64 - 1])
@pytest.mark.parametrize("inherited", [(None, None), (100, None), (100, 200), (2**31, 2**32)])
def test_linux_limit_preserves_stricter_limits(monkeypatch, infinity: int, inherited: tuple):
    calls = []
    resource = SimpleNamespace(
        RLIMIT_AS=9,
        RLIM_INFINITY=infinity,
        getrlimit=lambda _: tuple(infinity if value is None else value for value in inherited),
        setrlimit=lambda kind, values: calls.append((kind, values)),
    )
    monkeypatch.setattr(limits, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setitem(sys.modules, "resource", resource)
    limits.limit_memory()
    expected = tuple(1024**3 if value is None else min(value, 1024**3) for value in inherited)
    assert calls == [(resource.RLIMIT_AS, expected)]


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_other_platforms_do_not_import_resource(monkeypatch, platform: str):
    monkeypatch.setattr(limits, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setitem(sys.modules, "resource", None)
    limits.limit_memory()


@pytest.mark.parametrize("stage", ["get", "set", "import"])
def test_linux_limit_setup_failure_is_closed_and_sanitized(monkeypatch, stage: str):
    def failure(*args):
        raise OSError("private-canary")

    resource = SimpleNamespace(
        RLIMIT_AS=9,
        RLIM_INFINITY=-1,
        getrlimit=failure if stage == "get" else lambda _: (-1, -1),
        setrlimit=failure,
    )
    monkeypatch.setattr(limits, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setitem(sys.modules, "resource", None if stage == "import" else resource)
    with pytest.raises(ToolError, match="EXTRACTION_FAILED") as caught:
        limits.limit_memory()
    assert "private-canary" not in caught.value.detail
