"""Check Registry metadata offline, or prepare/verify an already published release."""

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

SERVER_NAME = "io.github.egor-fedorov/telegram-mcp"
SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
REPOSITORY = "https://github.com/egor-fedorov/telegram-mcp"
REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"


def ownership_marker(description: str) -> bool:
    return re.findall(r"mcp-name:\s+([^\s<>]+)", description) == [SERVER_NAME]


def validate_manifest(server: dict[str, Any], project: dict[str, Any], readme: str) -> None:
    """Validate this project's fixed launch contract, beyond upstream schema checks."""
    fields = {
        "$schema",
        "name",
        "title",
        "description",
        "repository",
        "websiteUrl",
        "version",
        "packages",
    }
    if not isinstance(server, dict) or set(server) != fields:
        raise ValueError("server.json has missing or unexpected fields.")
    version = project["version"]
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ValueError("project.version must be a stable X.Y.Z version.")
    if server["version"] != version:
        raise ValueError("server.json version differs from project.version; use --sync-version.")
    if server["$schema"] != SCHEMA or server["name"] != SERVER_NAME:
        raise ValueError("server.json must use the reviewed schema and Registry name.")
    if server["repository"] != {"url": REPOSITORY, "source": "github", "id": "1410391452"}:
        raise ValueError("server.json repository does not identify this GitHub repository.")
    if project["urls"]["Repository"] != REPOSITORY:
        raise ValueError("project.urls.Repository differs from the Registry repository.")
    if server["websiteUrl"] != f"{REPOSITORY}#install-and-log-in":
        raise ValueError("server.json websiteUrl must link to local setup instructions.")
    for key in ("title", "description"):
        if not isinstance(server[key], str) or not 1 <= len(server[key].strip()) <= 100:
            raise ValueError(f"server.json {key} must contain 1..100 characters.")
    minimum = re.match(r">=(\d+\.\d+)(?:,|$)", project["requires-python"])
    if minimum is None:
        raise ValueError("Cannot determine the minimum Python version from requires-python.")
    package = {
        "registryType": "pypi",
        "registryBaseUrl": "https://pypi.org",
        "identifier": project["name"],
        "version": version,
        "runtimeHint": "uvx",
        "runtimeArguments": [{"type": "named", "name": "--python", "value": minimum[1]}],
        "packageArguments": [{"type": "positional", "value": "serve"}],
        "transport": {"type": "stdio"},
    }
    if project["name"] != "telegram-context-mcp" or server["packages"] != [package]:
        raise ValueError("server.json packages must match the versioned PyPI/uvx STDIO launch.")
    scripts = project["scripts"]
    if not scripts.get("telegram-mcp") or scripts.get(project["name"]) != scripts["telegram-mcp"]:
        raise ValueError("The package-named executable must point to the telegram-mcp CLI.")
    if not ownership_marker(readme):
        raise ValueError("README.md must contain exactly one matching mcp-name ownership marker.")


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *arguments], text=True, encoding="utf-8"
    ).strip()


def load_manifest(repo: Path, tag: str | None = None, *, sync: bool = False) -> dict[str, Any]:
    commit = None
    if tag is not None:
        if re.fullmatch(r"v\d+\.\d+\.\d+", tag) is None:
            raise ValueError("Use a stable release tag, such as v0.4.1.")
        commit = git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, "origin/main"],
            check=True,
        )

    def read(name: str) -> str:
        return (
            git(repo, "show", f"{commit}:{name}")
            if commit is not None
            else (repo / name).read_text(encoding="utf-8")
        )

    project = tomllib.loads(read("pyproject.toml"))["project"]
    server = json.loads(read("server.json"))
    if tag is not None and tag != f"v{project['version']}":
        raise ValueError("The tag must match project.version in the tagged commit.")
    if sync:
        if tag is not None:
            raise ValueError("Cannot synchronize immutable tagged metadata.")
        server["version"] = project["version"]
        server["packages"][0]["version"] = project["version"]
    validate_manifest(server, project, read("README.md"))
    if sync:
        write_manifest(repo / "server.json", server)
    return server


def write_manifest(path: Path, server: dict[str, Any]) -> None:
    path.write_text(json.dumps(server, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_json(url: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
    try:
        with urlopen(url, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        raise
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Remote metadata exceeds the 2 MiB limit.")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Remote metadata must be a JSON object.")
    return data


def check_pypi(server: dict[str, Any]) -> None:
    package = server["packages"][0]
    data = fetch_json(f"https://pypi.org/pypi/{package['identifier']}/{package['version']}/json")
    if data is None:
        raise ValueError("The selected PyPI release is missing.")
    info = data["info"]
    if info["name"] != package["identifier"] or info["version"] != package["version"]:
        raise ValueError("PyPI package identity/version does not match server.json.")
    if not ownership_marker(info.get("description", "")):
        raise ValueError("The published PyPI description lacks the matching mcp-name marker.")
    files = data.get("urls", [])
    kinds = {file["packagetype"] for file in files if not file.get("yanked")}
    if not {"bdist_wheel", "sdist"} <= kinds:
        raise ValueError("PyPI must contain a non-yanked wheel and source distribution.")


def registry_published(server: dict[str, Any]) -> bool:
    name, version = quote(server["name"], safe=""), quote(server["version"], safe="")
    data = fetch_json(f"{REGISTRY}/{name}/versions/{version}", missing_ok=True)
    if data is None:
        return False
    if data.get("server") != server:
        raise ValueError("Registry already contains different immutable metadata for this version.")
    status = (
        data.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {}).get("status")
    )
    if status != "active":
        raise ValueError("The matching Registry entry is not active.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--tag", help="Read metadata from a release tag merged into origin/main.")
    source.add_argument("--sync-version", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-pypi", action="store_true")
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--check-registry", action="store_true")
    state.add_argument("--require-published", action="store_true")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.github_output and not (args.check_registry or args.require_published):
        parser.error("--github-output requires a Registry status check.")
    try:
        server = load_manifest(args.repo, args.tag, sync=args.sync_version)
        if args.check_pypi:
            check_pypi(server)
        published = False
        if args.check_registry or args.require_published:
            published = registry_published(server)
            if args.require_published and not published:
                raise ValueError(
                    "The expected Registry version is not published yet; retry verification."
                )
        if args.output:
            write_manifest(args.output, server)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"published={str(published).lower()}\n")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.CalledProcessError,
    ) as exc:
        parser.exit(1, f"Registry validation failed: {exc}\n")
    print(f"Validated {server['name']}@{server['version']} metadata.")
    if args.check_registry or args.require_published:
        print(
            "Registry version is already published and matches."
            if published
            else "Not published yet."
        )


if __name__ == "__main__":
    main()
