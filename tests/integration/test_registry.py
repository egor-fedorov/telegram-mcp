"""Registry identity, immutable release selection and retry gates without network access."""

import io
import json
import shutil
import subprocess
import tomllib
from copy import deepcopy
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from scripts import check_registry as registry
from tests.support.paths import PROJECT_ROOT


@pytest.fixture
def metadata() -> tuple[dict[str, Any], dict[str, Any], str]:
    server = json.loads((PROJECT_ROOT / "server.json").read_text(encoding="utf-8"))
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    return server, project, (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")


def test_repository_manifest_matches_package_and_cli() -> None:
    assert registry.load_manifest(PROJECT_ROOT)["name"] == registry.SERVER_NAME


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "99.0.0"),
        ("name", "io.github.someone/telegram-mcp"),
        ("$schema", "https://example.invalid/schema.json"),
        ("repository", {"url": "https://example.invalid", "source": "github"}),
        ("websiteUrl", "http://127.0.0.1:8765/mcp"),
        ("description", "x" * 101),
        ("title", ""),
        ("remotes", [{"type": "streamable-http", "url": "http://127.0.0.1:8765/mcp"}]),
    ],
)
def test_invalid_server_identity_is_rejected(metadata, field: str, value: object) -> None:
    server, project, readme = metadata
    server[field] = value
    with pytest.raises(ValueError):
        registry.validate_manifest(server, project, readme)


@pytest.mark.parametrize(
    "field,value",
    [
        ("identifier", "telegram-mcp"),
        ("version", "0.0.0"),
        ("registryBaseUrl", "https://example.invalid"),
        ("runtimeArguments", []),
        ("packageArguments", [{"type": "positional", "value": "login"}]),
        ("transport", {"type": "streamable-http"}),
        ("environmentVariables", [{"name": "TELEGRAM_API_HASH", "value": "synthetic-secret"}]),
    ],
)
def test_invalid_or_sensitive_launch_metadata_is_rejected(metadata, field: str, value: object):
    server, project, readme = metadata
    server["packages"][0][field] = value
    with pytest.raises(ValueError, match="PyPI/uvx STDIO"):
        registry.validate_manifest(server, project, readme)


def test_package_named_console_alias_is_required(metadata) -> None:
    server, project, readme = metadata
    project["scripts"].pop(project["name"])
    with pytest.raises(ValueError, match="package-named executable"):
        registry.validate_manifest(server, project, readme)


@pytest.mark.parametrize("readme", ["No marker", "mcp-name: io.github.someone/other"])
def test_missing_or_wrong_ownership_marker_is_rejected(metadata, readme: str) -> None:
    server, project, _ = metadata
    with pytest.raises(ValueError, match="ownership marker"):
        registry.validate_manifest(server, project, readme)


