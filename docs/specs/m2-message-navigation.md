## M2: Topic navigation and richer message data

### Goal and delivery order

Help agents navigate a permitted conversation and preserve the structure of its
messages. Deliver the following work in separate PRs; M2 is complete only when all
four work items meet their acceptance criteria.

- [x] M2.1 — List forum topics and retrieve chat-wide pinned messages.
- [x] M2.2 — Read a selected topic's history, including General, and filter pins by topic.
- [x] M2.3 — Preserve available message counters, reaction summaries, and poll data.
- [x] M2.4 — Retrieve the messages of a selected album using its original grouping.

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

### M2.3: Counters, reaction summaries, and polls

Extend the existing `Message` output; keep all 13 tools, their arguments, read
permissions, and the existing content-type filters. Poll messages retain
`content_type="other"` and gain a structured `poll` payload.

| Field | Contract |
| --- | --- |
| `view_count` | Telegram's available view counter; `null` when absent |
| `forward_count` | Telegram's available forward counter; `null` when absent |
| `reply_count` | The supplied reply/comment total; `null` without reply metadata |
| `reactions` | A nullable `ReactionSummary` with native-order counts and reduced-result status |
| `poll` | Nullable poll definition, formatted options, and available results |

Counters are nonnegative integers. Preserve known zero separately from unknown;
never derive totals from recent-user lists, sum option votes to invent a voter total,
or make additional requests to populate missing data. Reply metadata does not grant
access to a linked discussion or trigger a read of that chat.

`ReactionSummary` has `is_min`, `reactions_as_tags`, and `results`. The tags flag
distinguishes Saved Messages labels from ordinary reactions. Each result carries a
reaction `type`
(`emoji`, `custom_emoji`, `paid`, `empty`, or `unknown`), optional `emoji` or string
`document_id`, `count`, nullable `is_chosen`, and nullable `chosen_order`. An absent
summary is `null`; a present empty result list remains `[]`. Reduced (`is_min=true`)
data has unknown personal selection. Full emoji/custom-emoji rows preserve selection
and its order, including order zero. Paid, empty, and unknown reaction types leave
personal selection unknown. Paid reaction counts represent Stars rather than people;
do not report a combined number of reacting users.

`PollData` carries a string ID, `question: FormattedText`, native-order `answers`,
closed/quiz/public-voter/multiple-choice flags, open-answer/revoting/shuffle/hidden-result
flags, optional closing interval/date, and nullable `results`. Every answer has
`option_id` and formatted `text`. Encode opaque Telegram option bytes as lowercase hex,
including empty or non-UTF-8 values; never use option position or text as identity.
IDs are meaningful within the poll. Preserve duplicate labels and result rows whose
option is not present in the supplied definition.

`PollResults` has `is_min`, nullable `total_voters`, nullable `answers`, and nullable
formatted `solution`. Each answer result retains `option_id`, nullable `voter_count`,
nullable `is_chosen`, and nullable `is_correct`. Missing results and an empty result
vector are distinct; no missing option row or count becomes zero. Reduced results
withhold current-user selection. Quiz correctness is unknown until Telegram reveals
it; a reported correct option establishes revealed results, while non-quiz polls
have no correctness value. Preserve absent versus empty solution text.

`FormattedText` preserves text and entities with their original UTF-16 offsets.
The shared `Entity` contract also preserves available formatting payloads: URLs,
preformatted language, mention/custom-emoji IDs as strings, blockquote state,
formatted-date values/flags, and replacement text. Use the same mapper for message
bodies, poll questions, options, and solutions. Existing `text_offset`, clipping,
and `next_text_offset` continue to apply to `Message.text`; formatted poll fields
are separate metadata and remain intact.

Normalize only fields already present in message responses. No view-counter updates,
reaction lists, poll-result refreshes, voting, reacting, paid requests, or background
tracking are introduced. Omit embedded recent replier/reactor/voter lists, paid-reactor
leaderboards, and option-author identities as well as any access hashes. Poll media
attachments and detailed voter/statistics APIs remain outside this step. Metadata
can change between requests; this does not provide a historical engagement snapshot.

Acceptance checks:

1. Raw Telegram fixtures distinguish missing counters from zero and preserve available
   views/forwards/replies without reading linked discussions or actor lists.
2. Reaction tests cover emoji/custom/paid/empty/unknown types, native order, absent and
   empty summaries, chosen order zero, and reduced results without invented selection.
3. Poll tests cover regular and quiz polls, open/closed and hidden results, formatted
   Unicode text, arbitrary binary IDs, reordered/orphan results, absent versus empty
   vectors/solutions, and unknown versus zero vote counts.
4. Shared entity conversion preserves source offsets and type-specific payloads. Original
   message text, source links, dates, forwarding/reply/topic metadata, and attachments
   remain compatible. No actor identity lists or access hashes enter the new fields.
5. Existing message retrieval paths expose the same fields, including clipping and
   text continuation. Actual MCP schemas/dispatch work in both modes without new tools,
   argument changes, additional RPCs, or a new `poll` content-type filter.
