"""Read the installed distribution's version for all external interfaces."""

import json
import re
from importlib.metadata import version
from importlib.resources import files

from telegram_mcp.adapters.wire.status import BuildInfo


def get_version() -> str:
    return version("telegram-context-mcp")


def _build_commit() -> str | None:
    try:
        value = json.loads(files("telegram_mcp").joinpath("_build_info.json").read_text())
        commit = value.get("build_commit")
    except (OSError, ValueError, AttributeError):
        return None
    return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None


# A running process must not advertise freshly installed metadata as its loaded code.
_BUILD: BuildInfo = {"version": get_version(), "build_commit": _build_commit()}


def get_build_info() -> BuildInfo:
    return _BUILD.copy()
