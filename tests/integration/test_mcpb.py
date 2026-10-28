"""Portable bundle contents, setup errors and the actual extension entry point."""

import asyncio
import io
import json
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from contrib.mcpb import server as launcher
from scripts import build_mcpb
from scripts.smoke_mcpb import extract_bundle
from telegram_mcp.adapters.config import save_settings
from tests.support.paths import PROJECT_ROOT
from tests.support.resources import assert_binary_read, assert_compact_download
from tests.support.transport import running_resource_server


def wheel_at(directory: Path, *, extra: str | None = None, version: str = "1.2.3") -> Path:
    wheel = directory / "telegram_context_mcp-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("telegram_mcp/__init__.py", "")
        archive.writestr(
            "telegram_context_mcp-1.2.3.dist-info/METADATA",
            f"Name: telegram-context-mcp\nVersion: {version}\n",
        )
        if extra:
            archive.writestr(extra, "synthetic private data")
    return wheel


@pytest.mark.parametrize(
    "extra",
    [
        "account.session",
        ".env",
        "mcp.token",
        "telegram_mcp/config.toml",
        "telegram_mcp/../secret.py",
    ],
)
def test_bundle_rejects_private_or_unexpected_wheel_files(tmp_path, extra):
    with pytest.raises(ValueError, match="Unexpected file"):
        build_mcpb.validate_wheel(wheel_at(tmp_path, extra=extra), "1.2.3")


def test_bundle_rejects_mismatching_release_wheel(tmp_path):
    with pytest.raises(ValueError, match="metadata"):
        build_mcpb.validate_wheel(wheel_at(tmp_path, version="9.9.9"), "1.2.3")


def test_package_constraints_preserve_platform_markers_and_local_wheel():
    constraints = ["example==2.3.4; sys_platform == 'win32'"]
    project = tomllib.loads(build_mcpb.project_toml("1.2.3", ">=3.12", "package.whl", constraints))
    assert project["tool"]["uv"]["constraint-dependencies"] == constraints
    assert project["tool"]["uv"]["sources"]["telegram-context-mcp"] == {
        "path": "wheels/package.whl"
    }


