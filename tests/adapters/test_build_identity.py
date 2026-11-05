"""Loaded builds remain stable across install/checkout changes and proxy forwarding."""

import json
from types import SimpleNamespace

import pytest
from mcp_types import CallToolResult, TextContent, Tool

from hatch_build import build_commit
from telegram_mcp.adapters import version
from telegram_mcp.adapters.bridge_status import status_result, status_tool
from telegram_mcp.adapters.wire.status import BuildInfo


def test_build_identity_preserves_sdist_commit_without_git(tmp_path):
    package = tmp_path / "src" / "telegram_mcp"
    package.mkdir(parents=True)
    assert build_commit(tmp_path) is None
    (package / "_build_info.json").write_text(json.dumps({"build_commit": "a" * 40}))
    assert build_commit(tmp_path) == "a" * 40
    (package / "_build_info.json").write_text(json.dumps({"build_commit": "untrusted non-sha"}))
    assert build_commit(tmp_path) is None


@pytest.mark.parametrize("dirty", [False, True])
def test_build_commit_never_labels_dirty_sources_as_a_clean_commit(tmp_path, monkeypatch, dirty):
    (tmp_path / ".git").mkdir()
    calls = []

    def output(command, **kwargs):
        calls.append(command)
        assert kwargs["cwd"] == tmp_path
        return (" M tracked.py\n" if dirty else "") if command[1] == "status" else "b" * 40

    monkeypatch.setattr("hatch_build.subprocess.check_output", output)
    assert build_commit(tmp_path) == (None if dirty else "b" * 40)
    assert len(calls) == (1 if dirty else 2)


def test_loaded_identity_does_not_follow_new_installed_metadata(monkeypatch):
    loaded = version.get_build_info()
    monkeypatch.setattr(version, "get_version", lambda: "99.0.0")
    monkeypatch.setattr(version, "_build_commit", lambda: "c" * 40)
    assert version.get_build_info() == loaded
    changed = version.get_build_info()
    changed["version"] = "changed"
    assert version.get_build_info() == loaded


def test_malformed_packaged_build_identity_is_unknown(monkeypatch):
    monkeypatch.setattr(
        version,
        "files",
        lambda name: SimpleNamespace(
            joinpath=lambda name: SimpleNamespace(read_text=lambda: "{broken")
        ),
    )
    assert version._build_commit() is None


@pytest.mark.parametrize("legacy_payload", [False, True])
def test_proxy_reports_its_own_build_separately_and_mirrors_text(legacy_payload):
    original = {"authorized": True}
    server = {"version": "0.7.0", "build_commit": "a" * 40, "started_at": "2026-11-05T00:00:00Z"}
    if not legacy_payload:
        original["server"] = server
        original["connect"] = None
    upstream = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(original))], structured_content=original
    )
    proxy: BuildInfo = {"version": "0.8.0", "build_commit": "b" * 40}
    result = status_result(upstream, proxy, "0.7.0")
    assert result.structured_content["connect"] == proxy
    assert result.structured_content["server"] == (
        server if not legacy_payload else {"version": "0.7.0", "build_commit": None}
    )
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == result.structured_content
    assert upstream.structured_content == original
    tool = Tool(
        name="get_status",
        input_schema={},
        output_schema={
            "type": "object",
            "properties": {"authorized": {"type": "boolean"}},
            "additionalProperties": False,
        },
    )
    enriched = status_tool(tool)
    assert enriched.output_schema is not None and tool.output_schema is not None
    assert "connect" in enriched.output_schema["properties"]
    assert "connect" not in tool.output_schema["properties"]
    assert status_tool(enriched) == enriched


def test_proxy_leaves_status_errors_untouched():
    error = CallToolResult(content=[TextContent(type="text", text="offline")], is_error=True)
    assert status_result(error, {"version": "1.0", "build_commit": None}, None) is error
