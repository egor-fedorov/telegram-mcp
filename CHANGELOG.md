# Changelog

Public changes to `telegram-context-mcp`. The command name is `telegram-mcp`.

A version entry describes its target artifact. Verify publication on
[PyPI](https://pypi.org/project/telegram-context-mcp/) and
[GitHub Releases](https://github.com/egor-fedorov/telegram-mcp/releases).

## [Unreleased]

## [0.3.0] - 2026-10-22

### Added

- Automatic bounded reconnection after a lost Telegram connection, with fresh
  authorization checks and retry deadlines.
- A 30-second completed-catalog cache and resumable, bounded cold loads that return
  `CATALOG_LOADING`; direct reads independent of folders bypass catalog scans.
- Account-dialog global search with all-chat permission, fair restricted-source preparation with progress,
  native URL/photo/voice/audio filters, `has_link`, and webpage preview metadata.
- Typed responses for all 14 tools, deterministic payload/schema size regression
  checks, and an opt-in bounded live-validation script for explicitly selected sources.

### Changed

- **Breaking output contract:** message references are canonical strings; message
  responses share brief chat summaries; unknown optional metadata is omitted.
  `list_folders` returns `{folders: [...]}`. Full compact JSON appears in both text
  and structured content. See [migration guidance](docs/migration-0.3.md).
- Restricted search uses numeric chat IDs for same-second ordering; global search keeps
  Telegram's order within equal timestamps. Search preserves its selected
  search engine, date ceiling, and scope across continuation calls.
- Raw message mapping no longer calls Telethon's private `_finish_init` method.
  Preview images/files no longer become standalone message attachments.

### Compatibility and validation

- Existing read permissions, input references, session files, STDIO, local HTTP, and
  the bridge remain supported. Global search is restricted to all-chat access without
  chat, folder, or sender selectors. No paid public-post search is used.
- Catalog reuse can delay folder membership changes; public-channel grants still use
  fresh metadata. Expired/partial catalogs do not grant access.
- The fixed 30-post response preserves all text while reducing the complete MCP
  result from 83,588 to 32,075 bytes (61.6%); `tools/list` falls from 110,590 to
  77,653 bytes (29.8%).
- Selected-source live checks covered history, keyword/link/media searches,
  explicit messages, context, albums, and reconnect. Global search, catalog loads,
  forums, and authorization revocation were not live-tested. See the
  [validation record](docs/validation-0.3.md) for evidence and coverage limits.

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
