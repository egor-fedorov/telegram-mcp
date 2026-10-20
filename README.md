# Telegram Context MCP

A local MCP server for reading Telegram through your user account.
Python 3.12, Telethon, the official MCP Python SDK 2, Pydantic, uv.

See [ROADMAP.md](ROADMAP.md) for the development plan and the next stage.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_status` | Authorization, connection, permitted sources |
| `list_folders` | Folders, IDs, order, inclusion/exclusion rules |
| `list_chats` | Chats filtered by name, type, or folders |
| `get_chat` | Description and linked discussion group |
| `search_messages` | Keyword search by chats, folders, dates, sender, or content type |
| `get_history` | History over a time range, in chronological or reverse order |
| `get_messages` | Messages by links/IDs with authors, dates, and attachment metadata |
| `get_context` | Messages before/after an anchor and a chain of parent replies |
| `get_thread` | Comments on a channel post or replies in a group thread |

Codex produces summaries and answers from the source messages.
Message text is treated as data, not as instructions to the agent.

## Installation and login

From the project directory:

```sh
uv sync --locked
uv run --locked telegram-mcp login
uv run --locked telegram-mcp configure
uv run --locked telegram-mcp status
```

1. Obtain `api_id` and `api_hash` from the API development tools section at
   [my.telegram.org](https://my.telegram.org).
2. `login` prompts for them locally, followed by your phone number, login code,
   and 2FA password if enabled. The code and 2FA password are not saved in the configuration.
3. `configure` displays folder IDs and prompts you to select permitted folders/chats,
   then separately opt in to reading explicitly specified public channels (`y`/`n`).
   Enter preserves the current value of each setting.
   By default, message access is not permitted for any chat.
4. `status` should show `authorized: true` and your selected sources.

Log in yourself in a terminal; do not share login codes in a chat with the model.
The local `catalog` command displays metadata for all account dialogs and folders
so you can choose IDs for the configuration. It does not output message text:

```sh
uv run --locked telegram-mcp catalog
```

The configuration and Telegram session are stored outside the repository in user
paths provided by `platformdirs`. The `login` command prints the exact configuration path.
Configuration and session files are created with `0600` permissions. The session contains
an account access key. The server enforces read restrictions; the session itself has
user-client permissions.

To use a custom path, place `--config /absolute/private/config.toml` before the command
name. The environment variables `TELEGRAM_MCP_CONFIG`, `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH`, and `TELEGRAM_SESSION_PATH` are supported and override TOML values.
See `config.example.toml`; chat IDs are strings.
`allow_all_chats = true` grants access to all chats instead of using an allowlist.
`allow_public_channel_reads = true` separately permits explicitly specified public
channels outside the allowlist; this setting is disabled by default. It does not expand
`list_chats`, searches without `chat_ids`, or access to private dialogs or groups.
Public-channel status is checked using fresh metadata: the chat must be a broadcast channel
with a primary or active additional username. The server does not automatically join channels.
Configuration changes take effect after restarting the MCP server.

## Connecting to Codex

In Settings → MCP servers → Add server, select STDIO:

Below, `/absolute/path/to/telegram-mcp` is a placeholder: replace it with the absolute path
to your copy of the project.

- Command: `/absolute/path/to/telegram-mcp/.venv/bin/telegram-mcp`
- Arguments: `serve`

Alternatively, add this to your existing Codex `config.toml`, preserving other settings:

```toml
[mcp_servers.telegram]
command = "/absolute/path/to/telegram-mcp/.venv/bin/telegram-mcp"
args = ["serve"]
startup_timeout_sec = 20
tool_timeout_sec = 120
enabled_tools = [
  "get_status", "list_folders", "list_chats", "get_chat", "search_messages",
  "get_history", "get_messages", "get_context", "get_thread"
]
```

Update the absolute path if you move the project. The server starts even before login:
tools remain discoverable, and `get_status` returns `AUTH_REQUIRED`.
Diagnostic logs go to stderr; stdout is reserved for the MCP protocol.

Only one process can use a Telegram session at a time. If you receive `SESSION_IN_USE`,
stop the previous MCP process before running another command with the same session.
This also applies to `login`, `configure`, `catalog`, and `status` in another terminal.
Use the `get_status` MCP tool while Codex is running.

`get_status` checks authorization with a fresh request to Telegram. The `authorized`
field is `true` if authorization is confirmed, `false` if it is missing or revoked,
and `null` if the check cannot be completed, for example because of a network error.
The `error` field explains the reason; `connected` reports connection state separately.
The response also includes `allow_public_channel_reads` and the other access settings.

## Usage

To find the folder named `Work` and list its channels, call `list_folders(query="Work")`,
then `list_chats` with its ID in `folder_ids` and `chat_type="channel"`.

Example `search_messages` arguments (the folder IDs are illustrative):

```json
{
  "query": "monetization",
  "folder_ids": [2, 5],
  "date_from": "2026-09-01T00:00:00+03:00",
  "date_to": "2026-09-08T00:00:00+03:00",
  "limit": 30
}
```

`get_history` accepts exactly one source parameter: the existing `chat_id` or the new
`chat_ref` (a numeric ID, `@username`, `https://t.me/username`, or `https://t.me/s/username`).
`get_chat` supports the same references. For example, to read a known channel's history:

