# Migrating to the 0.4 contract

The source version is `0.4.0.dev0`; this is not a published PyPI release. Installation
examples pinned to 0.3.0 continue to use the previous behavior. Source users should
restart the server and refresh their client's tool schemas. Process-local cursors
expire on restart; begin those searches again. There is no legacy response switch.
Existing configuration, Telegram sessions and durable M4 checkpoint formats remain usable.

## Scope is a bounded preview

Every wire `scope` now contains at most 20 `chat_ids` and 20 `pending_chat_ids`, even
on accounts with thousands of dialogs. These are the first IDs in the internal scope,
not a complete directory or a random sample. Internal policy and cursors still retain
all IDs. Read these fields together:

| Field | Meaning |
| --- | --- |
| `chat_count` | Exact number of selected sources |
| `pending_chat_count` | Exact number of unfinished sources or remaining chat rows |
| `chat_ids_truncated` | The selected-ID preview omits IDs |
| `pending_chat_ids_truncated` | The pending-ID preview omits IDs |
| `complete` | Traversal finished without issues; unchanged meaning |

`resolved_at` and `folder_ids` remain. A truncated preview does not mean incomplete
retrieval; inspect `complete`, `issues` and `next_cursor`. In `list_chats`, pending IDs
are the chats after the returned page. Native global search cannot report per-source
exhaustion, so all selected sources remain pending until that stream finishes.
Use paginated `list_chats` when you need the full permitted directory.

## Search returns early by default

`search_messages` adds `ordering="page"|"global"`, defaulting to `"page"`, and exposes
`order="newest_first"|"oldest_first"`, defaulting to `"newest_first"`.

Page ordering visits permitted sources round-robin and sorts only each returned page.
It stops fetching once that page is full. The next page may include newer messages,
and a temporarily unavailable source does not block other sources. Clients that relied
on a globally sorted result must explicitly pass `ordering="global"` on every call.
That mode retains strict merging and may return empty pages while preparing source heads.
Always follow `next_cursor` and report issues before claiming complete coverage.

The result's `ordering` states the actual guarantee. With all-chat permission, no
chat/folder/sender selector and newest-first order, native global search still provides
global ordering even for a request using `ordering="page"`. Restricted searches only
request permitted chats; they do not fetch outside content for local filtering.
Changing the requested ordering invalidates continuation. `progress.sources_started`
counts sources with at least one successful read; it does not measure completeness.

## Catalog reuse and concurrent calls

The default `catalog_cache_ttl_seconds` increases from 30 to 300. New setting
`catalog_cache_stale_seconds=300` permits the previous completed snapshot during a
refresh for at most another five minutes. Existing explicit TTL values are preserved.
TTL zero disables reuse; stale zero disables the grace period. Authorization and folder
definitions are checked on each dependent call. Cached unread/mute/archive/contact state
and newly joined dialogs may lag. See the [full freshness contract](configuration.md#catalog-freshness-and-recovery-04-unreleased).

Normal network failures retain cached dialog state and scan progress. One shared worker
refreshes on demand, stops on failure, and resumes only after a later call and any retry
deadline. Failed authorization or folder checks never release saved message buffers.

STDIO, HTTP and `connect` now share four active tool/resource slots and a FIFO queue of
16 calls, waiting up to 120 seconds. Queue overflow or timeout returns `SERVER_BUSY`
with a five-second retry delay. Downloads remain serialized; independent incremental
reads can overlap, while operations on the same named subscription serialize.

## Durable monitoring and selected files

Checkpoint database version 3 migrates existing tokens in place. For version-1 data
without ancestry, migration protects the newest valid token per authorization,
tool and canonical chat. Current heads are protected from capacity eviction
for 30 days; unchanged heads reuse their token and renew expiry. Superseded versions
remain readable until expiry or capacity eviction. Resuming an older token creates
an independent branch. `CHECKPOINT_STORAGE_FULL` preserves existing positions rather
than evicting a protected head. Do not downgrade a migrated database to the old writer.

New unused baselines have one protected head per authorization and chat; fresh
equivalent starts reuse a token. Continuing a token protects that independent chain.
Version-2 roots may already have been continued without changing their token, so the
version-3 migration conservatively keeps them protected. If storage is already full,
use the [offline preview and cleanup commands](usage.md#inspect-or-clean-checkpoints-unreleased)
after stopping the session owner. Protected cleanup explicitly invalidates selected
tokens; it never resets named watches or stored FloodWait deadlines.

`get_changes` now reports `baseline_created`. `CHECKPOINT_EXPIRED` never silently
starts another baseline: recover the missing date range with history/export before
deliberately starting at the current head.

Use the five named-watch tools for monitoring without retaining per-chat tokens.
Subscriptions persist positions and one unacknowledged response, including text, in a
separate private store. Acknowledge each `batch_id` only after processing it. They have
no automatic expiration or eviction. See [the polling contract](usage.md#monitor-a-folder-or-selected-sources-unreleased).
`get_watch_changes` also accepts `ack_batch_id` for acknowledgement followed by polling.
Channel/supergroup event markers skip unchanged completed reads; existing watches
establish their markers through normal bounded passes. Creation now accepts at most
20 explicit references per call; use folders for larger selections. Existing larger
watches retain their selectors and positions.

`save=true` adds a permanent copy to selected downloads or file exports when
`archive_directory` is configured. The new optional `saved_file` includes its path,
size, hash, timestamp and `metadata_path`; ordinary resources retain their 15-minute expiry.
New copies include versioned source metadata alongside their generated filename.
Old permanent files are left intact without inventing missing provenance.
Permanent copies are managed by the user, not the cache cleaner.

## Compact tool discovery

The default `mcp_schema_mode="compact"` omits optional output schemas from `tools/list`
while retaining input schemas, descriptions, annotations, typed server-side validation,
and identical JSON responses. Use `"full"` to advertise output schemas. The 22-tool
fixture is about 25 KiB in compact mode versus 108 KiB in full mode; server instructions
stay below 1 KiB. These are regression budgets, not limits on legitimate message text.

## Offline regression coverage

Fixtures cover 300/3000-source payloads, 100/300-source searches, and a 2000-dialog
catalog. Scope stays below 2 KiB; complete duplicated MCP results stay below 24 KiB for
10 short messages and 32 KiB for 30 chats in those fixtures. These are measured fixture
budgets, not universal response limits. Text and metadata are not dropped to meet them.
For the 3000-source fixture, complete MCP results measure 7,944 bytes for search and
20,816 bytes for chat listing; the scope object is 896 bytes in each result.
Real offline STDIO, HTTP and proxy tests cover concurrent histories and cursor replay
in both MCP modes. Selected-source live validation and publication remain separate.
