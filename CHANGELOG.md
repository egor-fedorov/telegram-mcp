# Changelog

Public changes to `telegram-context-mcp`. The command name is `telegram-mcp`.

A version entry describes its target artifact. Verify publication on
[PyPI](https://pypi.org/project/telegram-context-mcp/) and
[GitHub Releases](https://github.com/egor-fedorov/telegram-mcp/releases).

## [Unreleased]

### Added

- `get_changes` for one explicitly selected chat: current-head baselines, new messages,
  bounded recent revision checks, explicit unavailability and durable immutable checkpoints.
  Checkpoints retain positions/fingerprints without message bodies, survive restart,
  recheck access and preserve M4 FloodWait deadlines across retries and restarts.
- `export_history` and file output from `get_changes`, with versioned JSONL and readable
  Markdown, full original text/metadata and source links. History parts freeze their
  upper date/ID and resume with checkpoints. Protected/expiring records are omitted explicitly.
- Temporary UTF-8 export resources through STDIO, HTTP and `connect`, with SHA-256,
  20 MiB parts, a separate 100 MiB / 32-file cache, 15-minute expiry and cleanup.
  The original M4 implementation preserved the previous 15 tool contracts; the 0.4
  changes below apply to the combined development tree. Published 0.3.0 is unchanged.

- Opt-in `download_attachment` for one selected file up to 20 MiB, with a private
  temporary local path, SHA-256, and an MCP resource available through STDIO, HTTP,
  and `connect`. Resource reads recheck access, source protection and media identity.
- Bounded storage (100 MiB / 32 files), 15-minute expiry, cleanup, controlled Telegram
  file-reference recovery, and FloodWait cooldowns. Published 0.3.0 is unchanged.

### Changed

- **Breaking 0.4 contract:** scope ID arrays are previews of at most 20 entries, with
  exact selected/pending counts and explicit truncation flags. Internal authorization
  and continuation retain the full scope. See [migration guidance](docs/migration-0.4.md).
- Restricted `search_messages` defaults to `ordering="page"` for early, fairly traversed
  results sorted within each page; `ordering="global"` retains strict merging. Responses
  report the actual guarantee and `progress.sources_started`. Both order directions are
  exposed in MCP. Native unrestricted global search keeps its existing access rules.
- Dialog cache defaults to 300 seconds plus 300 seconds during background refresh.
  Current folder definitions and authorization are checked on each dependent call.
  One shared worker retains scan progress; normal network failures preserve valid
  snapshots. Cold/hard-expired reads return `CATALOG_LOADING` after a bounded wait.
- STDIO, HTTP and `connect` now share four active tool/resource slots, a FIFO queue of
  16 calls and a 120-second queue timeout. Cancellation removes queued work; overflow
  or timeout returns `SERVER_BUSY` with a five-second retry delay.
- Set the source version to `0.4.0.dev0`, without a release tag or PyPI publication.
- Separated Telegram search, topic-history and album-window readers while retaining
  the shared runtime, entity cache, pagination behavior and MCP contracts.
- Organized tests by layer with explicit shared helpers; refreshed the architecture
  guide, shortened the roadmap and added navigation to the usage guide.
- Updated development checks to ty 0.0.92 and Ruff 0.17.2, with current async
  context-manager annotations. Dependabot separates development tools from other library updates;
  updates continue to require manual review and passing CI.
- Updated the locked MCP packages to 2.3.0, HTTP libraries and platformdirs after
  compatibility review, preserving the versioned tool contracts and all transports.
- Updated SHA-pinned setup-uv and artifact upload/download actions in CI and release workflows.

### Security

- Updated locked PyJWT to 2.15.1 and OpenTelemetry API to 1.45.1 to address the
  reported vulnerabilities in MCP's transitive dependencies. The local HTTP server
  continues to authenticate with its configured bearer token.

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
[current architecture](docs/architecture.md) for the maintained design.