def test_prepare_uses_allowlisted_files_and_derives_version(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copytree(PROJECT_ROOT / "contrib/mcpb", root / "contrib/mcpb")
    (root / "pyproject.toml").write_text(
        '[project]\nversion = "1.2.3"\nrequires-python = ">=3.12"\n'
    )
    (root / "uv.lock").write_text('[[package]]\nname = "example"\nversion = "2.3.4"\n')
    (root / "LICENSE").write_text("Synthetic license")
    (root / "account.session").write_text("synthetic secret")
    (root / "contrib/mcpb/config.toml").write_text("synthetic secret")

    def run_uv(arguments, directory):
        if arguments[0] == "export":
            assert "--locked" in arguments and "--no-dev" in arguments
            return "example==2.3.4\n"
        assert arguments[0] == "lock"
        (directory / "uv.lock").write_text(
            '[[package]]\nname = "example"\nversion = "2.3.4"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
        )
        return ""

    monkeypatch.setattr(build_mcpb, "run_uv", run_uv)
    files = build_mcpb.prepare(root, wheel_at(tmp_path), tmp_path / "stage")
    assert set(files) == {
        "manifest.json",
        "server.py",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        "wheels/telegram_context_mcp-1.2.3-py3-none-any.whl",
    }
    assert b"synthetic secret" not in b"".join(files.values())
    manifest = json.loads(files["manifest.json"])
    assert manifest["version"] == "1.2.3"
    assert manifest["server"]["type"] == "uv"
    assert manifest["user_config"]["config_file"]["required"]
    assert set(manifest["compatibility"]["platforms"]) == {"win32", "darwin", "linux"}


@pytest.mark.parametrize(
    "source,version",
    [
        ('{ path = "/private/example.whl" }', "2.3.4"),
        ('{ registry = "https://pypi.org/simple" }', "9.9.9"),
    ],
)
def test_bundle_rejects_nonportable_or_changed_resolution(tmp_path, source, version):
    root, stage = tmp_path / "root", tmp_path / "stage"
    root.mkdir()
    stage.mkdir()
    (root / "uv.lock").write_text('[[package]]\nname = "example"\nversion = "2.3.4"\n')
    (stage / "uv.lock").write_text(
        f'[[package]]\nname = "example"\nversion = "{version}"\nsource = {source}\n'
    )
    with pytest.raises(ValueError):
        build_mcpb.validate_lock(root, stage, "package.whl", "1.2.3")


def test_bundle_bytes_do_not_depend_on_file_order_or_permissions():
    files = {"server.py": b"pass\n", "manifest.json": b"{}\n"}
    first = build_mcpb.bundle_bytes(files)
    assert first == build_mcpb.bundle_bytes(dict(reversed(list(files.items()))))
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all(info.external_attr >> 16 == 0o100644 for info in archive.infolist())


def test_bundle_rejects_dependency_artifacts_not_in_reviewed_lock(tmp_path):
    root, stage = tmp_path / "root", tmp_path / "stage"
    root.mkdir()
    stage.mkdir()
    original = (
        '[[package]]\nname = "example"\nversion = "2.3.4"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.pythonhosted.org/example.whl", hash = "sha256:old" }]\n'
    )
    (root / "uv.lock").write_text(original)
    (stage / "uv.lock").write_text(original.replace("sha256:old", "sha256:changed"))
    with pytest.raises(ValueError, match="artifacts differ"):
        build_mcpb.validate_lock(root, stage, "package.whl", "1.2.3")


def test_smoke_refuses_changed_artifact(tmp_path):
    artifact = tmp_path / "test.mcpb"
    artifact.write_bytes(b"modified")
    artifact.with_suffix(".mcpb.sha256").write_text("wrong checksum")
    with pytest.raises(ValueError, match="checksum"):
        extract_bundle(artifact, tmp_path / "extracted")


@pytest.mark.parametrize("kind", ["missing", "relative", "inside-extension"])
def test_launcher_requires_an_existing_external_configuration(tmp_path, monkeypatch, capsys, kind):
    extension = tmp_path / "extension"
    extension.mkdir()
    monkeypatch.setattr(launcher, "__file__", str(extension / "server.py"))
    config = tmp_path / "profile.toml"
    if kind == "inside-extension":
        config = extension / "profile.toml"
    if kind != "missing":
        config.write_text("")
    value = config.name if kind == "relative" else str(config)
    monkeypatch.setattr(sys, "argv", ["server.py", "--config", value])
    monkeypatch.setattr(launcher, "run_cli", lambda: pytest.fail("Invalid setup must not run CLI"))
    with pytest.raises(SystemExit) as caught:
        launcher.main()
    assert caught.value.code == 1
    assert "MCPB_SETUP_REQUIRED" in capsys.readouterr().err


def test_launcher_selects_only_connect_and_preserves_config(tmp_path, monkeypatch):
    config = tmp_path / "profile with spaces.toml"
    config.write_text("# External configuration\n")
    monkeypatch.setattr(sys, "argv", ["server.py", "--config", str(config)])
    received = []
    monkeypatch.setattr(launcher, "run_cli", lambda: received.extend(sys.argv))
    launcher.main()
    assert received == ["telegram-mcp", "--config", str(config), "connect"]
    assert config.read_text() == "# External configuration\n"


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_extension_entry_point_delivers_binary_resources(tmp_path, mode):
    extension = tmp_path / "extension with spaces"
    extension.mkdir()
    shutil.copyfile(PROJECT_ROOT / "contrib/mcpb/server.py", extension / "server.py")
    async with running_resource_server(tmp_path, 1024) as (settings, service):
        config = tmp_path / "shared configuration.toml"
        save_settings(settings, config)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(extension / "server.py"), "--config", str(config)],
        )
        async with asyncio.timeout(30):
            async with Client(parameters, mode=mode, cache=None) as client:
                response = await client.call_tool("download_attachment", {"attachment_id": "1:1"})
                uri = assert_compact_download(response, 1024)
                await assert_binary_read(client, uri, 1024)
        assert service.read_calls and not service.closed
    assert service.closed
