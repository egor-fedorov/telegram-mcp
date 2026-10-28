"""Build a portable MCPB from the release wheel and the repository's locked dependencies."""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "telegram-context-mcp"
BUNDLE_PROJECT = "telegram-mcp-desktop"
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"


def build_environment() -> dict[str, str]:
    """Keep local credentials and uv project/index overrides out of the build."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TELEGRAM_")
        and (not key.startswith("UV_") or key in {"UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"})
        and key not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    }


def run_uv(arguments: list[str], directory: Path) -> str:
    return subprocess.check_output(
        ["uv", *arguments],
        cwd=directory,
        env=build_environment(),
        text=True,
        encoding="utf-8",
        timeout=180,
    )


def validate_wheel(wheel: Path, version: str) -> None:
    expected = f"telegram_context_mcp-{version}-py3-none-any.whl"
    if wheel.name != expected:
        raise ValueError(f"Expected the release wheel {expected}.")
    info = f"telegram_context_mcp-{version}.dist-info/"
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            source = name.startswith("telegram_mcp/") and (
                name.endswith(".py") or name.endswith("/py.typed")
            )
            metadata = name in {
                info + "METADATA",
                info + "WHEEL",
                info + "RECORD",
                info + "entry_points.txt",
                info + "licenses/LICENSE",
            }
            if not (source or metadata) or ".." in Path(name).parts:
                raise ValueError(f"Unexpected file in release wheel: {name}")
        metadata = BytesParser().parsebytes(archive.read(info + "METADATA"))
        if metadata["Name"] != PACKAGE or metadata["Version"] != version:
            raise ValueError("Wheel metadata must match the package name and version.")


def runtime_constraints(root: Path) -> list[str]:
    requirements = run_uv(
        [
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--no-header",
            "--no-annotate",
        ],
        root,
    )
    return [line for line in requirements.splitlines() if line and not line.startswith("#")]


def project_toml(version: str, python: str, wheel: str, constraints: list[str]) -> str:
    # JSON string/array literals are also TOML literals for these ASCII requirements.
    return (
        "[project]\n"
        f"name = {json.dumps(BUNDLE_PROJECT)}\n"
        f"version = {json.dumps(version)}\n"
        f"requires-python = {json.dumps(python)}\n"
        f"dependencies = [{json.dumps(f'{PACKAGE}=={version}')} ]\n\n"
        "[tool.uv]\n"
        "package = false\n"
        f"constraint-dependencies = {json.dumps(constraints, indent=2)}\n\n"
        "[tool.uv.sources]\n"
        f"{PACKAGE} = {{ path = {json.dumps('wheels/' + wheel)} }}\n"
    )


def validate_lock(root: Path, stage: Path, wheel: str, version: str) -> None:
    original = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked = tomllib.loads((stage / "uv.lock").read_text(encoding="utf-8"))
    known = {(package["name"], package["version"]): package for package in original["package"]}
    for package in locked["package"]:
        name, source = package["name"], package["source"]
        if name == BUNDLE_PROJECT:
            expected = {"virtual": "."}
        elif name == PACKAGE:
            expected = {"path": "wheels/" + wheel}
        else:
            expected = {"registry": "https://pypi.org/simple"}
        if source != expected:
            raise ValueError(f"Nonportable source in bundle lock: {name}")
        if name in {BUNDLE_PROJECT, PACKAGE}:
            if package["version"] != version:
                raise ValueError("Bundle and wheel versions must match.")
        elif (name, package["version"]) not in known:
            raise ValueError(f"Bundle dependency differs from the repository lock: {name}")
        elif any(
            package.get(field) != known[name, package["version"]].get(field)
            for field in ("sdist", "wheels")
        ):
            raise ValueError(f"Bundle artifacts differ from the repository lock: {name}")


def prepare(root: Path, wheel: Path, stage: Path) -> dict[str, bytes]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    validate_wheel(wheel, version)
    template = root / "contrib/mcpb"
    manifest: dict[str, Any] = json.loads((template / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest["compatibility"]["runtimes"]["python"] = project["requires-python"]
    files = {
        "manifest.json": (json.dumps(manifest, indent=2) + "\n").encode(),
        "server.py": (template / "server.py").read_bytes(),
        "LICENSE": (root / "LICENSE").read_bytes(),
        "wheels/" + wheel.name: wheel.read_bytes(),
        "pyproject.toml": project_toml(
            version, project["requires-python"], wheel.name, runtime_constraints(root)
        ).encode(),
    }
    for name, data in files.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    run_uv(["lock", "--python", "3.12"], stage)
    validate_lock(root, stage, wheel.name, version)
    files["uv.lock"] = (stage / "uv.lock").read_bytes()
    return files


def bundle_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    # Store entries to avoid OS-specific timestamps, permissions and zlib differences.
    # The largest member, the wheel, is already compressed.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def validate_manifest(stage: Path) -> None:
    npx = shutil.which("npx")
    if npx is None:
        raise ValueError("Official manifest validation requires Node.js and npx.")
    subprocess.run(
        [npx, "--yes", MCPB_CLI, "validate", str(stage / "manifest.json")],
        check=True,
        env=build_environment(),
        timeout=180,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, help="Exact release wheel; defaults to dist/.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/mcpb")
    parser.add_argument("--validate", action="store_true", help="Use the pinned official MCPB CLI.")
    parser.add_argument("--check", action="store_true", help="Rebuild and compare existing bytes.")
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    wheel = args.wheel or ROOT / "dist" / f"telegram_context_mcp-{version}-py3-none-any.whl"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="mcpb-build-", dir=args.output_dir) as temporary:
            stage = Path(temporary)
            files = prepare(ROOT, wheel.resolve(), stage)
            if args.validate:
                validate_manifest(stage)
            content = bundle_bytes(files)
        artifact = args.output_dir / f"telegram-mcp-{version}.mcpb"
        checksum = artifact.with_suffix(".mcpb.sha256")
        digest = f"{hashlib.sha256(content).hexdigest()}  {artifact.name}\n"
        if args.check:
            if artifact.read_bytes() != content or checksum.read_text(encoding="utf-8") != digest:
                raise ValueError(
                    "Rebuilt MCPB bytes or checksum differ from the existing artifact."
                )
        else:
            artifact.write_bytes(content)
            checksum.write_text(digest, encoding="utf-8", newline="\n")
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"MCPB build failed: {exc}\n")
    print(f"{'Verified' if args.check else 'Built'} {artifact.name} ({len(content)} bytes).")


if __name__ == "__main__":
    main()
