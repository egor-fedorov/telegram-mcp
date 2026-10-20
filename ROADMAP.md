# Telegram MCP roadmap

The project aims to provide general-purpose Telegram access for agents: discovering
sources, retrieving original messages and files, tracking changes, and optionally
acting on behalf of the user. Job searches, news research, and work conversations
use the same tools; the calling agent performs content analysis.

This document records priorities, milestone scope, and acceptance criteria.
Planned milestones will be refined before implementation.
No delivery dates have been assigned. See [README](README.md) for current features
and setup instructions.

## Milestones

| ID | Status | Outcome | Dependencies |
| --- | --- | --- | --- |
| M0 | Done | Reliable retrieval: channel references, explicit public-channel access, batched messages, recovery from temporary failures, and fresh authorization status | Base MCP server |
| M1 | Done | Public-channel discovery and similar sources, followed by existing retrieval tools | M0 |
| M2 | In progress | Forum topic and pinned-message navigation; richer message data, including albums, reactions, polls, and available counters | M0 |
| M3 | Planned | Downloading selected attachments with size limits and file delivery to the calling client | M0; use album structure from M2 |
| M4 | Planned | New messages and available changes, persistent checkpoints, and export with source references | M0; M2–M3 for their respective content types |

M0 was implemented in commit `b167044`. At that checkpoint, `make check` passed:
Ruff format/check, ty, and 110 offline tests. This completion record does not include
validation against a live Telegram account.

M1 adds the two discovery tools, an independent opt-in permission, compact metadata,
and MCP error propagation with retry delays. `make check` passed with 225 offline
tests, including both MCP modes and existing retrieval contracts. Live Telegram
validation remains separate.

M2.1 adds forum topic listings and chat-wide pinned-message retrieval. `make check`
passed with 325 offline tests, including both MCP modes, native topic pagination,
access revocation, and temporary-failure recovery.

M2.2 adds optional topic selection to history and pinned-message retrieval, including
General, with fresh topic checks and preserved message membership. `make check`
passed with 490 offline tests. Live Telegram validation remains separate. M2 remains
in progress; the next PR is M2.3, available counters, reaction summaries, and poll data.

The row order expresses priority, not a restriction on independent work. “Planned”
means an agreed direction, not a complete technical specification.

## M1: Public-channel discovery and similar sources

### Goal and user workflow

An agent searches for candidate sources, presents their names and links, and checks
posts from selected channels through the existing retrieval tools. It can expand
the selection by requesting channels similar to a specific public source.

Example: search for channels on a topic → select candidates → read their posts from
the past week → compare the sources with links to original messages. The calling
agent evaluates relevance and writes recommendations.

### New tools

| Tool | Input | Behavior |
| --- | --- | --- |
| `search_public_channels` | `query: str`, `limit: int = 20` | Search Telegram entities and return only public broadcast channels |
| `get_similar_channels` | `chat_ref: str`, `limit: int = 20` | Return similar public channels for one explicitly specified public channel |

- `limit` ranges from 1 to 100, matching existing tools.
- `query` must be nonempty after trimming whitespace and contain at most 1,000
  characters. Do not invent a minimum length beyond that: translate Telegram's
  rejection of a short query into a clear argument error.
- `chat_ref` is required and uses the shared ID, username, and channel-link resolver.
  Preserve the suggestion to use a username or link when a numeric ID is unknown.
  Personalized recommendations without a source channel are outside M1.
- Return compact metadata: ID, title, active username, public URL, and participant
  count only when supplied by Telegram. An unknown count is `null`, not zero.
  Do not populate unknown mute, unread, or folder state using defaults from the
  dialog-oriented `Chat` model.
- Return the actual number of results. For recommendations, also preserve Telegram's
  total count when available and indicate local truncation by `limit` separately.
  Telegram's count is not the size of a complete directory of matching channels.
- Neither tool has a cursor. Do not promise exhaustive coverage of Telegram.

### Access policy and scope

Add `allow_public_channel_discovery = false` to configuration, the local `configure`
command, the example configuration, and `get_status`. When disabled, the new tools
return `FEATURE_DISABLED` before contacting Telegram, regardless of
`allow_all_chats` or `allow_public_channel_reads`.

