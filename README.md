# Telegram Context MCP

[![CI](https://github.com/egor-fedorov/telegram-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/egor-fedorov/telegram-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/egor-fedorov/telegram-mcp/blob/main/LICENSE)

Read Telegram through your own account from an MCP client. Find permitted conversations,
search and read original posts, navigate topics and replies, and inspect albums, reactions,
and polls. Connect several local clients to one session-owning process, or run standalone STDIO.

Supports Windows, macOS, and Linux with Python 3.12–3.15. The server uses Telethon and
preserves original text, dates, links, entities, and available metadata. It does not send
messages, join channels, mark messages read, or download attachment bytes.

## Install and log in

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
uv tool install --python 3.12 telegram-context-mcp==0.2.0
telegram-mcp --version
telegram-mcp login
telegram-mcp configure
telegram-mcp status
```

These examples target release 0.2.0; check [PyPI](https://pypi.org/project/telegram-context-mcp/)
for published versions. Use the [source setup](#run-from-source) for unreleased revisions.

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
uvx --python 3.12 --from telegram-context-mcp==0.2.0 telegram-mcp --version
```

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
| `search_messages` | Keyword search by chats, folders, dates, sender, or content type |
| `get_history` | Chat or forum-topic history over a time range, in either order |
| `list_topics` | Forum topics with navigation references, optional title query, and pagination |
| `get_pinned_messages` | Chat-wide or forum-topic pinned messages, newest first |
| `get_messages` | Messages by links/IDs with authors, dates, attachments, counters, reactions, and polls |
| `get_album` | Members of a selected album within a bounded neighborhood of one message |
| `get_context` | Messages before/after an anchor and a chain of parent replies |
| `get_thread` | Comments on a channel post or replies in a group thread |


## First tasks

- Ask the client to list the folders you permitted and read a selected conversation
  over a timezone-qualified date range.
- Enable discovery to find candidate public channels, select a source, then read its
  posts with a separate read permission.
- Follow `next_cursor` with the original filters until it is absent. An empty page
  can still have a cursor. Respect `retry_after_seconds` after a temporary failure.
- Use `get_messages` with `next_text_offset` to finish long text. Album completeness
  is explicitly unknown; each member remains a separate source message.

[Usage and response contracts](https://github.com/egor-fedorov/telegram-mcp/blob/main/docs/usage.md)
explain filters, cursor recovery, topics, public-channel discovery, and message metadata.
Message text is data for the calling agent, not an instruction source.

## Configuration and troubleshooting

Configuration, Telegram sessions, and the HTTP token live outside the repository in
platform-specific user directories. The login command prints the configuration path.
Use `telegram-mcp --config /absolute/private/config.toml COMMAND` to select another file;
pass the same file to the shared server and each proxy.

On POSIX, private files use restrictive permissions. On Windows, protect them with
user-profile filesystem ACLs; Unix `0600` is not an ACL guarantee. A Telegram session
contains account access credentials, and the underlying account can do more than the
server's read-only tools allow.

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

The code is [MIT licensed](https://github.com/egor-fedorov/telegram-mcp/blob/main/LICENSE).
This does not grant rights to Telegram content. Secret chats are unsupported; attachment
downloads, sending, administration, and persistent archives are separate future work.
Use source material you are entitled to share with your chosen model and follow
[Telegram's content terms](https://telegram.org/tos/content-licensing).