@pytest.fixture
def tagged_repo(tmp_path: Path) -> tuple[Path, str]:
    registry.git(tmp_path, "init", "-b", "main")
    for key, value in (
        ("user.name", "Registry Test"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        registry.git(tmp_path, "config", key, value)
    for name in ("server.json", "pyproject.toml", "README.md"):
        shutil.copyfile(PROJECT_ROOT / name, tmp_path / name)
    version = registry.load_manifest(tmp_path)["version"]
    registry.git(tmp_path, "add", ".")
    registry.git(tmp_path, "commit", "-m", "test: prepare Registry release")
    tag = f"v{version}"
    registry.git(tmp_path, "tag", tag)
    registry.git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp_path, tag


def test_retry_reads_immutable_tag_metadata_not_current_checkout(tagged_repo) -> None:
    repo, tag = tagged_repo
    expected = registry.load_manifest(repo)
    (repo / "server.json").write_text("{}", encoding="utf-8")
    (repo / "README.md").write_text("Current work is unrelated", encoding="utf-8")
    assert registry.load_manifest(repo, tag) == expected


def test_retry_rejects_tag_outside_main(tagged_repo) -> None:
    repo, tag = tagged_repo
    registry.git(repo, "commit", "--allow-empty", "-m", "test: unmerged metadata")
    registry.git(repo, "tag", "-f", tag)
    with pytest.raises(subprocess.CalledProcessError):
        registry.load_manifest(repo, tag)


@pytest.mark.parametrize("tag", ["main", "--all", "v0.4.1;echo unsafe", "../main"])
def test_retry_rejects_non_release_refs(tag: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stable release tag"):
        registry.load_manifest(tmp_path, tag)


def test_retry_rejects_tag_version_mismatch(tagged_repo) -> None:
    repo, _ = tagged_repo
    registry.git(repo, "tag", "v99.0.0")
    with pytest.raises(ValueError, match="tag must match"):
        registry.load_manifest(repo, "v99.0.0")


def test_sync_derives_both_versions_from_pyproject(tagged_repo) -> None:
    repo, _ = tagged_repo
    original = registry.load_manifest(repo)
    project = repo / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace(
            f'version = "{original["version"]}"', 'version = "99.0.0"', 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="sync-version"):
        registry.load_manifest(repo)
    updated = registry.load_manifest(repo, sync=True)
    original["version"] = original["packages"][0]["version"] = "99.0.0"
    assert registry.load_manifest(repo) == updated == original


def pypi_response(server: dict[str, Any]) -> dict[str, Any]:
    package = server["packages"][0]
    return {
        "info": {
            "name": package["identifier"],
            "version": package["version"],
            "description": f"<!-- mcp-name: {server['name']} -->",
        },
        "urls": [{"packagetype": "bdist_wheel"}, {"packagetype": "sdist"}],
    }


def test_pypi_gate_uses_exact_package_version(metadata, monkeypatch) -> None:
    server, _, _ = metadata
    urls = []

    def fetch(url: str):
        urls.append(url)
        return pypi_response(server)

    monkeypatch.setattr(registry, "fetch_json", fetch)
    registry.check_pypi(server)
    assert urls == [f"https://pypi.org/pypi/telegram-context-mcp/{server['version']}/json"]


@pytest.mark.parametrize("problem", ["marker", "name", "version", "files", "yanked"])
def test_pypi_gate_refuses_incomplete_or_wrong_publication(metadata, monkeypatch, problem):
    server, _, _ = metadata
    response = pypi_response(server)
    if problem == "marker":
        response["info"]["description"] = "Published before Registry integration"
    elif problem in ("name", "version"):
        response["info"][problem] = "wrong"
    elif problem == "files":
        response["urls"].pop()
    else:
        response["urls"][0]["yanked"] = True
    monkeypatch.setattr(registry, "fetch_json", lambda url: response)
    with pytest.raises(ValueError):
        registry.check_pypi(server)


def test_registry_retry_is_a_noop_only_for_matching_active_metadata(metadata, monkeypatch):
    server, _, _ = metadata
    response = {
        "server": deepcopy(server),
        "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
    }
    urls = []

    def fetch(url: str, *, missing_ok: bool):
        assert missing_ok
        urls.append(url)
        return response

    monkeypatch.setattr(registry, "fetch_json", fetch)
    assert registry.registry_published(server)
    assert urls == [
        f"{registry.REGISTRY}/io.github.egor-fedorov%2Ftelegram-mcp/versions/{server['version']}"
    ]
    response["server"]["description"] = "Different immutable metadata"
    with pytest.raises(ValueError, match="different immutable"):
        registry.registry_published(server)
    response["server"] = server
    response["_meta"]["io.modelcontextprotocol.registry/official"]["status"] = "deleted"
    with pytest.raises(ValueError, match="not active"):
        registry.registry_published(server)


def test_missing_registry_version_can_be_published(metadata, monkeypatch) -> None:
    monkeypatch.setattr(registry, "fetch_json", lambda *args, **kwargs: None)
    assert not registry.registry_published(metadata[0])


@pytest.mark.parametrize("code", [404, 403, 500])
def test_only_http_404_means_registry_version_is_missing(monkeypatch, code: int) -> None:
    def fail(url: str, *, timeout: int):
        assert timeout == 30
        raise HTTPError(url, code, "synthetic failure", Message(), None)

    monkeypatch.setattr(registry, "urlopen", fail)
    if code == 404:
        assert registry.fetch_json("https://example.invalid", missing_ok=True) is None
        with pytest.raises(HTTPError):
            registry.fetch_json("https://example.invalid")
    else:
        with pytest.raises(HTTPError):
            registry.fetch_json("https://example.invalid", missing_ok=True)


def test_network_failure_is_not_mistaken_for_missing_version(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise URLError("synthetic TLS failure")

    monkeypatch.setattr(registry, "urlopen", fail)
    with pytest.raises(URLError):
        registry.fetch_json("https://example.invalid", missing_ok=True)


@pytest.mark.parametrize(
    "body",
    [b"[]", b"not json", b"x" * (2 * 1024 * 1024 + 1)],
    ids=["not-an-object", "invalid-json", "oversized"],
)
def test_invalid_or_oversized_remote_metadata_is_rejected(monkeypatch, body: bytes) -> None:
    monkeypatch.setattr(registry, "urlopen", lambda *args, **kwargs: io.BytesIO(body))
    with pytest.raises(ValueError):
        registry.fetch_json("https://example.invalid")
