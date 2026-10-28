# Changelog

Public changes to `telegram-context-mcp`. The command name is `telegram-mcp`.

A version entry describes its target artifact. Verify publication on
[PyPI](https://pypi.org/project/telegram-context-mcp/) and
[GitHub Releases](https://github.com/egor-fedorov/telegram-mcp/releases).

## [Unreleased]

### Distribution

- Optional Linux systemd user unit and installation/lifecycle guide for the shared
  HTTP server. It runs a persistent installed executable, restarts after crashes,
  and preserves the existing local authentication and session ownership model.
  The unit works with published 0.5.1; no package upgrade is required to adopt it.

## [0.5.1] - 2026-10-28

Security patch for 0.5.0; upgrading is recommended for every installation.

### Security

- Telegram service notifications (`777000`), which deliver login codes, are never
  readable: all-chat and folder access exclude them, explicit reads and monitoring,
  export or download requests return `ACCESS_DENIED`, and configuration that
  allowlists them is rejected. Previously `allow_all_chats` or a folder containing
  this chat let a prompt-injected agent read a login code.
- Access checks reject noncanonical numeric chat IDs before all-chat, explicit,
  public-channel or same-source grants. Unicode-digit aliases previously bypassed
  the service-notification deny rule in message and context reads. `allowed_chat_ids`
  also requires ASCII decimal IDs without leading zeros or extra characters.

### Development

- R6 structural consolidation: MCP tools and wire DTOs are organized by feature, complex
  query, change, watch, storage and Telegram reader functions are split into named steps,
  and Ruff enforces a McCabe complexity limit of 15. Tool schemas, descriptions, error
  codes and persisted formats are unchanged.
- Tests run in parallel through pytest-xdist; `make test-fast` skips tests marked `slow`.
  New fixtures pin tool order and descriptions, wire/domain field parity and 0.5 state.

### Compatibility and validation

- Tools, prompts, schemas and persisted state are unchanged; no client refresh is needed.
  Upgrade the shared server and client proxies together and restart the server.
- A configuration whose `allowed_chat_ids` contains `777000` now fails with
  `INVALID_CONFIGURATION`; remove that entry or run `telegram-mcp configure`.
- Noncanonical allowlist IDs also fail configuration validation. Use the ASCII decimal
  ID returned by Telegram MCP, without leading zeros, a plus sign or whitespace.
- See the [validation record](docs/validation-0.5.1.md) for offline and
  selected-source checks.

## [0.5.0] - 2026-10-27

### Added

- `get_history` reads several selected chats (`chat_ids`) or folders (`folder_ids`) over
  a fixed `[date_from, date_to)` period in one traversal. It defaults to early
  `ordering="page"` results and cumulative `max_sources=20` / `max_messages=100` budgets;
  a larger selection returns `SOURCE_LIMIT_EXCEEDED` before any history read, and a
  typed `budget` object reports exhaustion while keeping the cursor resumable.
- `get_watch_changes(save=true)` writes each pending batch to a permanent JSONL copy with
  `metadata.json` (watch, batch and sources) before it can be acknowledged and returns
  `saved_file`. Retries, lost responses, restarts and crashes reuse the same copy;
  `WATCH_ARCHIVE_PENDING` and `ARCHIVE_COPY_INVALID` keep positions unchanged.
- MCP prompts `watch_digest` and `period_digest` for source-linked digests, with
  explicit trust rules for Telegram content, bounded workflows and a documented vacancy
  preset; completion for `watch_digest.watch_name` from local storage only. Both are
  forwarded by `connect`.
- `timezone` configuration (IANA name, default `UTC`) for calendar dates in prompts.
- MCP Registry metadata for `io.github.egor-fedorov/telegram-mcp`, validated against
  the package version and published after PyPI through GitHub OIDC. A separate
  workflow retries Registry publication without rebuilding or uploading the package.
  The first Registry entry requires a release containing this integration.
- `telegram-context-mcp` as an alias of the existing `telegram-mcp` CLI, allowing
  Registry clients to use the package's default `uvx` executable.

### Compatibility and validation

- All changes are additive: existing tool arguments, single-chat history responses
  and `save=false` watches behave as in 0.4.0. Refresh client tool lists to see the new
  `get_history` and `get_watch_changes` arguments.
- Windows installations now include `tzdata`, because Windows has no system time-zone
  database.
- Upgrade the shared server and client proxies together, restart the server, and
  refresh client tools and prompts. `watch_digest` saves batches by default: configure
  `archive_directory`, or explicitly choose `save=false` and acknowledge after delivery.
- Selected-source live checks verified bounded multi-source reads, saved-batch recovery
  across restarts and acknowledgement, plus a Claude Code 2.1.310 digest workflow and
  watch-name completion. See the [validation record](docs/validation-0.5.md) for coverage.

## [0.4.0] - 2026-10-26

### Added

- Opt-in `download_attachment` for a selected photo or document up to 20 MiB, with
  SHA-256, a private local file and an MCP resource. The 100 MiB / 32-file cache
  expires after 15 minutes; reads recheck access, protection and media identity.
- `get_changes` for new messages and bounded recent revisions in one selected chat.
  Durable checkpoints survive restart without storing message bodies, retain
  FloodWait deadlines and report unavailable messages without claiming deletion.
- `export_history` and JSONL/Markdown output from `get_changes`, preserving original
  text, metadata and source links in bounded, restartable parts. History exports
  freeze their upper date/ID and omit protected or expiring records explicitly.
- Named chat/folder subscriptions through `create_watch`, `list_watches`,
  `get_watch_changes`, `ack_watch_changes` and `delete_watch`. Positions and one
  unacknowledged batch survive restart; polling is on demand. `ack_batch_id`
  acknowledges processed data and polls in one call, with safe duplicate retries.
- Optional `archive_directory` and `save=true` for permanent selected downloads and
  exports. Each copy includes versioned `metadata.json` provenance, original name,
  MIME type, checksum and export boundaries. Publication and rollback use bounded
  worker threads so disk writes do not block other calls.
- Offline `telegram-mcp checkpoints status/prune`, with preview by default and
  explicit flags for deleting protected positions under the session lease.
- Bounded opt-in live probes for selected downloads, checkpoints, exports and peer
  metadata, with redacted reports and disposable-state cleanup.

### Changed

- **Breaking response contract:** `scope.chat_ids` and `pending_chat_ids` preview at
  most 20 IDs, with exact counts and truncation flags. Authorization and continuation
  retain the full scope. See the [migration guide](docs/migration-0.4.md).
- **Changed search default:** restricted `search_messages` uses `ordering="page"`
  to return results before every source is initialized. Select `ordering="global"`
  for ordering across pages. Both traversal directions remain available.
- Dialog snapshots default to 300 seconds of reuse plus 300 seconds during one
  shared background refresh. Authorization and folder definitions remain fresh;
  ordinary connection failures preserve catalog progress and usable snapshots.
- STDIO, HTTP and `connect` share four active calls, 16 FIFO waiters and a
  120-second queue timeout. Independent incremental reads can overlap; subscription
  operations serialize only by name. Downloads remain serialized.
- Watches compare selected channel/supergroup heads and `pts` in batches of 100.
  Completed unchanged sources skip history reads; unsupported markers and observed
  polls retain ordinary revision checks. Creation accepts 20 explicit references,
  deduplicates aliases and has a 35-second deadline; folders cover larger selections.
- Current checkpoint heads retain 30-day protection. Equivalent positions reuse
  tokens; fresh starts protect one unused baseline per source and authorization.
  Consumed chains and independent branches remain protected. Version-3 migration
  conservatively retains ambiguous legacy roots; offline cleanup can recover a full store.
- Compact tool discovery omits optional output schemas by default while retaining
  typed validation and identical JSON responses. Set `mcp_schema_mode="full"` to
  advertise them. The full surface now contains 22 tools.
- Organized tests by layer and separated Telegram search, topic-history and album
  readers. Updated ty to 0.0.92 and Ruff to 0.17.2; Dependabot separates development
  tooling and retains manual review. CI/release actions use reviewed pinned updates.
- Updated locked MCP packages to 2.3.0, the HTTP stack and platformdirs after
  compatibility checks across STDIO, HTTP and the bridge.

### Security

- Updated locked PyJWT to 2.15.1 and OpenTelemetry API to 1.45.1 for reported
  vulnerabilities in transitive dependencies. Local HTTP still requires its bearer token.

### Compatibility and validation

- Existing Telegram sessions and source permissions remain supported. New downloads
  and permanent copies require explicit opt-ins. After upgrading, restart the server
  and refresh client tool schemas; do not downgrade a migrated checkpoint database.
- Offline checks cover Python 3.12–3.15 and all three transports. Selected-source live
  checks cover photo/video downloads, export integrity, durable monitoring and reconnect.
  See the [validation record](docs/validation-0.4.md) for measurements and coverage limits.

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
