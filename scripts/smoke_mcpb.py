"""Install and exercise an extracted MCPB in a disposable environment without Telegram login."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


def extract_bundle(artifact: Path, destination: Path) -> dict:
    checksum = artifact.with_suffix(".mcpb.sha256").read_text(encoding="utf-8")
    expected = f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n"
    if checksum != expected:
        raise ValueError("MCPB checksum does not match its artifact.")
    with zipfile.ZipFile(artifact) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        expected_files = {
            "LICENSE",
            "manifest.json",
            "pyproject.toml",
            "server.py",
            "uv.lock",
            f"wheels/telegram_context_mcp-{manifest['version']}-py3-none-any.whl",
        }
        if set(archive.namelist()) != expected_files or len(archive.namelist()) != 6:
            raise ValueError("Unexpected MCPB contents.")
        archive.extractall(destination)
    return manifest


def smoke(artifact: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TELEGRAM_", "UV_"))
        and key not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    }
    env["PYTHONUTF8"] = "1"
    with tempfile.TemporaryDirectory(prefix="mcpb smoke-", dir=artifact.parent) as temporary:
        root = Path(temporary)
        extension = root / "extension with spaces \u00e9"
        manifest = extract_bundle(artifact, extension)
        subprocess.run(
            ["uv", "sync", "--locked", "--python", "3.12", "--directory", str(extension)],
            check=True,
            env=env,
            timeout=180,
        )
        interpreter = (
            extension / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        probe = root / "probe_mcpb.py"
        shutil.copyfile(Path(__file__).with_name("probe_mcpb.py"), probe)
        subprocess.run(
            [str(interpreter), "-I", str(probe), str(extension)],
            check=True,
            cwd=root,
            env=env,
            timeout=120,
        )
        shutil.rmtree(extension)
        extract_bundle(artifact, extension)
        # Reinstall/removal must leave the external profile byte for byte intact.
        snapshot = json.loads((root / "profile-snapshot.json").read_text(encoding="utf-8"))
        for name, digest in snapshot.items():
            assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    print(
        f"MCPB {manifest['version']}: isolated uv install, proxy and profile preservation passed."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    smoke(args.bundle.resolve())


if __name__ == "__main__":
    main()
