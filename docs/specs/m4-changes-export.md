# M4: Changes and export

Status: implemented for 0.4.0. Selected-source live checks are recorded in
[0.4 validation](../validation-0.4.md); publication follows the release procedure.
Current examples and operational limits are in [usage](../usage.md#changes-and-exports).
This is the original milestone contract. The current guide supersedes its FIFO
retention rule and single-chat-only monitoring scope with protected checkpoint heads
and named subscriptions.

## Accepted boundary

One explicit chat per checkpoint; ordinary source-read policy applies to every call.
Track newly available IDs and current revisions of the last 100 known messages.
Export history ranges and change pages as JSONL or Markdown with original references.
No background polling, account-wide update journal, multi-source selection, search
export, binary download, automatic joins, writes or persistent message archive.

These are observations of currently available content. Intermediate edits, messages
deleted before observation, edits outside the rolling window and a complete deletion
history are not recoverable. Counter/reaction/poll-vote changes are not content edits.

## Tools and continuation

`get_changes(chat_ref=None, checkpoint=None, limit=30, output="inline")` accepts exactly
one selector. A source reference creates a baseline at the current head, fingerprints
up to 100 messages and returns no historical messages. A checkpoint resumes a partial
pass or starts the next pass when its previous response was complete. The limit is
1–100 observed changes; file output uses `jsonl` or `markdown` instead of `inline`.

Each pass fixes its upper ID, rechecks saved IDs in ascending order, then reads new
IDs in ascending order through that boundary. The persisted phase, recheck index and
history position advance only through processed records. Unreturned payloads are not
stored; the next call rereads their current versions. Arrivals beyond the fixed upper
ID wait for the next pass. Inline text retains existing clipping/continuation semantics.

Changes contain `new`, `updated` or `unavailable`, a canonical reference, revision,
observation time and the current message when available. An unavailable message is
reported once per observed transition, not on every poll. Reappearance is `updated`.
Compare the last applied revision per message when processing repeats; this is not a
globally unique, exactly-once event log. Recheck IDs/counts describe actual coverage.

`export_history(chat_ref=None, date_from=None, date_to=None, checkpoint=None,
format="jsonl")` starts with a source and optional aware dates, or resumes with only
the checkpoint and desired format. Lower date is inclusive; upper date is exclusive
and capped at the initial current time. Upper date/ID remain frozen after restart.
Parts preserve ascending message IDs, with at most 1000 records or 20 MiB, subject
to existing RPC/time budgets. Complete history has a null `next_checkpoint`; a
complete change pass still returns a checkpoint for the next poll.

The result distinguishes returned/exported records, protected omissions, issues and
traversal completion. Empty parts can have a continuation. Completion never claims
that omitted content was exported or that every historical edit was checked.

## Persistent state and failures

Checkpoints are opaque immutable snapshots in a separate private SQLite database,
isolated by canonical session path and bound to a digest of the current authorization.
They contain versioned positions, bounds, hashes and availability flags, not message
text, entities, access hashes or serialized Telegram objects. The database also stores
session-wide M4 retry deadlines. It never loads pickle data.

Writes are atomic. Only a successfully constructed response publishes its new token;
the original remains reusable until expiry/eviction. Replays can repeat records or
observe newer versions. Clients save the new token after processing the response.
Independent clients need independent tokens, not a shared mutable subscription.

Retention is up to 30 days, with 4096 snapshots / 64 MiB of serialized state. Eviction
removes expired snapshots first, then oldest snapshots. Missing/expired/evicted tokens
return `CHECKPOINT_EXPIRED`. Invalid tokens, mismatched tools/authorizations, damaged
state and unsupported schemas fail explicitly rather than starting a new baseline.

Continuation rechecks current authorization and source policy. Public-channel grants
remain request-local, require fresh public broadcast metadata, and never extend to
private dialogs, groups, linked discussions or forwarded sources. Failed access/catalog
checks return no saved content and do not consume the original token.

Temporary retrieval failures preserve successful progress and report an unfinished
checkpoint. The failed operation is not repeated inside that call. FloodWait deadlines
use UTC persistence and are checked before M4 authorization/reconnection requests;
old tokens, new selections and restart cannot bypass them. Cleared transient issues
do not contaminate later successful responses. Tool-level failures/cancellation leave
the original token available; no partial export file is exposed.

## Export data and delivery

JSONL format version 1 has a `manifest`, individual `message`/`change`/`omitted`
records, and a `summary`. Source metadata appears once in the manifest; message
references are objects with `chat_id` and `message_id`. Messages preserve full text,
original UTF-16 entities, source URLs, dates, replies, forwards and attachment metadata.
Markdown includes source links or canonical references, authors, dates, original text
in safely sized code fences, metadata and coverage. No summary replaces an original.

Chat-wide saving restrictions reject the export. Protected or expiring individual
messages produce explicit omissions without their content. Binaries, previews and
external URLs are not fetched. Byte limits accept only whole records; oversized single
records fail rather than truncating or silently skipping. File summaries record partial
coverage and continuation. Separate calls do not constitute an atomic Telegram snapshot.

Tool responses contain only compact controls, file metadata and ResourceLink when file
output is selected. The file result includes format, name, MIME, byte size, SHA-256,
local path, resource URI and expiry. JSONL/Markdown resources are UTF-8 MCP text resources,
available through STDIO, loopback HTTP and `connect`, without resource enumeration or
a separate HTTP file endpoint.

Exports have a separate private cache: 20 MiB per part, 100 MiB / 32 files total,
15-minute expiry, 60-second cleanup and shutdown cleanup. Generated filenames and a
process lease isolate owners. Crash leftovers are removed on the next initialization;
failed deletions remain accounted and are retried. Resource reads check current source
access/protection and authorization binding, then verify stored size/hash. They deliver
the saved observation without refreshing every message. Delivered copies cannot be revoked.

## Architecture and acceptance

Domain modules own checkpoint contracts, revision fingerprints and file rendering.
Application services own polling/export state machines through incremental-read,
checkpoint-store and export-store ports. Adapters own Telegram requests, SQLite and
private temporary files. Existing cursor and attachment algorithms remain distinct.
No dependency/version changes or broad pagination refactor are required.

Offline acceptance covers restart and replay; fixed ID/date boundaries; partial pages;
Unicode and text limits; available/unavailable/restored transitions; rolling-window
limits; volatile metadata exclusion; access revocation; FloodWait/network recovery;
state corruption/retention; file quotas/integrity/cleanup; source protection; unchanged
existing tool schemas; and real STDIO/HTTP/bridge resource delivery in both MCP modes.

Run `make check`, review the diff, and require GitHub `checks` before squash merge.
Live Telegram validation remains a separate selected-source activity before publication.
Do not claim live edit/deletion coverage based on offline fixtures or a baseline-only poll.