Discovery and retrieval permissions are independent:

- `allow_public_channel_discovery` permits candidate metadata retrieval.
- The existing read policy (`allow_all_chats`, allowlists, or
  `allow_public_channel_reads`) governs message retrieval.
- Discovering a channel does not add it to the allowlist or expand `list_chats`,
  message searches without explicit sources, or access to linked discussions.

Only accessible public broadcast channels with a primary or active additional
username are eligible. Exclude users, bots, groups, private channels, and forbidden
channels. Validate the source of similar-channel recommendations using fresh
metadata. Existing M0 access checks still apply before reading a discovered channel.

M1 does not include public-group discovery, global post search, joining channels,
sending messages, paid requests, automatic candidate-post retrieval, a local archive,
or a semantic index.

### Implementation approach

Preserve the MCP → Service → Backend → Telethon boundary:

- The backend calls `contacts.SearchRequest(..., broadcasts=True)` and
  `channels.GetChannelRecommendationsRequest`, converting TL objects into independent
  models. It verifies the real channel type, broadcast/supergroup flags, and public
  username before producing a public-channel result. Both requests have been
  verified in the pinned Telethon 1.45.0 package;
  no dependency upgrade is planned for this milestone.
- The service validates settings and arguments, deduplicates by ID, and applies
  the limit. The MCP adapter defines the tools,
  schemas, and documented restrictions.
- For search, traverse peers from `my_results` and `results` in their returned order,
  resolving them through the `chats` metadata. Validate channel type and public
  visibility even when a server-side filter is used.
- Use metadata already present in the response instead of fetching full details for
  every candidate. Validate a recommendation source under the discovery policy,
  without calling `Service.get_chat`, which requires retrieval permission.
- Filtering may leave fewer than `limit` results; do not silently issue additional
  searches to fill the result. For recommendations, apply `limit` locally because
  the underlying request has neither `limit` nor `offset`.
- Preserve server-limited recommendation results without trying to bypass the limit.
  A missing count does not imply zero results or exhaustive coverage.
- Reuse existing timeouts, error classification, and FloodWait handling. On temporary
  failure, return its cause and any retry delay; the caller retries with a new tool
  call. Do not disguise failures as empty results.
- At the MCP boundary, expose expected domain errors as JSON in error text, including
  `code`, `message`, and an optional `retry_after_seconds`. Keep unexpected failures
  redacted by the SDK. This applies consistently to discovery and retrieval tools.

