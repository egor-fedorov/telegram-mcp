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
| M2 | Planned | Forum topic and pinned-message navigation; richer message data, including albums, reactions, polls, and available counters | M0 |
| M3 | Planned | Downloading selected attachments with size limits and file delivery to the calling client | M0; use album structure from M2 |
| M4 | Planned | New messages and available changes, persistent checkpoints, and export with source references | M0; M2–M3 for their respective content types |

M0 was implemented in commit `b167044`. At that checkpoint, `make check` passed:
Ruff format/check, ty, and 110 offline tests. This completion record does not include
validation against a live Telegram account.

M1 adds the two discovery tools, an independent opt-in permission, compact metadata,
and MCP error propagation with retry delays. `make check` passed with 225 offline
tests, including both MCP modes and existing retrieval contracts. Live Telegram
validation remains separate. M2 is next for detailed specification; its scope
remains planned until that step is complete.

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
