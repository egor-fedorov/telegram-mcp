"""Launch only the existing shared-server proxy from a desktop extension."""

import argparse
import sys
from pathlib import Path

from telegram_mcp.cli import main as run_cli


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = args.config.expanduser()
    if not config.is_absolute() or not config.is_file():
        parser.exit(
            1,
            "MCPB_SETUP_REQUIRED: Select an existing absolute config.toml path in the "
            "extension settings. Complete local login and configuration first.\n",
        )
    if config.resolve().is_relative_to(Path(__file__).resolve().parent):
        parser.exit(
            1,
            "MCPB_SETUP_REQUIRED: Keep the shared configuration outside the extension "
            "directory so updates and removal preserve it.\n",
        )
    sys.argv = ["telegram-mcp", "--config", str(config), "connect"]
    run_cli()


if __name__ == "__main__":
    main()
