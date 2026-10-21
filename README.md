# Telegram Context MCP

[![CI](https://github.com/egor-fedorov/telegram-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/egor-fedorov/telegram-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A local MCP server for reading Telegram through your user account.
Python 3.12, Telethon, the official MCP Python SDK 2, Pydantic, uv.

See [ROADMAP.md](ROADMAP.md) for the development plan and the next stage, and
[Architecture](docs/architecture.md) for layer boundaries and implementation guidance.

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
   then separately opt in to reading explicitly specified public channels and discovering
   public-channel metadata (`y`/`n` for each).
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

`allow_public_channel_discovery = true` permits public-channel search and similar-channel
metadata. It is disabled by default and independent of all read permissions, including
`allow_all_chats` and `allow_public_channel_reads`. When disabled, both discovery tools return
`FEATURE_DISABLED` before contacting Telegram, including authorization or source checks.
Discovery does not permit reading posts or add a channel to the allowlist. It does not expand
`list_chats`, message searches without explicit sources, or access to linked discussions.
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
  "get_history", "get_messages", "get_album", "get_context", "get_thread",
  "search_public_channels", "get_similar_channels", "list_topics", "get_pinned_messages"
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
The response also includes `allow_public_channel_reads`, `allow_public_channel_discovery`,
and the other access settings.

## Usage

### Discover, select, and read

Enable `allow_public_channel_discovery` locally, restart the server, and then:

1. Call `search_public_channels(query="technology", limit=20)` to find candidate channels.
2. Review their titles and public URLs, then select the sources relevant to your question.
   Call `get_similar_channels(chat_ref="https://t.me/example_channel", limit=20)` to expand
   the candidates around a selected public channel. Replace the example reference with a
   returned channel ID, username, or link. Recommendations always require a source;
   personalized recommendations without a source are not supported.
3. Use `get_history` with a selected channel's ID or URL and a date range to read its posts,
   then cite original messages when comparing sources. Reading still requires the existing
   allowlist, `allow_all_chats`, or `allow_public_channel_reads`; discovery alone is not enough.

Both discovery tools default to `limit=20` and accept limits from 1 to 100. Search queries
must be nonempty after trimming whitespace and contain at most 1,000 characters. Invalid
queries and limits are rejected as argument errors; Telegram's rejection of an empty
or too-short query returns `INVALID_ARGUMENT`.
Only accessible public broadcast channels are returned. Users, bots, groups, private
channels, and forbidden channels are excluded. A recommendation source is checked using
fresh metadata and must satisfy the same public-channel criteria. If a numeric ID cannot
be resolved, use its username or link.

Each `PublicChannel` contains `id`, `title`, an active `username`, public `url`, and
`participants_count`. A participant count unavailable in Telegram's response is `null`,
not zero. Discovery does not invent dialog state such as mute, unread, or folder membership.
Each `DiscoveryResult` contains:

| Field | Meaning |
| --- | --- |
| `channels` | Eligible channels, deduplicated by ID in Telegram's result order |
| `returned_count` | Actual number of channels returned |
| `telegram_total_count` | Telegram's recommendation count when provided; otherwise `null` |
| `locally_truncated` | Whether eligible results were cut to the requested `limit` locally |
| `exhaustive` | Always `false`; discovery does not cover all matching Telegram channels |

Neither tool has a cursor. Filtering may leave fewer results than requested; the server
does not issue extra searches to fill the response. Recommendations use Telegram's available
results and apply `limit` locally. Telegram's total count and server-imposed limits do not
describe a complete directory, and a missing count does not imply zero results.

Authorization, FloodWait, network, and server failures are reported as errors, not empty
results. After temporary `RATE_LIMITED`, `CONNECTION_ERROR`, or `TELEGRAM_UNAVAILABLE`
errors, retry with a new tool call. Expected tool failures include JSON in the MCP error
text with `code`, `message`, and, when supplied, `retry_after_seconds`; respect that delay.
Discovery does not join channels, make payments, fetch candidate posts automatically,
or perform global post search.

### Read permitted sources

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

`get_messages`, `get_album`, `get_context`, and `get_thread` accept a Telegram link, a
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

### Read message counters, reactions, and polls

All message-returning tools include available metadata from the same Telegram message
payload. This also applies to context, replies, selected-topic history, and pins.
No additional requests are made for counters, reactions, poll results, or participant
identities. The tools do not vote, react, send Stars, or mark messages read.

| Message field | Meaning |
| --- | --- |
| `view_count` | Telegram's message view count, or `null` when unavailable |
| `forward_count` | Telegram's message forward count, or `null` when unavailable |
| `reply_count` | Telegram's reply/comment count, or `null` when unavailable |
| `reactions` | Aggregate reaction entries and their `is_min` flag, or `null` |
| `poll` | Poll definition and available result metadata, or `null` |

An explicit count of `0` is retained; missing counts are never inferred as zero.
Counters, reactions and poll results can change between calls. Their presence does not
grant access to linked comments or other messages.

`reactions.results` contains each reaction's `type` and `count`. Standard emoji use
`type="emoji"` and `emoji`; custom emoji use `type="custom_emoji"` and a string
`document_id`. Paid reactions use `type="paid"`: their count measures **Stars, not
people**. Unsupported and empty reaction types retain their counts without inventing
an emoji. `is_chosen` and `chosen_order` describe the current account's selection only
when Telegram supplies a full result for an ordinary or custom emoji. An `is_min=true`
summary is partial: its personal selection fields remain `null`. Paid reactions also
leave personal selection fields `null`. `reactions_as_tags=true` identifies reactions
used as Saved Messages tags. Reactor lists are not returned.

`poll` contains a string `id`, formatted `question`, ordered `answers`, state flags,
and optional `close_period_seconds` and `closes_at`. Each answer has formatted `text`
and an `option_id`: a lowercase hexadecimal encoding of Telegram's opaque option bytes.
Use this ID to match results to options; titles and positions are not stable identifiers.
Polls keep `content_type="other"` for compatibility with existing filters.

`poll.results` is `null` when unavailable. When present, it contains `is_min`, optional
`total_voters`, an optional `answers` result vector, and an optional formatted `solution`.
The result vector is kept separate from the poll's option list: `null` means the vector
was absent, while `[]` means Telegram supplied an empty vector. Each entry keeps its
`option_id`, nullable `voter_count`, `is_chosen`, and `is_correct`; do not infer zero votes
for options absent from the result. An `is_min=true` result leaves `is_chosen` unknown.
Quiz correctness remains `null` until Telegram reveals a correct answer; ordinary polls
have no quiz correctness. `public_voters` describes the poll's setting but does not expose
voter identities. Recent voters, reactors, reply participants, and answer authors are
not included in this metadata.

Formatted poll text retains original Telegram entities, including their UTF-16 offsets
and available attributes such as URLs or custom emoji IDs. Message text continuation
through `next_text_offset` changes only the returned message-text slice; counters,
reactions, polls and original entities are preserved.

### Read a selected album

Pass a known album member to `get_album(ref, scan_limit=30)`. For example:

```json
{
  "ref": "https://t.me/example_channel/123",
  "scan_limit": 30
}
```

The source must satisfy the same access policy as `get_messages`. Explicit public
broadcast channels outside the allowlist require `allow_public_channel_reads`;
discovery alone does not grant access. Each call rechecks access and reads only the
resolved chat. Linked discussions and other chats are not followed.

`scan_limit` must be an integer from 1 to 100. The server reads the anchor and up to
that many neighboring messages on each side, then selects messages with the same
`album_id` in the same chat. Nonmembers count toward the scan limit and do not stop
the scan; unrelated messages can appear between album members. Duplicate references
are removed, and the result is ordered oldest first. The default window can return
at most 61 messages; the largest window can return at most 201.

`AlbumResult` contains:

| Field | Meaning |
| --- | --- |
| `anchor_ref`, `album_id` | Canonical source reference and the anchor's album identifier |
| `messages`, `order` | Observed members, including the anchor, in `oldest_first` order |
| `completeness` | Always `"unknown"`; Telegram does not provide an album manifest |
| `scan_limit` | Maximum neighboring messages inspected on each side |
| `scanned_before`, `scanned_after` | Telegram rows inspected, including nonmembers and unavailable records |
| `before_exhausted`, `after_exhausted` | `true` if that history boundary was reached, `false` if not established, `null` if failed or not attempted |
| `issues` | Failures encountered while reading neighboring messages |

History exhaustion does not prove album completeness: inaccessible or removed members
cannot be inferred, and a small window can miss distant members. The result has no
cursor. Increase `scan_limit` and repeat the call to inspect a wider window, while still
reporting completeness as unknown. Messages can change between calls.

An unavailable anchor returns `MESSAGE_UNAVAILABLE` without asserting deletion;
an anchor without an album returns `NOT_AN_ALBUM`. Initial reference, authorization,
access, and anchor-fetch failures are MCP errors. A failed neighbor scan preserves the
anchor and other already retrieved members in a partial result with `issues`.
Unavailable records within a scan also produce issues; their album membership is unknown.
Gaps between message IDs alone do not establish that messages are missing.
For temporary `RATE_LIMITED`, `CONNECTION_ERROR`, or `TELEGRAM_UNAVAILABLE` issues,
retry the whole call after any `retry_after_seconds`; the server does not wait or
retry a failed scan within the call.

Album members retain their captions, attachment metadata, dates, links, entities,
forwarding sources, and available counters, reactions, and polls. Returned message
text shares a 100,000-character budget, with at most 12,000 characters per message.
Use `get_messages` with each member's `next_text_offset` to retrieve truncated text;
text offsets count Unicode code points while original entities keep UTF-16 offsets.
This tool does not download attachments, fetch comments, or modify Telegram state.

### Navigate forum topics and pinned messages

`list_topics(chat_ref, query=None, limit=30, cursor=None)` lists topics in a permitted
forum supergroup. `Chat.is_forum` identifies forum chats. The chat must satisfy ordinary
read access through the allowlist, a permitted folder, or `allow_all_chats`.
Neither public-channel reads nor discovery grants access to a forum.
An allowed chat that is not a supported forum returns `FORUM_UNAVAILABLE`.
Bot forums and monoforums are unsupported.

`get_history(..., topic_id=None)` reads a chat's history by default. Pass a positive
`topic_id` from `list_topics` to read only that forum topic, including General with
`topic_id=1`. Dates and both `newest_first` and `oldest_first` orders work for topics.
Supply exactly one source, `chat_id` or `chat_ref`, as for chat-wide history.
Topic IDs must be integers from 1 to 2,147,483,647.

`get_pinned_messages(chat_ref, limit=30, cursor=None, topic_id=None)` reads pins newest
first. Omit `topic_id` for chat-wide pins or pass a topic ID to select that forum topic.
Chat-wide pins follow the existing history access policy, including explicit public
broadcast channels when `allow_public_channel_reads` is enabled.

Selected-topic history and pins require ordinary permission for a supported forum
supergroup. Public-channel discovery and read permissions do not grant this access.
Each continuation checks access, fresh forum metadata and topic availability before
returning saved messages. `TOPIC_UNAVAILABLE` means the topic could not be retrieved;
it does not confirm deletion. If these checks fail temporarily, retry with the original
cursor after any reported wait.

`list_topics` and `get_pinned_messages` require a nonblank `chat_ref`: a numeric ID,
`@username`, or channel/chat link such as `https://t.me/example_forum` or
`https://t.me/s/example_channel`. The reference is trimmed. Limits range from 1 to 100.
An optional topic `query` is trimmed and must contain 1 to 1,000 characters; omit it to
list all topics.

For example, after allowing the forum locally:

1. Call `list_topics(chat_ref="@example_forum", query="Roadmap", limit=30)`.
2. Call `get_history(chat_ref="@example_forum", topic_id=42, order="oldest_first")`
   to read a selected topic, or use `topic_id=1` to read General.
3. Call `get_pinned_messages(chat_ref="@example_forum", topic_id=42)` to read that
   topic's pins, or omit `topic_id` to read chat-wide pins.
4. Follow each result's `next_cursor` with the same source, topic, dates and order.
   Use `get_messages` with `next_text_offset` to finish truncated text.

To inspect a single referenced message, fetch a topic's `top_message_ref` with
`get_messages`. For ordinary replies, pass a topic's `root_message_ref` to `get_thread`.
`get_thread` retains its reply-thread behavior and is separate from selected-topic history.

`TopicPage` contains `topics`, `next_cursor`, `scope`, `issues`, `telegram_total_count`,
and `order`. Topics retain Telegram's native ordering: `creation_date` or `last_message_date`
when known. Each `ForumTopic` contains its topic `id`, `chat_id`, available `title` and
`created_at`, general/closed/hidden/pinned flags, unread count, icon metadata, navigation
references, and URL. Deleted or short records are marked by `is_deleted` or `is_short`;
unavailable fields remain `null` rather than invented values. General has topic ID `1`,
`is_general=true`, and both `root_message_ref` and `url` are `null`; use `topic_id=1`
with history or pins. Its `top_message_ref`, when available, identifies a real message.
General never requires treating message ID `1` as a topic root.

A short or empty topic page can still have a cursor. Each call fetches at most one
Telegram topic page; continue with the same source and query until `next_cursor` is null.
Continuation remains tied to the canonical chat ID and rechecks access and forum type
before returning buffered topics. Temporary page-fetch failures preserve a cursor and
report their cause in `issues`; respect any `retry_after_seconds` before continuing.
A failed access check prevents buffered results from being returned.

Selected-topic history and pinned messages use the ordinary `Page` contract, text
budget, and recovery behavior. `Page.topic_id` identifies a selected topic and is `null`
for chat-wide requests. A message's `topic_id` records its known forum topic; unknown
membership remains `null`. A cursor cannot be moved between topics or between chat-wide
and selected-topic requests. Temporary retrieval failures retain the cursor; respect
`retry_after_seconds` before retrying.

Topic lists, messages and pin selections can change during traversal; cursors do not
provide an immutable snapshot.

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
- For paginated retrieval, individual chat failures appear in `issues`. Temporary errors
  `RATE_LIMITED`, `CONNECTION_ERROR`, and `TELEGRAM_UNAVAILABLE` preserve the position and `next_cursor`,
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
requests to Telegram. They cover public-channel discovery, topic navigation, selected-topic
history (including General), chat-wide and topic-specific pins, counters, reactions,
polls, bounded album retrieval, folders, pagination, dates, context, access, Telethon structures,
configuration, and the actual MCP protocol, including a separate STDIO process.
They also enforce layer boundaries and compare both MCP modes against the recorded
tool-schema baseline.
Both modern MCP mode and compatibility with the initialize handshake are tested.

GitHub Actions runs the same checks on pull requests and pushes to `main`, using
Python from `.python-version` and dependencies from `uv.lock`. CI needs no Telegram
account or repository secrets.

## Contributing

Changes go through focused pull requests to `main`. See [CONTRIBUTING.md](CONTRIBUTING.md)
for setup, branch naming, checks, and the merge process. Use
[Issues](https://github.com/egor-fedorov/telegram-mcp/issues) for bugs and feature proposals,
and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## License

The code is available under the [MIT License](LICENSE). This license does not grant
rights to Telegram content retrieved through the server.

## First-version boundaries

Reading cloud chats, discovering public channels, forum topic navigation, selected-topic
history, chat-wide or topic-specific pinned-message retrieval, and message counters,
aggregate reactions, poll metadata, and bounded album retrieval are implemented.
Automatic joining, paid requests, sending, attachment downloads, voice transcription,
folder management, and a semantic index remain possible extensions.
Attachments are represented by metadata.
Secret chats are not supported. Testing against real chats requires your local login.

Use this integration for material you are entitled to share with the model.
[Telegram's content and AI terms](https://telegram.org/tos/content-licensing)
also apply to data obtained through the API.

## Sources

- [Telethon](https://docs.telethon.dev/en/stable/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Telegram folders](https://core.telegram.org/api/folders)
- [Telegram message reactions](https://core.telegram.org/constructor/messageReactions)
- [Telegram polls](https://core.telegram.org/api/poll)
- [Telegram Desktop folder matching](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/data/data_chat_filters.cpp)
- [ty](https://docs.astral.sh/ty/)