```json
{
  "chat_ref": "https://t.me/s/example_channel",
  "date_from": "2026-09-01T00:00:00+03:00",
  "date_to": "2026-09-08T00:00:00+03:00",
  "order": "oldest_first",
  "limit": 30
}
```

The channel must be within the permitted scope, or it must be public with
`allow_public_channel_reads` enabled. In `search_messages`, this additional access applies
only to explicit `chat_ids`, which still intersect with any selected folders.
If Telegram does not yet have an entity for a numeric ID, use a username or link first.
When continuing, history remains bound to the original canonical ID. If the username has
been reassigned to another channel, continuation returns `CURSOR_MISMATCH` instead of
switching to the new source.

`get_messages`, `get_context`, and `get_thread` accept a Telegram link, a
`chat_id:message_id` string, or an object such as `{"chat_id": "…", "message_id": 123}`.
Public/private links, links to messages in topics, and `?comment=…` are supported.
Numeric chat_id values use Telethon marked peer IDs; message_id is the Telegram
message identifier, not an internal TDLib ID.

`get_messages` fetches messages in batches grouped by chat, preserving input order
and duplicates. Issues include `input_index`, the zero-based index of the original item,
and, once the reference has been resolved, `message_ref`. `MESSAGE_UNAVAILABLE` means
the message is unavailable; it does not confirm deletion. A failure in one chat does
not discard results from other chats.

`forward_origin` stores the original forwarding source and the author's signature
in `post_author`. Information about the source of a saved forward is stored separately:
`saved_from_ref`, `saved_from_sender_id`, `saved_from_name`, and `saved_at`.
Unknown fields are `null`; no additional requests are made to forwarding sources.

## Retrieval rules

- Folders form a union without duplicates. `chat_ids` intersect with selected folders.
  An omitted selector means no additional filter; an empty list is an error.
- Server restrictions apply in addition to these filters, including linked discussions
  and parent replies. To read channel comments, permit the linked group as well.
  Public-channel permission applies only to the current request and does not grant
  access to the linked group or replies from another chat.
- Folder rules account for categories, pinned/explicitly included chats, exclusions,
  archive status, mute/unread state, and unread mentions according to Telegram Desktop rules.
- Dates must include a timezone; the interval is `[date_from, date_to)`.
  History/search fixes the upper time boundary on the first request.
- Search uses Telegram's native keyword search. The server additionally filters by
  date range and content type. There is no semantic index yet.
- Pagination freezes the selected set of chats. Continuation checks current access,
  including the public status of channels outside the allowlist. Revoked access causes
  the call to fail. If catalog/access checks fail, buffered messages are not returned;
  retry the original cursor after resolving the cause.
- `scope.complete` means the entire selection has been traversed without errors.
  Keep calling with `next_cursor` until it becomes null. Preserve the original filters;
  `limit` may change.
- With many chats, an empty page may include `next_cursor`: the server is loading
  initial source pages to preserve the global ordering of messages by time.
- Individual chat failures appear in `issues`. Temporary errors `RATE_LIMITED`,
  `CONNECTION_ERROR`, and `TELEGRAM_UNAVAILABLE` preserve the position and `next_cursor`,
  while `scope.complete` remains `false`. Repeat the original arguments with this cursor;
  for FloodWait, first wait for `retry_after_seconds`. The server does not wait inside
  the call or retry the failed stream within the same response.
- Merged output pauses if a temporarily unavailable source prevents the server from
  guaranteeing global order. Once continuation succeeds, that source's temporary issue
  disappears. Permanent issues remain in the result while other sources continue;
  retrying a permanent failure requires a new request.
- Cursors last 15 minutes and are bound to the process. Up to 128 checkpoints are stored
  within a 64 MiB serialized-data budget; older checkpoints are evicted. Start again after
  expiry, eviction, or a restart. If FloodWait exceeds the cursor lifetime, wait until
  the cooldown ends and start a new selection.
- Message text shares a total response budget. `text_total_length`, `text_offset`, and
  `next_text_offset` let you retrieve remaining text through `get_messages`. Text offsets
  count Unicode code points; `entities` retain Telegram UTF-16 offsets into the full original.
- This is not a transactional archive: messages may change or disappear during traversal.

## Checks

```sh
make check
```

This runs Ruff format/check, `ty check`, and pytest. Tests use no account and make no
requests to Telegram. They cover folders, pagination, dates, context, access, Telethon
structures, configuration, and the actual MCP protocol, including a separate STDIO process.
Both modern MCP mode and compatibility with the initialize handshake are tested.

## First-version boundaries

Reading cloud chats is implemented. Automatic joining, paid requests, sending,
attachment downloads, voice transcription, folder management, and a semantic index
remain possible extensions. Attachments are represented by metadata. Secret chats are
not supported. Testing against real chats requires your local login.

Use this integration for material you are entitled to share with the model.
[Telegram's content and AI terms](https://telegram.org/tos/content-licensing)
also apply to data obtained through the API.

## Sources

- [Telethon](https://docs.telethon.dev/en/stable/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Telegram folders](https://core.telegram.org/api/folders)
- [Telegram Desktop folder matching](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/data/data_chat_filters.cpp)
- [ty](https://docs.astral.sh/ty/)
