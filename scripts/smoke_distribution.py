"""Install each built artifact in an isolated environment and probe it outside the checkout."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def clean_environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TELEGRAM_")
        and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    env["PYTHONUTF8"] = "1"
    return env


def smoke(artifact: Path, python: str, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="telegram-mcp-package-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        env = clean_environment()
        subprocess.run(["uv", "venv", "--python", python, str(environment)], check=True, env=env)
        interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(interpreter), str(artifact)],
            check=True,
            cwd=root,
            env=env,
        )
        probe = root / "probe_package.py"
        shutil.copyfile(Path(__file__).with_name("probe_package.py"), probe)
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "tests/fixtures/mcp_contract.json",
            root / "mcp_contract.json",
        )
        shutil.copyfile(Path(__file__).resolve().parents[1] / "server.json", root / "server.json")
        subprocess.run(
            [str(interpreter), "-I", str(probe), "--version", version],
            check=True,
            cwd=root,
            env=env,
            timeout=90,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]
    wheels = list(args.dist_dir.glob("*.whl"))
    sdists = list(args.dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        parser.error("Expected exactly one wheel and one source distribution in --dist-dir.")
    for artifact in [*wheels, *sdists]:
        smoke(artifact.resolve(), args.python, version)


if __name__ == "__main__":
    main()
