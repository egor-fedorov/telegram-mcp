"""Embed source identity in distributions, including wheels rebuilt from an sdist."""

import json
import re
import subprocess
from importlib import import_module
from pathlib import Path


def build_commit(root: Path) -> str | None:
    if (root / ".git").exists():
        try:
            dirty = subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=root,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if dirty.strip():
                return None
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return None
    else:
        try:
            commit = json.loads(
                (root / "src/telegram_mcp/_build_info.json").read_text(encoding="utf-8")
            ).get("build_commit")
        except (OSError, ValueError, AttributeError):
            return None
    return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def get_build_hook():
    # Hatchling is a build-system dependency, not a runtime or development dependency.
    interface = import_module("hatchling.builders.hooks.plugin.interface").BuildHookInterface

    class BuildInfoHook(interface):
        def initialize(self, version, build_data):
            if version == "editable":
                return
            root = Path(self.root)
            self.record = Path(self.directory) / f"build-info-{self.target_name}.json"
            self.record.parent.mkdir(parents=True, exist_ok=True)
            self.record.write_text(
                json.dumps({"build_commit": build_commit(root)}, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            target = "telegram_mcp/_build_info.json"
            if self.target_name == "sdist":
                target = "src/" + target
            build_data["force_include"][str(self.record)] = target

        def finalize(self, version, build_data, artifact_path):
            if version != "editable":
                self.record.unlink(missing_ok=True)

    return BuildInfoHook