Primary references: [entity search](https://core.telegram.org/method/contacts.search),
[similar channels](https://core.telegram.org/api/recommend), and
[channel fields](https://core.telegram.org/constructor/channel).
[Global post search](https://core.telegram.org/api/search#posts-tab) has separate
capabilities and constraints and is outside this milestone.

### Work items

- [x] M1.1 — Result models, backend interfaces, and Telethon requests.
- [x] M1.2 — Discovery policy, configuration, CLI, and the two MCP tools.
- [x] M1.3 — Offline tests, including real MCP protocol dispatch.
- [x] M1.4 — README, example configuration, `enabled_tools`, and a
  “discover → select → read” example; complete `make check`.

### Acceptance criteria

1. Search and recommendations return IDs and links usable by `get_chat` and
   `get_history`; original messages remain readable only with retrieval permission.
2. Disabled discovery makes no Telegram RPCs, including authorization checks,
   catalog retrieval, or source resolution. Test all combinations of discovery
   and public-read settings, including discovery without permission to read posts.
3. Mixed Telegram results are filtered correctly. Cover active additional usernames,
   unavailable sources, duplicates, and preservation of result order.
4. Cover empty results, empty and too-short queries, invalid references, boundary
   values of `limit`, and unknown participant counts.
5. Cover both recommendation responses, `messages.Chats` and `messages.ChatsSlice`,
   a total count larger than the available result set, and local truncation.
6. FloodWait, network, and server failures retain a clear cause. There are no hidden
   joins, payments, post reads, or access-policy changes.
7. The existing nine tools remain compatible. New tools have correct MCP schemas
   and read-only annotations. Their descriptions and the server instructions
   distinguish bounded discovery results from paginated history retrieval.
   The full `make check` passes.

Offline tests are required and must not need an account. Any live validation is a
separate check using public sources selected by the user after local configuration.

## M2: Topic navigation and richer message data

### Goal and delivery order

Help agents navigate a permitted conversation and preserve the structure of its
messages. Deliver the following work in separate PRs; M2 is complete only when all
four work items meet their acceptance criteria.

- [x] M2.1 — List forum topics and retrieve chat-wide pinned messages.
- [x] M2.2 — Read a selected topic's history, including General, and filter pins by topic.
- [ ] M2.3 — Preserve available message counters, reaction summaries, and poll data.
- [ ] M2.4 — Retrieve the messages of a selected album using its original grouping.

Keep STDIO, the MCP → Service → Backend boundary, and the locked dependencies.
No topic creation, pinning/unpinning, voting, reactions, membership changes,
attachment downloads, or background synchronization are part of M2.

### M2.1: Topic listings and chat-wide pins

| Tool | Input | Result |
| --- | --- | --- |
| `list_topics` | `chat_ref: str`, `query: str \| None = None`, `limit: int = 30`, `cursor: str \| None = None` | A `TopicPage` with forum topic metadata and continuation |
| `get_pinned_messages` | `chat_ref: str`, `limit: int = 30`, `cursor: str \| None = None` | The existing message `Page`, newest messages first |

Both tools accept the shared numeric ID, username, and channel-link references.
Limits are 1–100. Topic queries use Telegram's name search, are trimmed, and must
contain 1–1,000 characters when supplied. An omitted query lists all topics.

Access follows the existing retrieval policy. Topic listings require an ordinary
allowlisted forum supergroup, a permitted folder, or `allow_all_chats`. Public-channel
read and discovery settings do not authorize groups. Add `Chat.is_forum` for supported
ordinary forum supergroups; bot forums and monoforums remain outside this step.
Chat-wide pins may use the explicit public broadcast-channel read exception.
Linked discussions retain their independent permissions.

`ForumTopic` includes its ID and chat ID, available title and creation time, General,
closed/hidden/pinned state, icon metadata, unread count, and source references.
`root_message_ref` identifies the topic-creation message for ordinary topics;
General (`id=1`) has `root_message_ref=null` and `url=null`. `top_message_ref` identifies the
last message when known. Use `get_messages` for these message references and
`get_thread` for ordinary topic roots. Dedicated General-topic history is M2.2.

`TopicPage` contains `topics`, `next_cursor`, `scope`, `issues`,
`telegram_total_count`, and the Telegram order mode (`creation_date` or
`last_message_date`). Preserve Telegram's order and deduplicate topic IDs during
one traversal. Deleted topics retain only their known identity and deletion flag.
Reduced topic records expose only trustworthy fields; other fields remain `null`.
Do not fetch details or message content separately for every topic.

Use the pinned Telethon `messages.GetForumTopicsRequest`. Maintain all three native
offsets: date, last message ID, and topic ID. The response's `order_by_create_date`
flag determines whether the offset date comes from the topic or its last message.
Match that last message by both peer and ID. A short page is not proof of exhaustion;
continue until an empty native page. If a safe continuation cannot be derived from
the final topic, report `PAGINATION_INCOMPLETE` and do not claim complete coverage.

One topic-list call fetches at most one native topic page. Cursors bind the original
selector and query to the canonical chat ID; changing `limit` is allowed. Revalidate
access and forum status before releasing any stored buffer. Temporary failures retain
the position and a cursor, honor FloodWait without sleeping, and clear on a successful
retry. Permanent errors remain visible. Repeated offsets or a changed order mode
terminate with an explicit incomplete result. Topic titles, ordering, and pin membership
can change during traversal; this is not a transactional snapshot.

Chat-wide pinned-message reads use `InputMessagesFilterPinned` and reuse existing
message normalization, text budgets, source links, pagination, and retry handling.
Do not combine Telethon's `reply_to` iterator with this filter: topic-scoped pin
queries require a separate implementation in M2.2.

Acceptance checks:

1. Raw Telegram fixtures cover both topic order modes, multi-page and short-page
   traversal, shuffled top messages, General, deleted/reduced records, missing cursor
   metadata, and rejection of foreign-peer data.
2. Cursors preserve order and identity, tolerate a changed page size, stop on a
   non-advancing offset, and cannot bypass access revocation or a changed forum type.
3. FloodWait and connection/server failures preserve retryable state; calls during
   the cooldown make no premature page request. Saved checkpoints remain reusable
   after a failed catalog or access check.
4. Pins return only pinned messages, preserve original text/entities/links, support
   multiple pages and recovery, and follow ordinary or explicit-public read permissions.
5. The MCP tools have read-only annotations and validated schemas in both protocol
   modes. Existing tools remain compatible; README and `enabled_tools` are updated.
   `make check` and the PR's GitHub `checks` job pass without a Telegram account.

Primary references: [forum behavior](https://core.telegram.org/api/forum),
[topic pagination](https://core.telegram.org/constructor/messages.forumTopics),
[topic fields](https://core.telegram.org/constructor/forumTopic), and
[pinned-message filter](https://core.telegram.org/constructor/inputMessagesFilterPinned).

### M2.2: Selected-topic history and pins

Extend the existing tools without changing their default behavior or adding tools:

| Tool | New input | Result |
| --- | --- | --- |
| `get_history` | `topic_id: int \| None = None` | The selected forum topic's history over the existing date interval and order |
| `get_pinned_messages` | `topic_id: int \| None = None` | Pinned messages in the selected forum topic, newest first |

An omitted `topic_id` keeps chat-wide retrieval. When supplied, it must be an integer
from 1 to 2,147,483,647; booleans, strings, and fractional values are rejected.
`get_history` retains its exactly-one-of-`chat_id`/`chat_ref` rule and existing positional
arguments. `get_pinned_messages` retains its required `chat_ref`. General is topic `1`;
other topic IDs come from `list_topics`.

Add optional `Page.topic_id` to identify the selected topic and `Message.topic_id` to
preserve the message's topic when supported forum metadata makes it known. A topic ID
is scoped to its chat, not a global identifier. General does not imply a root-message
reference to message `1`. Preserve actual reply references and creation/service messages
returned by Telegram; do not fetch or fabricate a root merely to populate history.

Before each selected-topic call, including continuation, require ordinary read access,
refresh chat metadata, verify a supported forum supergroup, and resolve the exact topic
with `messages.GetForumTopicsByIDRequest`. A missing, deleted, or mismatched topic returns
`TOPIC_UNAVAILABLE`; a non-forum returns `FORUM_UNAVAILABLE`. Closed or hidden topics
remain readable if Telegram allows them. Public-channel discovery/read settings do not
authorize forum groups. Failed access or topic checks release no saved buffer, and a
previous cursor remains reusable after a temporary failure.

Use raw `messages.GetRepliesRequest` with the selected topic ID for history, including
General. This matches TDLib's forum-topic history handling; it does not treat General
as an ordinary reply thread to message `1`. For pins, use `messages.SearchRequest` with
`top_msg_id` and `InputMessagesFilterPinned`. Do not combine Telethon's `reply_to` iterator
with the pin filter, which it ignores. No dependency update is required.

The backend reads at most one native message page per fetch. Preserve existing
per-call RPC/time limits, text budgets, half-open date intervals, and both history
orders. Maintain a fixed upper date throughout traversal. A short native page alone
does not prove exhaustion; advance on trustworthy native message IDs, including
unavailable-message placeholders, and continue until an empty page or the date boundary.
Filtered empty pages may retain a cursor. Unsafe or unrelated returned data is omitted
with an incomplete-result issue instead of expanding the requested scope.

Derive topic membership from the forum flag, creation action, and reply header.
`reply_to_top_id` identifies an ordinary topic only when the header's `forum_topic`
flag says so; a General reply may have a normal thread root of its own. Preserve
the local topic ID when a message replies across chats, and retain the separate
cross-chat parent reference. Malformed topic headers remain unknown. No parent-message
lookups are needed to classify a reply, including when its parent is unavailable.

Cursors bind the original selector, canonical chat ID, topic, date interval, and
order. Page size may change. Page-fetch FloodWait, network, and server failures retain
the position and continuation; no failed stream is retried in the same call or before
its wait expires. Clear resolved temporary issues and preserve permanent ones.
Telegram topic metadata, message membership, and pins can change during traversal;
this is not a transactional snapshot.

Acceptance checks:

1. Existing chat-wide history, pins, and thread tools remain compatible; both MCP modes
   expose optional bounded integer topic IDs and reject invalid values before backend calls.
2. General and ordinary topics return only their own messages/pins. Raw fixtures cover
   creation events, nested General replies, cross-chat replies, unavailable parents,
   foreign peers, unknown membership, and original author/text/entity/media data.
3. Raw request shapes and multi-page traversal cover both history orders, short pages,
   empty/placeholder pages, date boundaries, fixed upper dates, and non-advancing results.
4. Access revocation, forum conversion, topic disappearance, and source/topic changes
   cannot release saved data or redirect a cursor. A failed metadata check leaves the
   original checkpoint usable; closed/hidden metadata does not disable permitted reads.
5. Temporary page-fetch failures preserve continuation and ordering without premature
   FloodWait retries. Text budgets and Unicode continuation remain unchanged.
6. README and roadmap are updated; `make check` and GitHub `checks` pass offline.

Primary references: [forum semantics](https://core.telegram.org/api/forum),
[topic lookup](https://core.telegram.org/method/messages.getForumTopicsByID),
[thread history](https://core.telegram.org/method/messages.getReplies),
[scoped message search](https://core.telegram.org/method/messages.search),
[reply headers](https://core.telegram.org/constructor/messageReplyHeader), and
[TDLib forum history](https://github.com/tdlib/td/blob/master/td/telegram/MessagesManager.cpp).

### M2.3–M2.4 acceptance boundaries

Before each remaining PR, finalize its exact model and tool schemas in this document.

- **Rich metadata:** read counters, reaction summaries, and poll questions/options/results
  from already returned Telegram objects. Unknown values remain `null`; zero is a known
  value. Preserve formatted question/answer text and stable option identifiers. Do not
  fetch voter/reactor identities or perform extra requests for every message.
- **Albums:** use `album_id` with the chat ID, never globally. Return original messages
  in message order, preserving each caption, attachment, and source link. Bound scans
  and response size; explicitly report unavailable items or uncertain completeness.
  Do not fabricate a combined message or download attachment bytes.

## Later: ideas requiring a separate decision

- Saved searches, local source collections, and unread-message workflows.
- Selective archives, full-text search, and semantic search with explicit index coverage.
- Document text extraction, OCR, and audio transcription through optional processors.
- Scheduled monitoring, notifications, and analytics for selected sources.
- An optional write mode: drafts, sending and editing messages, scheduled publishing,
  reactions, folder management, and channel membership changes.
- An optional administration mode with account-permission checks.
- Authenticated HTTP, isolated account profiles, and multiple clients sharing one
  session-owning process per account.

Reading, downloading, sending, and administration must have separate permissions.
Preserve original content and source links; summaries must not replace originals.
New capabilities must not require a built-in LLM or a persistent message database
for basic retrieval.

## Working process

1. Select a bounded work item from the next milestone and clarify its acceptance criteria.
2. Implement it on a separate Conventional Branch such as `feature/topic-navigation`
   or `bugfix/cursor-recovery`, preserving existing contracts or explicitly documenting
   any required change. Use `chore/` for documentation and maintenance work.
3. Add checks for new behavior, run `make check`, and open a focused pull request to
   `main` with a Conventional Commits title. Review the diff and GitHub CI results.
4. Commit agreed changes and update completed roadmap items with the implementation.
   Squash-merge after the `checks` job passes, then create the next branch from updated
   `main`. Mark a milestone done only when its acceptance criteria are met.
5. Before starting the next milestone, add its detailed description here. If task
   tracking grows, move individual work items to Issues and retain goals, ordering,
   and issue links in this roadmap.

Track `ROADMAP.md` in Git. Do not include credentials, private conversations, or
transient execution logs in this document.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the complete contribution workflow.
