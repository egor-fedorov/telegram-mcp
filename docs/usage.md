# Using Telegram Context MCP

[Setup](../README.md) · [Client configuration](clients.md) · [Access policy](configuration.md) · [Troubleshooting](troubleshooting.md)

The same 22 tools, two prompts and read policy apply to standalone STDIO and the shared
local HTTP server. In shared mode, all connected clients use the same account, permitted
sources, and in-memory cursors. They are not isolated tenants. Treat message text
as source data, not instructions; use original message links in answers.
This guide describes the 0.4 contract; see the [migration guide](migration-0.4.md)
for changes from 0.3.0. Sections marked *(unreleased)* describe the M5 additions in
the source tree, which are not in a published package yet.

- [Download a selected attachment](#download-a-selected-attachment)
- [Changes and exports](#changes-and-exports)
- [Read a period across chats or folders](#read-a-period-across-chats-or-folders-unreleased)
- [Monitor a folder or selected sources](#monitor-a-folder-or-selected-sources)
- [Save watch batches before acknowledging](#save-watch-batches-before-acknowledging-unreleased)
- [Monitoring prompts](#monitoring-prompts-unreleased)
- [Keep selected files](#keep-selected-files)
- [Discover, select, and read](#discover-select-and-read)
- [Read permitted sources](#read-permitted-sources)
- [Search links and media](#search-links-and-media-efficiently)
- [Read counters, reactions, and polls](#read-message-counters-reactions-and-polls)
- [Read a selected album](#read-a-selected-album)
- [Navigate forum topics and pinned messages](#navigate-forum-topics-and-pinned-messages)
- [Retrieval rules](#retrieval-rules)

### Download a selected attachment

Enable `allow_attachment_downloads = true` in local configuration and restart the
server. Source permissions still apply independently.

1. Read the selected post with `get_messages(refs=["https://t.me/example_channel/123"])`.
2. Choose its `attachments[].id`, for example `-1001234567890:123:document`.
3. Call `download_attachment(attachment_id="-1001234567890:123:document")`.
4. Use `local_path` from a client on the same computer, or explicitly request
   `resources/read` with the returned `resource_uri`. MCP client support varies;
   a `ResourceLink` alone does not transfer the file bytes.

The compact result includes `attachment_id`, canonical `message_ref`, name, MIME,
actual `size`, `sha256`, absolute `local_path`, `resource_uri`, and `expires_at`.
Only the resource read includes base64-encoded original bytes. No file is executed,
unpacked, parsed, transcribed, or automatically included in model context by this server.
Some clients impose their own resource-size limits; local file access avoids that transfer.

Each call downloads one current attachment up to **20 MiB**, with a 60-second total
deadline. Documents include audio, voice and video; photos use the largest available
still image in Telegram, which may differ from the original upload. For albums, choose
individual members from `get_album`. IDs identify a message attachment slot, not a
historical version: a later download may retrieve an edited replacement. Resource reads
recheck authorization, source access, protection and media identity before returning bytes.

Protected or self-destructing media, webpage previews, stories, paid media and alternate
video qualities are unavailable. Downloads never follow a forwarded source or external
URL. An expired Telegram file reference is refreshed once, only for the same media.
Network errors, timeouts and cancellation discard the partial file. Retry a download
from the beginning; respect `retry_after_seconds` for FloodWait. No partial resume exists.

Files expire after **15 minutes**, with a **100 MiB / 32-file** storage limit and no
eviction of unexpired files. One 20 MiB reservation counts while downloading. Store files
are private, generated filenames under the application cache directory, isolated by
session path. Expired resources stop working immediately; cleanup runs every 60 seconds
and on shutdown. Crash leftovers are removed at the next authorized store initialization.
Busy-file deletion failures remain accounted and are retried. Already delivered copies
cannot be revoked. Use `save=true` with a configured archive directory to keep a copy.

Specific errors are `DOWNLOADS_DISABLED`, `ATTACHMENT_UNAVAILABLE`,
`ATTACHMENT_PROTECTED`, `ATTACHMENT_TOO_LARGE`, `DOWNLOAD_STORAGE_FULL`, and
`DOWNLOAD_UNAVAILABLE` (unknown/expired resource). Standard access, authorization,
network and local I/O errors retain their existing meanings. Resources are not listed
as a shared download catalog; use the exact URI returned by the tool.

### Changes and exports

`get_changes` and `export_history` select one explicit chat and use ordinary
source-read permissions. They
do not follow discussions or forwarded sources, and need no attachment-download opt-in.
This low-level interface keeps checkpoints in the calling client. For continuity across
agent sessions and multiple sources, use a named subscription below. Neither interface
polls Telegram in the background.

Start observing a selected source:

```text
get_changes(chat_ref="https://t.me/example_channel")
get_changes(checkpoint="<returned next_checkpoint>", limit=30)
```

The first call establishes the current head and fingerprints the latest 100 messages;
it returns no old messages and explicitly reports `baseline_created=true`.
Save `next_checkpoint` and pass it **instead of** `chat_ref`
on subsequent calls. Each pass fixes an upper message ID, rechecks previously observed
messages in ascending ID order, then returns new IDs in ascending order. Newer arrivals
wait for the next pass. Use `export_history` or `get_history` for older history.

The result includes `changes`, `returned_count`, `complete`, `upper_message_id`,
`rechecked_message_ids`, `tracked_count`, `issues`, and `next_checkpoint` with its expiry.
Change records have `kind`, canonical `message_ref`, `revision`, `observed_at`, and the
current `message` when available:

- `new`: an available message beyond the previous position;
- `updated`: changed content or a tracked message becoming available again;
- `unavailable`: a tracked message could not be retrieved; this **does not prove deletion**.

`complete=true` means the current pass finished. Its checkpoint starts the next poll;
an incomplete checkpoint continues the same pass. The rolling window retains only the
last 100 known IDs, including unavailable ones until newer IDs displace them. Intermediate
edits, older edits outside that window, and messages deleted before observation are not
recoverable. Counters, reactions and poll-vote changes alone do not produce updates.
Inline text retains the normal offsets for finishing long text with `get_messages`.

The latest checkpoint of each independent reading chain survives restart and capacity
pressure until its **30-day expiry**. Unchanged state reuses its token and renews that
expiry. Superseded versions can be evicted within the **4096-snapshot / 64 MiB** budget.
If only protected states remain, `CHECKPOINT_STORAGE_FULL` preserves them instead of
discarding a source. Replaying an old version starts an independent branch.
Calls with `chat_ref` protect only the latest **unused baseline** for that source and
authorization; older unused baselines become eligible for eviction. Once a token is
used for continuation, its chain has independent protection, including unchanged polls
and retries after file-publication failure. Use named watches for consumers without token storage.
Checkpoints contain only positions/fingerprints and are bound to the session path and
Telegram authorization. Username changes cannot redirect them. For an expired token,
recover the missing period with `get_history` or `export_history`; a new baseline
explicitly skips old messages and is never started automatically.

Old tokens remain reusable until expiry/eviction. Replays can repeat records: apply the
current state by `message_ref` and compare its last applied `revision`. This is not an
exactly-once event log; returning from unavailable to an earlier revision is still an update.
Keep a token until its response has been processed. Temporary retrieval failures return
an unfinished checkpoint and issues; a tool-level access/storage error leaves the original
token usable. Respect `retry_after_seconds`. M4 FloodWait deadlines survive restart and
apply to all M4 calls in the session, including old-token retries.

Export a period, with an inclusive lower and exclusive upper date:

```text
export_history(chat_ref="https://t.me/example_channel",
               date_from="2026-09-01T00:00:00Z", date_to="2026-09-02T00:00:00Z",
               format="jsonl")
export_history(checkpoint="<returned next_checkpoint>", format="jsonl")
get_changes(checkpoint="<saved change checkpoint>", output="markdown")
```

Without dates, history starts at the oldest available message and ends at the first
call's current time. Upper date and ID stay fixed across parts and restarts. A history
checkpoint already contains its source and dates; do not repeat them. `format` may
change between parts. Each part contains at most **1000 records / 20 MiB** within the
normal RPC/time budgets. Follow `next_checkpoint` until null; an empty part can still
need continuation. Protected/expiring messages become explicit omitted records.
`complete` describes traversal, so also inspect `omitted_count` and `issues`.

JSONL format version 1 contains a manifest with source metadata, one record per full
message/change or omission, and a summary with coverage and continuation. Message
references in the file are `{chat_id, message_id}` objects. Full text, original UTF-16
entities, dates, source URLs, forwards and attachment metadata are preserved. Markdown
provides source links, dates, authors, original text in safe code fences and metadata.
When a Telegram URL is unavailable, the canonical reference remains. No format downloads
binary attachments or rewrites originals into summaries. Versions are observed during
reading; Telegram does not provide a transactional snapshot of the whole export.

File output returns a small result plus `export` metadata and a ResourceLink, without
inline message duplication. Read `export.resource_uri` explicitly with `resources/read`
for UTF-8 text, or use `export.local_path` on the server computer. Files expire after
**15 minutes** or shutdown; export storage is separately limited to **100 MiB / 32 files**.
Use `save=true` to keep the selected part. Source permission and chat protection are checked
again for resource reads; the file is not regenerated. Delivered copies cannot be revoked.
Cleanup runs every 60 seconds, on shutdown and at the next export-store initialization
after a crash. Failed deletions remain counted against quota and are retried.

Errors include `INVALID_CHECKPOINT`, `CHECKPOINT_EXPIRED`, `CHECKPOINT_MISMATCH`,
`CHECKPOINT_INCOMPATIBLE`, `CHECKPOINT_STORAGE_ERROR`, `EXPORT_PROTECTED`,
`EXPORT_RECORD_TOO_LARGE`, `EXPORT_STORAGE_FULL`, and `EXPORT_UNAVAILABLE`.
`CHECKPOINT_STORAGE_FULL` means every remaining snapshot is protected. An oversized
record is not truncated or silently skipped; the input checkpoint remains
usable for ordinary retrieval. See [local state and permissions](configuration.md#m4-local-state).

### Read a period across chats or folders (unreleased)

`get_history` also accepts plural selectors: exactly one of `chat_id`, `chat_ref`,
`chat_ids` or `folder_ids`. One MCP traversal then replaces a client loop over chats;
Telegram still reads each source separately. Folder IDs form a union and duplicate
sources are read once. Plural selectors require both `date_from` and `date_to`
(`[date_from, date_to)`, aware) and do not accept `topic_id`.

```text
get_history(folder_ids=[3], date_from="2026-10-19T00:00:00+03:00",
            date_to="2026-10-26T00:00:00+03:00", limit=50)
# Continue with next_cursor and the same arguments until it is null.
```

Multi-source history defaults to `ordering="page"`: available messages are returned
without first fetching a head from every source, sorted within each page only. Use
`ordering="global"` for strict order across pages, at the existing preparation cost.
Both `order` directions work. The selection and upper date are frozen on the first
page; new folder members do not join an existing cursor. Each continuation rechecks
authorization and folder-derived access before returning buffered messages, so revoked
access cannot expose them. Public-channel reads apply only to explicit `chat_ids`.

Two traversal budgets apply, separate from the per-page `limit` and the existing
RPC/time/text limits:

| Budget | Default for plural selectors | Range | Effect |
| --- | --- | --- | --- |
| `max_sources` | 20 | 1–1000 | A larger permitted selection returns `SOURCE_LIMIT_EXCEEDED` with the count before any history read |
| `max_messages` | 100 | 1–1000 | Total messages emitted across all pages of the traversal |

The server never silently reads the first N sources: narrow the selection or pass a
larger `max_sources`. Bounded responses include `budget` with `max_sources`,
`max_messages`, `emitted_messages` and `exhausted`. When the message budget is reached
with unread work, `exhausted=true`, `scope.complete=false` and `next_cursor` remains;
continuing at the same budget makes no history request. Passing a larger `max_messages`
(up to 1000) with that cursor continues it; every other argument stays fixed, and a
smaller value is rejected. Replaying an earlier cursor never counts messages twice.
Legacy single-chat calls keep their behavior and have no implicit total budget; they
may opt into the same budgets explicitly.

Offline fixture measurements (5 posts per source, `limit=50`, compact JSON):

| Selection | Arguments | MCP calls | History reads | Messages | Coverage | Response bytes |
| --- | --- | --- | --- | --- | --- | --- |
| 20 sources | defaults | 2 | 20 | 100 | complete | ~48 KB |
| 100 sources | `max_sources=100` | 2 | 20 | 100 | partial, `exhausted=true` | ~49 KB |
| 100 sources | `max_sources=100, max_messages=1000` | 10 | 100 | 500 | complete | ~244 KB |

The first page was nonempty after 10 source reads in each case. A real account's latency,
FloodWait and post volume determine actual time; budgets bound the work, not the wall clock.
Multi-source file export is not part of this change.

### Monitor a folder or selected sources

Create a named subscription once, then find it again with `list_watches` in any later
agent session. Select exactly one of `chat_refs` or `folder_ids`; existing access rules
still apply. Names use 1–64 lowercase letters, digits, hyphens or underscores.
Explicit `chat_refs` are limited to 20 entries, with equivalent usernames/links resolved
once. Use folders for larger selections. Creation has a 35-second deadline and leaves
no partial watch on failure; respect any returned FloodWait before retrying.

```text
create_watch(name="jobs", folder_ids=[3])
get_watch_changes(name="jobs", limit=30)
ack_watch_changes(name="jobs", batch_id="<processed batch_id>")
# Or acknowledge the processed batch and retrieve the next one in the same call:
get_watch_changes(name="jobs", ack_batch_id="<processed batch_id>", limit=30)
```

By default coverage starts at creation time, including messages arriving before the
first poll. Automatic boundaries include the current whole second to match Telegram's
timestamp precision. Supply an aware `date_from` to read earlier available messages. Repeating
identical creation arguments preserves positions; different arguments under the same
name return `WATCH_CONFLICT`. Folder membership refreshes between passes. A new member
starts at the previous successfully completed pass's starting boundary, or the watch's
start date before the first completed pass. Returning members resume their saved positions.

Process the returned changes and omissions **before acknowledging**, including when
`complete=true`. The server persists one batch before delivery and returns the same
`batch_id` and content until it is acknowledged, even after a lost response or restart.
`limit` applies to new batches only. Acknowledgement atomically advances positions;
duplicate acknowledgements cannot confirm a newer batch. Delivery can repeat; this is
not exactly-once processing. Independent consumers need different subscription names.
With `ack_batch_id`, acknowledgement is committed before the next read. If that read
fails or its response is lost, retry the same argument: it cannot acknowledge a newer
batch. The separate acknowledgement tool remains available for the final batch.

When `batch_id=null`, no confirmation is needed. Empty progress is saved automatically.
Continue an incomplete pass by name, respecting issues and retry delays. Per-call RPC,
time and text budgets still apply: a large folder can require several calls, but found
changes are returned without preparing every source first. Sources rotate fairly;
there is no strict global ordering across sources or pages. `progress` reports counts,
not a repeated full list of source IDs. A failed source keeps its position while healthy
sources continue in later passes. `complete=true` requires a finished pass without
unresolved source errors; also inspect omissions. Issue previews contain at most 20
entries; `issue_count` is the exact count.

For known channels/supergroups, heads and event counters (`pts`) are fetched in batches
of at most 100 selected sources. An unchanged completed source needs no history or
revision request. Edits are detected by the event counter even when the last message
ID stays the same. Initial passes, incomplete scans, missing counters and unsupported
sources use ordinary reads; observed polls retain revision checks. Fresh metadata
checks saving restrictions. No global account search or update stream is used.
Offline warm fixtures take one peer RPC for 100 unchanged channels and ten for 1000,
plus shared authorization/folder checks. Active sources and cold catalogs cost more.

Subscriptions retain positions and one unacknowledged batch, including returned text,
in private local storage until `delete_watch(name="jobs")`. There is no automatic expiry
or quota eviction: **64 subscriptions / 64 MiB** of serialized subscription data is the
default bound. `WATCH_STORAGE_FULL` preserves existing progress. Protected/expiring
content is omitted explicitly instead of being saved. Every replay checks current
authorization, access and source protection; a failed check withholds the entire batch
without advancing it. Acknowledgement confirms already processed data; it does not
read or resend the batch. Deletion explicitly discards unprocessed data as well as positions.

The same revision limits as `get_changes` apply: the last 100 known IDs are rechecked;
intermediate edits and messages deleted before observation cannot be recovered.
Subscriptions are on-demand only: no scheduler, notifications or complete message archive.

### Save watch batches before acknowledging (unreleased)

With `archive_directory` configured, `get_watch_changes(name="jobs", save=true)` writes
each returned batch to a permanent JSONL copy **before** it can be acknowledged and
returns its receipt in `saved_file`:

```text
get_watch_changes(name="jobs", save=true)
# Process the batch, then acknowledge it and archive the next one in one call:
get_watch_changes(name="jobs", save=true, ack_batch_id="<processed batch_id>")
ack_watch_changes(name="jobs", batch_id="<final processed batch_id>")
```

`save=true` requires `archive_directory` before any acknowledgement or polling and
otherwise returns `ARCHIVE_DISABLED`. The server persists the batch and a reserved copy
location first, publishes the file and its `metadata.json`, then records the receipt.
Retries, lost responses and restarts reuse the same copy; a crash between publication
and receipt is reconciled without writing a second file. Once saving was requested for
a pending batch, both acknowledgement paths require a verified receipt
(`WATCH_ARCHIVE_PENDING` otherwise); a later `save=false` call cannot bypass it. A
missing or damaged copy returns `ARCHIVE_COPY_INVALID` on acknowledgement and is rebuilt
from the still-pending batch by the next `save=true` call. With `ack_batch_id`, the
acknowledged batch is archived first; a later poll or save failure keeps that
acknowledgement and leaves the new batch pending. Access and saving restrictions are
rechecked before replaying or saving. Batches used only with `save=false` keep the
0.4 behavior.

The copy is versioned JSONL: a `watch_batch` header (watch name, batch ID, source IDs,
counts, progress, cycle start), one `change` record per returned change and one `issue`
record per reported issue. Message text is exactly as delivered, including clipping and
`next_text_offset`; this is not a full-message archive. `metadata.json` records the tool,
watch name, batch ID, source IDs, format, size, SHA-256 and save time. To recover after an
interrupted session without the old MCP response, find the batch under `archive_directory`:

```sh
grep -l '"batch_id": "<batch_id>"' /absolute/private/telegram-archive/*/metadata.json
grep -l '"watch_name": "jobs"' /absolute/private/telegram-archive/*/metadata.json
```

Retained copies survive acknowledgement and `delete_watch`. They allow a batch to be
reprocessed; they do not prove that a digest was delivered or keep the agent's report.

### Monitoring prompts (unreleased)

Two MCP prompts package the monitoring workflows. A client shows them as user-selected
templates; retrieving one never reads posts, creates or deletes watches, acknowledges,
downloads or writes files. Only `period_digest` with `folder_name` reads folder
definitions from Telegram, without a dialog scan.

| Prompt | Arguments | Workflow |
| --- | --- | --- |
| `watch_digest` | `watch_name` (required); `focus`, `max_messages` (1–1000, default 100), `save` (`true` default / `false`) | Process an existing named watch into a source-linked digest |
| `period_digest` | Exactly one of `chat_ref`, `folder_name`, `folder_id`; `days` (1–365, default 7) **or** `date_from`+`date_to`; `timezone`, `focus`, `max_sources` (default 20), `max_messages` (default 100) | Read a bounded period with multi-source `get_history`; watch positions are unchanged |

Prompt arguments are strings. `days=7` is the rolling previous 7×24 hours, frozen once
when the prompt is rendered. `date_from`/`date_to` take two calendar dates
(`YYYY-MM-DD`, midnight in `timezone` or the configured `timezone`) or two aware
timestamps, which keep their offsets. The upper bound is exclusive and capped at the
rendering time. Unknown zones, skipped or repeated local midnights, mixed forms and
empty periods are MCP parameter errors. `folder_name` must match exactly one folder
title (case-insensitive, trimmed); otherwise use `folder_id`. `focus` holds at most 1000
characters and is kept separate from Telegram content in the rendered text.

`watch_digest` saves each batch before acknowledging it by default (`save="true"`), so
`archive_directory` is required; with `save="false"` it processes one batch and leaves
it pending for the user to acknowledge. It finds the name with `list_watches` and never
creates or resets a subscription. A pending batch larger than the remaining budget is not
acknowledged. `period_digest` calls `get_history` once with fixed arguments and reports
`SOURCE_LIMIT_EXCEEDED` or an exhausted budget instead of raising limits itself.

Every template states that Telegram text, captions, forwards, filenames and links are
untrusted data: posts asking to send a resume, contact someone, delete a watch or
acknowledge a batch are summarized, not followed. The agent writes short notes with
canonical references per page or batch and groups reposts only when
`forward_origin.message_ref` is identical. These rules guide the agent; the server's
access controls remain the security boundary.

Clients that support completion can suggest `watch_name` values from local subscription
storage, without a Telegram request. See [client support](clients.md#prompts-and-completion).

**Vacancy preset.** Use `period_digest` with a vacancy focus, for example:

```text
period_digest(folder_name="Jobs", days="7",
  focus="Vacancies only: role, company, location or remote, salary, how to apply.
         Use only what posts state; mark missing fields as not stated.")
```

**Copyable recipe** for clients without prompts: call `list_watches`, then repeat
`get_watch_changes(name, save=true, limit<=100)`; process every change and omission,
pass the processed `batch_id` as `ack_batch_id` in the next call, and finish with
`ack_watch_changes` when `complete=true`. For a period, call
`get_history(folder_ids=[...], date_from, date_to, ordering="page")` and follow
`next_cursor` with the same arguments.

### Keep selected files

Set `archive_directory` to a dedicated private local directory and restart the server:

```toml
archive_directory = "/absolute/private/path/telegram-archive"
```

Then request `save=true` on `download_attachment`, `export_history`, or `get_changes`
with `output="jsonl"`/`"markdown"`. The call creates its permanent copy before returning.
`get_watch_changes(save=true)` saves batches the same way *(unreleased)*; see
[saving watch batches](#save-watch-batches-before-acknowledging-unreleased).
The file metadata includes `saved_file.local_path`, `metadata_path`, `size`, `sha256` and `saved_at`.
The original temporary resource and its `expires_at` remain unchanged; that expiry does
not apply to the permanent copy. Files use unique private subdirectories and cannot
overwrite a previous copy. The tool accepts no destination path.
Each new copy has an adjacent UTF-8 `metadata.json` (schema version 1) containing its
tool, canonical source, message reference/link when available, original filename, MIME,
size, checksum and save time. Exports also record their format and date/ID boundaries.
Names are metadata, never destination paths. Existing copies are not rewritten.
File and description are published together; hashing, writes, fsync and rollback use
two worker threads so a slow disk does not block other calls on the event loop.

Permanent copies survive shutdown and have **no automatic deletion or total archive
quota**; manage disk usage yourself. The existing 20 MiB per-file bound and content
protection rules still apply. Failed saving leaves the input checkpoint reusable.
`ARCHIVE_DISABLED` means the directory is unset; `ARCHIVE_STORAGE_ERROR` reports a write
or integrity failure. Files are on the server computer, not automatically uploaded to
the client. See [resource compatibility](clients.md#reading-files-and-resources).

### Inspect or clean checkpoints

Stop the process owning the Telegram session before running these offline commands:

```sh
telegram-mcp checkpoints status
telegram-mcp checkpoints prune                         # Preview expired/superseded records
telegram-mcp checkpoints prune --apply                 # Apply that cleanup
telegram-mcp checkpoints prune --chat-id=-1001234567890 --include-protected
# Only after inspecting the preview; selected active tokens will stop working:
telegram-mcp checkpoints prune --chat-id=-1001234567890 --include-protected --apply
```

Use `--all` instead of `--chat-id` to explicitly select every source for protected
cleanup. Ordinary pruning preserves live protected positions. Old version-2 roots
are conservatively retained because their prior use cannot be reconstructed; protected
cleanup is the recovery path if those roots have already filled storage. No Telegram
login or connection is needed. Commands preserve named watches, archives, session data
and FloodWait deadlines; an active server returns `SESSION_IN_USE`.

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
`participants_count`. A participant count unavailable in Telegram's response is omitted,
not zero. Discovery does not invent dialog state such as mute, unread, or folder membership.
Each `DiscoveryResult` contains:

| Field | Meaning |
| --- | --- |
| `channels` | Eligible channels, deduplicated by ID in Telegram's result order |
| `returned_count` | Actual number of channels returned |
| `telegram_total_count` | Telegram's recommendation count when provided; otherwise omitted |
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

`get_history` accepts exactly one source parameter: `chat_id` or
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
Unknown optional fields are omitted; no additional requests are made to forwarding sources.

### Search links and media efficiently

Use `search_messages(query="", has_link=true, chat_ids=["-1001234567890"])` to find
link-bearing messages without a keyword. `has_link=false` excludes links; `null`/omission
adds no link condition. A nonempty keyword remains required unless `has_link=true`.
The predicate recognizes URL/TextURL entities and Telegram webpage media, without
regex guesses or network requests to websites. It can be combined with `content_type`.

`web_page` describes available, pending, empty, or not-modified Telegram previews and
includes supplied URL/display URL, title, description, site, type, author, and pending
until date. Preview photos/documents are separate from actual message attachments.

URL, photo, voice, and audio filters run on Telegram where their coverage matches the
requested type. With both link and media conditions, URL is pushed down and the media
condition is checked locally. Sender searches retain local media filtering because
Telegram can ignore combined sender/media filters. Text, other, video, and document
categories retain local filtering to avoid omitting animations, round videos, or stickers.
All paths enforce exact date and content conditions locally.

With `allow_all_chats=true`, no chat/folder/sender selector and newest-first order,
search uses `messages.searchGlobal`. It freezes the initial catalog IDs and date ceiling.
Custom folder IDs are not passed as Telegram archive bucket IDs. Restricted searches
never fetch content from other chats through global search and filter it afterwards.
No paid public-post search is used.

Restricted searches default to `ordering="page"`: sources are visited round-robin,
results are returned as soon as a page is filled or its work budget is reached, and
only that page is sorted. Later pages may contain newer messages than earlier pages.
This avoids waiting for the first result from every permitted chat. For a chronological
merge across all pages, set `ordering="global"`; this can require empty preparation pages
on a large selection. Both support `order="newest_first"` (default) or `"oldest_first"`.
Per-chat ties use numeric chat ID and message ID.

The response's `ordering` reports the actual guarantee. Native unrestricted global
search retains global order even when the request uses the default `ordering="page"`;
it preserves Telegram's order within equal timestamps. Repeat both requested ordering
parameters when continuing a cursor; changing either requires a new search.

### Read message counters, reactions, and polls

All message-returning tools include available metadata from the same Telegram message
payload. This also applies to context, replies, selected-topic history, and pins.
No additional requests are made for counters, reactions, poll results, or participant
identities. The tools do not vote, react, send Stars, or mark messages read.

| Message field | Meaning |
| --- | --- |
| `view_count` | Telegram's message view count, omitted when unavailable |
| `forward_count` | Telegram's message forward count, omitted when unavailable |
| `reply_count` | Telegram's reply/comment count, omitted when unavailable |
| `reactions` | Aggregate reaction entries and their `is_min` flag, omitted when unavailable |
| `poll` | Poll definition and available result metadata, omitted when unavailable |

An explicit count of `0` is retained; missing counts are never inferred as zero.
Counters, reactions and poll results can change between calls. Their presence does not
grant access to linked comments or other messages.

`reactions.results` contains each reaction's `type` and `count`. Standard emoji use
`type="emoji"` and `emoji`; custom emoji use `type="custom_emoji"` and a string
`document_id`. Paid reactions use `type="paid"`: their count measures **Stars, not
people**. Unsupported and empty reaction types retain their counts without inventing
an emoji. `is_chosen` and `chosen_order` describe the current account's selection only
when Telegram supplies a full result for an ordinary or custom emoji. An `is_min=true`
summary is partial: its personal selection fields are omitted. Paid reactions also
omit personal selection fields. `reactions_as_tags=true` identifies reactions
used as Saved Messages tags. Reactor lists are not returned.

`poll` contains a string `id`, formatted `question`, ordered `answers`, state flags,
and optional `close_period_seconds` and `closes_at`. Each answer has formatted `text`
and an `option_id`: a lowercase hexadecimal encoding of Telegram's opaque option bytes.
Use this ID to match results to options; titles and positions are not stable identifiers.
Polls keep `content_type="other"` for compatibility with existing filters.

`poll.results` is omitted when unavailable. When present, it contains `is_min`, optional
`total_voters`, an optional `answers` result vector, and an optional formatted `solution`.
The result vector is kept separate from the poll's option list: omission means the vector
was absent, while `[]` means Telegram supplied an empty vector. Each entry keeps its
`option_id`, optional `voter_count`, `is_chosen`, and `is_correct`; do not infer zero votes
for options absent from the result. An `is_min=true` result leaves `is_chosen` unknown.
Quiz correctness is omitted until Telegram reveals a correct answer; ordinary polls
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
unavailable optional fields are omitted rather than invented. General has topic ID `1`,
`is_general=true`, and both `root_message_ref` and `url` are absent; use `topic_id=1`
with history or pins. Its `top_message_ref`, when available, identifies a real message.
General never requires treating message ID `1` as a topic root.

A short or empty topic page can still have a cursor. Each call fetches at most one
Telegram topic page; continue with the same source and query until `next_cursor` is null.
Continuation remains tied to the canonical chat ID and rechecks access and forum type
before returning buffered topics. Temporary page-fetch failures preserve a cursor and
report their cause in `issues`; respect any `retry_after_seconds` before continuing.
A failed access check prevents buffered results from being returned.

Selected-topic history and pinned messages use the ordinary `Page` contract, text
budget, and recovery behavior. `Page.topic_id` identifies a selected topic and is omitted
for chat-wide requests. A message's `topic_id` records its known forum topic; unknown
membership is omitted. A cursor cannot be moved between topics or between chat-wide
and selected-topic requests. Temporary retrieval failures retain the cursor; respect
`retry_after_seconds` before retrying.

Topic lists, messages and pin selections can change during traversal; cursors do not
provide an immutable snapshot.

## Retrieval rules

Version 0.3 uses [compact JSON](migration-0.3.md): string references, shared chat metadata,
and omitted unknown optional fields. Text and structured results carry the same complete
payload. `list_folders` returns an object with a `folders` array.

- Folders form a union without duplicates. `chat_ids` intersect with selected folders.
  An omitted selector means no additional filter; an empty list is an error.
- Server restrictions apply in addition to these filters, including linked discussions
  and parent replies. To read channel comments, permit the linked group as well.
  Public-channel permission applies only to the current request and does not grant
  access to the linked group or replies from another chat.
- Required catalogs load in one shared background worker; repeat `CATALOG_LOADING`
  with the original arguments after its retry delay. Default dialog reuse is five
  minutes plus up to five minutes during refresh. Folder definitions and authorization
  are checked on every dependent call. Direct reads independent of folders skip the
  catalog. See [freshness limits](configuration.md#catalog-freshness-and-recovery-04).
- Folder rules account for categories, pinned/explicitly included chats, exclusions,
  archive status, mute/unread state, and unread mentions according to Telegram Desktop rules.
- Dates must include a timezone; the interval is `[date_from, date_to)`.
  History/search fixes the upper time boundary on the first request.
- Search uses Telegram's native search index and supported server filters, then applies
  exact local conditions. There is no semantic index or guarantee of exhaustive account history.
- Pagination freezes the selected set of chats. Continuation checks current access,
  including the public status of channels outside the allowlist. Revoked access causes
  the call to fail. If catalog/access checks fail, buffered messages are not returned;
  retry the original cursor after resolving the cause.
- `scope.complete` means the selected traversal finished without errors. For search,
  this covers Telegram's returned index, not a proof that every historical message was indexed.
  Keep calling with `next_cursor` until it becomes null. Preserve the original filters;
  `limit` may change.
- `scope.chat_ids` and `scope.pending_chat_ids` preview at most 20 IDs each; they are
  not full source lists. Use exact `chat_count` / `pending_chat_count` and the explicit
  `chat_ids_truncated` / `pending_chat_ids_truncated` flags. Internal scope remains
  complete. For `list_chats`, pending means remaining chat rows; native global search
  treats all sources as pending until the stream finishes.
- An empty page can still include `next_cursor`: filters, retries or strict-order
  preparation may consume its work budget. `progress` reports sources total, started
  (at least one successful source read), ready (buffered or exhausted), exhausted without
  a buffer, and RPC pages used in this call.
  A source needing more data can stop being ready; readiness is a current state, not a
  monotonically increasing percentage.
- For paginated retrieval, individual chat failures appear in `issues`. Temporary errors
  `RATE_LIMITED`, `CONNECTION_ERROR`, and `TELEGRAM_UNAVAILABLE` preserve the position and `next_cursor`,
  while `scope.complete` remains `false`. Repeat the original arguments with this cursor;
  for FloodWait, first wait for `retry_after_seconds`. The server does not wait inside
  the call or retry the failed stream within the same response.
- With `ordering="global"`, merged output pauses if a temporarily unavailable source
  prevents global order. Page ordering continues with other ready sources. Once
  continuation succeeds, that source's temporary issue
  disappears. Permanent issues remain in the result while other sources continue;
  retrying a permanent failure requires a new request.
- Cursors last 15 minutes and are bound to the process. Up to 128 cursor snapshots are stored
  within a 64 MiB serialized-data budget; older snapshots are evicted. Start again after
  expiry, eviction, or a restart. If FloodWait exceeds the cursor lifetime, wait until
  the cooldown ends and start a new selection.
- Message text shares a total response budget. `text_total_length`, `text_offset`, and
  `next_text_offset` let you retrieve remaining text through `get_messages`. Text offsets
  count Unicode code points; `entities` retain Telegram UTF-16 offsets into the full original.
- This is not a transactional archive: messages may change or disappear during traversal.
- STDIO, HTTP and `connect` admit up to four calls concurrently, with 16 waiting slots
  and a 120-second FIFO queue timeout. `SERVER_BUSY` means overflow or timeout; retry
  after its five-second delay. Downloads remain serialized; watch calls serialize
  only for the same name. Cancellation removes queued work without executing it.
