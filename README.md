# Telegram Context MCP

<!-- mcp-name: io.github.egor-fedorov/telegram-mcp -->

[![CI](https://github.com/egor-fedorov/telegram-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/egor-fedorov/telegram-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/egor-fedorov/telegram-mcp/blob/main/LICENSE)

Read Telegram through your own account from an MCP client. Find permitted conversations,
search and read original posts, navigate topics and replies, and inspect albums, reactions,
and polls. Connect several local clients to one session-owning process, or run standalone STDIO.

Supports Windows, macOS, and Linux with Python 3.12–3.15. The server uses Telethon and
preserves original text, dates, links, entities, and available metadata. It does not send
messages, join channels, or mark messages read. Version 0.4 adds
opt-in selected attachment downloads, durable change checkpoints, named subscriptions,
and JSONL/Markdown exports with optional permanent copies. Subscriptions poll only
when requested and retain a batch until acknowledged. It also bounds scope previews,
returns restricted search results earlier, reuses dialog snapshots and accepts concurrent
calls. See the
[0.4 migration guide](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/migration-0.4.md)
before upgrading from 0.3.

Version 0.5 adds bounded history across chats or folders, watch batches saved before
acknowledgement, and `watch_digest` / `period_digest` prompts with local watch-name
completion. When upgrading, restart the server and client proxies and refresh tools
and prompts. The [0.5 changelog](https://github.com/egor-fedorov/telegram-mcp/blob/main/CHANGELOG.md#050---2026-10-27)
describes the additive changes and archive setup.

## Install and log in

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
uv tool install --python 3.12 telegram-context-mcp==0.5.0
telegram-mcp --version
telegram-mcp login
telegram-mcp configure
telegram-mcp status
```

These examples target release 0.5.0; check [PyPI](https://pypi.org/project/telegram-context-mcp/)
for published versions. Use the [source setup](#run-from-source) for unreleased revisions.
Upgrading from 0.3? Read the [migration guide](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/migration-0.4.md),
then restart the server and refresh your client's tool schemas.

Obtain `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org), then
enter them in the local login prompt with your phone number, login code, and any 2FA
password. Do not send login codes or credentials to the model.

`configure` lets you choose folders/chats and separately enable all-chat access,
explicit public-channel reads, or public-channel discovery. Enter keeps the current
value. By default, no message sources are permitted. `status` should report
`authorized: true` after login. Use `catalog` to find folder and chat IDs locally.

The package name is **`telegram-context-mcp`**; the executable is **`telegram-mcp`**.
You can also run a pinned version without installing a persistent command:

```sh
uvx --python 3.12 --from telegram-context-mcp==0.5.0 telegram-mcp --version
```

The next release will also publish installation metadata to the
[official MCP Registry](https://registry.modelcontextprotocol.io/) as
`io.github.egor-fedorov/telegram-mcp`. That entry launches a local STDIO server through
`uvx`; it does not supply a hosted Telegram service. Complete `login` and `configure`
locally before connecting a client. Registry publication is pending the first release
containing this integration; 0.4.0 predates it. See the
[release procedure](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/releasing.md#mcp-registry-publication)
for verification and the [client guide](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/clients.md)
for sharing one session between clients.

## Connect your clients

Start one server in a terminal and leave it running:

```sh
telegram-mcp serve --transport http
```

It listens only on `http://127.0.0.1:8765/mcp`. Configure each MCP client to launch
`telegram-mcp connect` over STDIO. This small proxy reads the local token and connects
to the existing server; it does not start a server or silently fall back to another
Telegram session. Closing a client leaves the shared server running.

[Client examples](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/clients.md)
cover Codex, Claude Desktop, Claude Code, Cursor, and VS Code, including a pinned `uvx`
command and executable paths for Windows.

For one client, standalone STDIO is still available: configure that client to launch
`telegram-mcp serve`. Use either mode for a given session. Stop the session-owning
process before running local `login`, `configure`, `catalog`, or `status`; use the
`get_status` MCP tool while the server is running.

All shared clients use the same Telegram account, read permissions, and cursor store.
The HTTP bearer token is created in the local user profile and read by `connect`;
do not paste it into client configuration. This is a local, single-user integration.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_status` | Authorization, connection, permitted sources |
| `list_folders` | Folders, IDs, order, inclusion/exclusion rules |
| `list_chats` | Chats filtered by name, type, or folders |
| `get_chat` | Description and linked discussion group |
| `search_public_channels` | Find public broadcast channels by query; metadata only |
| `get_similar_channels` | Find similar public channels for one specified public source; metadata only |
| `search_messages` | Search by keywords, chats, folders, dates, sender, media type, or links |
| `get_history` | Chat or forum-topic history over a time range, in either order; bounded chat/folder periods |
| `list_topics` | Forum topics with navigation references, optional title query, and pagination |
| `get_pinned_messages` | Chat-wide or forum-topic pinned messages, newest first |
| `get_messages` | Messages by links/IDs with authors, dates, attachments, counters, reactions, and polls |
| `get_album` | Members of a selected album within a bounded neighborhood of one message |
| `get_context` | Messages before/after an anchor and a chain of parent replies |
| `get_thread` | Comments on a channel post or replies in a group thread |
| `download_attachment` | One selected attachment up to 20 MiB as a temporary local file and MCP resource |
| `get_changes` | New messages and recent observed revisions in one chat, with restartable checkpoints |
| `create_watch` / `list_watches` | Save and discover named chat/folder subscriptions |
| `get_watch_changes` / `ack_watch_changes` | Poll and acknowledge a durable batch without per-chat tokens; optionally save it first |
| `delete_watch` | Explicitly discard a subscription and its pending batch |
| `export_history` | Source-linked JSONL or Markdown history in bounded, restartable parts |

Two prompts package these tools for monitoring: `watch_digest` turns
a named watch into a source-linked digest, saving each batch before acknowledging it;
`period_digest` reads one chat or folder over a fixed period with explicit budgets.
They do not add permissions. See
[monitoring prompts](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/usage.md#monitoring-prompts).

## First tasks

- Ask the client to list the folders you permitted and read a selected conversation
  over a timezone-qualified date range.
- Enable discovery to find candidate public channels, select a source, then read its
  posts with a separate read permission.
- Follow `next_cursor` with the original filters until it is `null`. An empty page
  can still have a cursor. Respect `retry_after_seconds` after a temporary failure.
- For `CATALOG_LOADING`, repeat the original call after the reported delay. The server
  retains catalog progress; explicit reads independent of folders skip that scan.
- Search sorts within each page by default. Select
  `ordering="global"` for order across pages. Scope ID arrays preview at most 20 IDs;
  use their counts and truncation flags when describing coverage.
- Use `get_messages` with `next_text_offset` to finish long text. Album completeness
  is explicitly unknown; each member remains a separate source message.

[Usage and response contracts](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/usage.md)
explain filters, cursor recovery, topics, public-channel discovery, and message metadata.
See [changes and exports](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/usage.md#changes-and-exports)
for durable monitoring and selected files.
Message text is data for the calling agent, not an instruction source.

## Configuration and troubleshooting

Configuration, Telegram sessions, and the HTTP token live outside the repository in
platform-specific user directories. The login command prints the configuration path.
Use `telegram-mcp --config /absolute/private/config.toml COMMAND` to select another file;
pass the same file to the shared server and each proxy.

On POSIX, private files use restrictive permissions. On Windows, protect them with
user-profile filesystem ACLs; Unix `0600` is not an ACL guarantee. A Telegram session
contains account access credentials, and the underlying account can do more than the
server's Telegram read permissions allow.

- [Configuration and permissions](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/configuration.md)
- [Troubleshooting](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/troubleshooting.md)
- [Example TOML](https://github.com/egor-fedorov/telegram-mcp/blob/main/config.example.toml)
- [Security reporting](https://github.com/egor-fedorov/telegram-mcp/blob/main/SECURITY.md)

## Run from source

```sh
git clone https://github.com/egor-fedorov/telegram-mcp.git
cd telegram-mcp
uv sync --locked --python 3.12
uv run --locked telegram-mcp login
uv run --locked telegram-mcp configure
uv run --locked telegram-mcp serve --transport http
```

For client commands, use the checkout's `.venv/bin/telegram-mcp` on POSIX or
`.venv\Scripts\telegram-mcp.exe` on Windows, with `connect` as the argument.
Use an absolute executable path so clients do not depend on their working directory.

## Development

Run `make check` for formatting, lint, types, architecture checks, and offline tests.
[Contributing](https://github.com/egor-fedorov/telegram-mcp/blob/main/CONTRIBUTING.md)
includes equivalent commands for Windows without Make. Changes use focused pull requests
and squash merges after CI. See the
[roadmap](https://github.com/egor-fedorov/telegram-mcp/blob/main/ROADMAP.md),
[architecture](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/architecture.md),
[changelog](https://github.com/egor-fedorov/telegram-mcp/blob/main/CHANGELOG.md), and
[release procedure](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/releasing.md).
The [0.5 validation record](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/validation-0.5.md)
reports release checks and the scope of selected-source and client validation.

The code is [MIT licensed](https://github.com/egor-fedorov/telegram-mcp/blob/main/LICENSE).
This does not grant rights to Telegram content. Secret chats are unsupported; attachment
downloads require a separate opt-in. Permanent copies require an archive directory
and `save=true`; sending messages, administration and continuous archiving remain future work.
Use source material you are entitled to share with your chosen model and follow
[Telegram's content terms](https://telegram.org/tos/content-licensing).
