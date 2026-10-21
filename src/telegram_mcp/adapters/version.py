"""Read the installed distribution's version for all external interfaces."""

from importlib.metadata import version


def get_version() -> str:
    return version("telegram-context-mcp")
