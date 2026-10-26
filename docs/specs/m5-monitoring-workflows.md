# M5: Monitoring workflows (0.5)

Status: proposed for review; not implemented. This milestone makes the existing
retrieval and subscription tools easier to use through reusable MCP prompts.

## Outcome and existing foundation

A new agent session can find a named subscription and produce a source-linked digest
without reconstructing per-chat checkpoints. A user can also request a digest or
vacancy selection for a fixed period in one permitted chat or folder. The calling
agent performs analysis; the server supplies original content and workflow templates.

Version 0.4 already provides named chat/folder watches, persistent positions and pending
batches, acknowledgement with polling, batched unchanged-source checks and permanent
selected files. Reuse these contracts, including their coverage and revision limits.
Do not add `get_changes(folder_id=...)` or a second monitoring implementation.

## Proposed prompt surface

| Prompt | Arguments | Workflow |
| --- | --- | --- |
| `watch_digest` | Required `watch_name`; optional `focus` | Process pending changes from an existing named subscription |
| `period_digest` | Required `date_from`, `date_to`; exactly one of `chat_ref` or `folder_id`; optional `focus` | Read the selected period without consuming subscription positions |
| `vacancy_digest` | Same period/source arguments; optional `criteria` | Apply the period workflow to vacancies with source-backed role, location and application details |

Prompt arguments are MCP strings. Reuse existing name/reference/folder validation;
dates must include a timezone and describe a nonempty `[date_from, date_to)` interval.
Optional `focus` and `criteria` contain at most 1000 characters. Unknown prompt names,
missing required arguments and invalid/conflicting values return MCP parameter errors.
Document names, arguments and examples as a public contract before implementation.

`prompts/list` returns concise descriptions and argument metadata. `prompts/get`
validates arguments and returns bounded instruction messages without Telegram RPCs,
account login, watch creation, acknowledgement, downloads or file writes. Caller input
is clearly separated from template instructions. Retrieving a prompt does not execute
its workflow or grant additional permissions.

As defined by the [MCP prompt specification](https://modelcontextprotocol.io/specification/2025-11-25/server/prompts),
prompts are selected by users; they are not scheduled tasks or instructions guaranteed
to run on every tool call. Keep global server instructions and tool descriptions short.
Provide equivalent copyable recipes for clients without prompt discovery.

## Workflow rules

- `watch_digest` uses `list_watches` to find the existing name and then polls it.
  An unknown name is reported, not silently created or reset. Document initial setup
  separately with the existing `create_watch` tool and an explicit start boundary.
- Process all messages and omissions in a returned batch, including items excluded
  from the requested topic. Produce the batch's source-linked result before
  acknowledging it. Use `ack_batch_id` for acknowledgement with the next poll and
  `ack_watch_changes` for the final processed batch. Failed processing leaves the
  batch pending; a replay is not automatically a new event.
- A null batch ID needs no acknowledgement. Empty output is not proof of completion.
  Follow progress, issues and retry delays; when the client stops early, report partial
  coverage and leave unprocessed data pending. Delivery remains at least once; a
  prompt cannot guarantee exactly-once processing or recovery of an agent's report.
- Period workflows use `get_history` with the same fixed dates on every page.
  For a folder, enumerate permitted sources with `list_chats(folder_ids=[...])`,
  following its cursor, then read each selected source. Existing watch positions
  remain unchanged. Do not use keyword search as proof of complete period coverage.
- Respect text continuation when content is truncated. Bound work through the
  existing tool budgets and report unfinished sources/pages when the agent's budget
  is exhausted. Do not fetch unauthorized sources or use account-wide global search
  to fill gaps. Folder membership and Telegram content are not a transactional snapshot.
- Every digest states its scope, period or polling coverage, outstanding issues and
  omissions. Preserve original links or canonical references. Distinguish observed
  edits and unavailable messages from proven deletions; do not invent missing vacancy
  details or imply that an empty/incomplete scan found no matching posts.

## Grouping forwards

The recipe may group publications with the same non-null canonical original
`forward_origin.message_ref`. Retain every publication reference, source-specific
text/commentary and observed revision. Keep the original source separate from
`saved_from_ref`; hidden origins and copied text are not proven duplicates.

Grouping is presentation performed by the calling agent. It does not remove events,
alter acknowledgement, fetch the original source automatically or promise cross-run
deduplication. Semantic similarity and a persistent deduplication index are outside M5.

## Architecture and compatibility

Keep prompt templates and validation in a focused MCP adapter module, with registration
through the existing server factory. The application/domain retrieval and watch layers
remain the source of access and progress rules; templates do not bypass them.

Extend `connect` to forward `prompts/list` and `prompts/get`, preserving arguments,
pagination and upstream MCP errors. Advertise prompt support only when supported by
the upstream server. Static templates do not require change notifications. Existing
tool/resource behavior and compact/full schema modes remain compatible. M5 requires no
dependency upgrade, elicitation support or new tool. This planning PR does not change
the package version; implementation and release preparation are subsequent work.

## Acceptance criteria

1. Offline tests verify prompt discovery, argument errors, bounded template output and
   no backend or state mutation during prompt retrieval, including without Telegram
   authorization. Tests exercise contracts, not a particular LLM's wording.
2. STDIO, authenticated HTTP and `connect` expose equivalent prompt metadata/results.
   Bridge tests cover cursor/argument forwarding, errors and an upstream without prompts.
   Existing tool/resource contracts and discovery-size checks continue to pass.
3. Focused fake-backed workflow checks cover a saved watch in a new session, pending
   replay after restart, acknowledgement only after processing, empty partial pages,
   retry delays, omissions and text continuation. Reuse existing watch tests/helpers.
   Period recipes cover multiple sources/pages and leave watch positions unchanged.
   These exercises validate tool sequences, not agent obedience; no workflow executor
   is introduced solely for testing prompts.
4. Synthetic digest examples preserve links and distinguish shared original forwards,
   hidden origins, copied text and edits. They make no model-quality guarantee.
5. A short client guide shows initial watch setup, later-session use and period/vacancy
   requests. Record actual client prompt support and a copyable-recipe fallback.
6. Run `make check` and the required GitHub `checks` job. Before release, exercise prompt
   discovery and a workflow in a real supported client on explicitly selected sources,
   recording client/version, completeness and acknowledgement behavior without content
   or credentials. This documentation PR does not claim those live checks have passed.

## Delivery boundary

Review this proposal first, then implement M5 in one focused PR. Mark completion only
after its acceptance criteria are met; publish through the existing release process.

Distribution is separate: [Registry #39](https://github.com/egor-fedorov/telegram-mcp/issues/39),
[autostart #40](https://github.com/egor-fedorov/telegram-mcp/issues/40) and
[MCPB #41](https://github.com/egor-fedorov/telegram-mcp/issues/41).
Extraction, OCR, transcription, Telegram writes, scheduling, notifications and a full
message archive remain outside this milestone. See the [roadmap](../../ROADMAP.md)
for later candidates and [current usage](../usage.md) for released behavior.
