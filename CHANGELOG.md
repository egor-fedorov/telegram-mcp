# Changelog

Public changes to `telegram-context-mcp`. The command name is `telegram-mcp`.

A version entry describes its target artifact. Verify publication on
[PyPI](https://pypi.org/project/telegram-context-mcp/) and
[GitHub Releases](https://github.com/egor-fedorov/telegram-mcp/releases).

## [Unreleased]

No additional changes recorded.

## [0.2.0] - 2026-10-21

### Added

- A foreground, bearer-authenticated local HTTP server at `127.0.0.1:8765/mcp`.
- A `connect` STDIO proxy so several MCP clients can share one Telegram session,
  read policy, and process-local cursor store.
- `--version`, pinned installation examples, and configuration guides for Codex,
  Claude Desktop, Claude Code, Cursor, and VS Code.
- Package build checks, platform/Python compatibility checks, Trusted Publisher
  release automation, and reviewed dependency-update PRs.

### Changed

- Session locking supports Windows as well as macOS and Linux.
- Local files use explicit UTF-8 handling; configuration errors identify invalid
  fields and sources without exposing their values.
- Interactive configuration preserves existing settings on Enter and explicitly
  prompts for `allow_all_chats`.
- The README focuses on installation and first use; detailed usage, configuration,
  troubleshooting, release instructions, and completed specifications have their
  own documents.

### Compatibility

- `telegram-mcp serve` remains the standalone STDIO command. The same 14 tools and
  message schemas are available through both transports.
- Shared clients must run as one trusted local user. The HTTP process is started
  explicitly; `connect` never starts it or falls back to opening another session.
- Stop the session-owning process before local login, configuration, catalog, or
  status commands. Use MCP `get_status` while the server is running.
- Existing Telegram sessions and TOML read-policy settings remain usable.

## Earlier source development

M0–M2 added reliable retrieval, public-channel discovery, topic and pinned-message
navigation, counters, reactions, polls, and bounded album retrieval. R1 separated
domain, application, adapters, and bootstrap while preserving all tool contracts.
See the [completed specifications](docs/specs/README.md) and
[architecture decision](docs/architecture.md) for those implementation records.