6. README, MCP descriptions, and roadmap are updated; `make check` and GitHub `checks`
   pass without a Telegram account. Live validation remains a separate task.

Primary references: [message fields](https://core.telegram.org/constructor/message),
[reply counters](https://core.telegram.org/constructor/messageReplies),
[reaction summaries](https://core.telegram.org/constructor/messageReactions),
[reaction counts](https://core.telegram.org/constructor/reactionCount),
[paid reactions](https://core.telegram.org/api/reactions#paid-reactions),
[polls](https://core.telegram.org/constructor/poll),
[poll answers](https://core.telegram.org/constructor/pollAnswer),
[poll results](https://core.telegram.org/constructor/pollResults), and
[answer results](https://core.telegram.org/constructor/pollAnswerVoters).

### M2.4: Selected-album retrieval

Add `get_album(ref: str | MessageRef, scan_limit: int = 30) -> AlbumResult`.
The required reference selects an existing album member using the same Telegram links,
`chat_id:message_id` strings, or structured references as `get_messages`. `scan_limit`
is a strict integer from 1 to 100 and bounds each direction independently.

Resolve the canonical chat and check the current retrieval policy before reading the
anchor. Explicit public-channel access applies only to that chat and request. Do not
follow linked discussions, replies, forwards, or identically numbered albums in other
chats. An unavailable anchor yields `MESSAGE_UNAVAILABLE`; an available message without
an album ID yields `NOT_AN_ALBUM`. Neither case starts a neighborhood scan.

Fetch at most one raw history window before and one after the anchor, each containing
at most `scan_limit` Telegram rows. Use a dedicated backend operation so Telethon's
history iterator cannot silently refill the window or discard unavailable placeholders.
Each attempted direction performs one history RPC, apart from the existing bounded
transport retries. Do not add content-type or topic filters. The service inspects the
whole window, retaining only the anchor's canonical chat and album ID; unrelated
messages can occur between album members. Numeric ID gaps and Telegram's send-time
album size limit do not establish retrieval completeness.

The result contains:

| Field | Contract |
| --- | --- |
| `anchor_ref`, `album_id` | Canonical selected message and its chat-scoped grouping ID |
| `messages` | Unique original messages, including the anchor, ordered by increasing message ID |
| `order` | Always `oldest_first`, referring to message order rather than edited timestamps |
| `completeness` | Always `unknown`; Telegram provides no manifest of the original album |
| `scan_limit` | Requested maximum raw rows in each direction |
| `scanned_before`, `scanned_after` | Raw rows examined, including nonmembers and unavailable placeholders |
| `before_exhausted`, `after_exhausted` | `true` for an observed history end, `false` when the end is not established, `null` for an unfinished/unattempted direction |
| `issues` | Actual retrieval failures and unavailable-row diagnostics |

Short channel or sliced history responses do not prove exhaustion. Even observed
ends in both directions cannot establish the original album's size or recover missing
members. An actual unavailable row may be reported with `MESSAGE_UNAVAILABLE`, but its
album membership is unknown. Do not invent unavailable members from gaps or assert
that they were deleted.

Bound the response to at most `1 + 2 * scan_limit` messages (at most 201), 100,000
message-text characters in total, and 12,000 per message. Preserve each caption,
attachment, entity, source link, date, and metadata separately; use existing
`next_text_offset` and `get_messages` for longer text. Do not combine captions into a
fabricated message or download attachment bytes.

After a scan failure, return the available members with an issue and leave the failed
direction's exhaustion unknown. A retryable failure also stops subsequent scans in
that call. There is no album cursor or persistent retry state: repeat the original
call after any `retry_after_seconds`, rechecking access and rereading the anchor.
Initial catalog, resolution, access, and anchor failures remain tool errors.

Acceptance checks:

1. Cover anchors at the beginning, middle, and end; sparse and interleaved IDs;
   duplicate or unordered responses; and identical album IDs in different chats.
2. Test strict scan bounds, one history RPC per direction, directional offsets,
   short/empty responses, unavailable rows, unexpected peers, and message-ID boundaries.
3. Preserve independent captions and all message metadata, including Unicode text
   continuation and the aggregate text budget, without mutating source messages.
4. Verify current allowlist/public-channel access, revocation, and no implicit reads
   of linked sources. Cover unavailable and non-album anchors before scanning.
5. Preserve partial results and retry delays after FloodWait, network, and server
   failures without waiting, same-call retries, or false completeness claims.
6. Add the fourteenth read-only MCP tool, validate both protocol modes, update README
   and roadmap, and pass `make check` and GitHub `checks` without a Telegram account.

Primary references: [message grouping](https://core.telegram.org/constructor/message),
[albums and captions](https://core.telegram.org/api/files#albums-grouped-media),
[history offsets](https://core.telegram.org/api/offsets), and
[Telethon's warning about interleaved album messages](https://docs.telethon.dev/en/v2/developing/migration-guide.html).
