# Architecture

The server separates domain contracts, application policy and concrete adapters.
This document describes the architecture after the R6 consolidation, including
attachment downloads, durable monitoring and export capabilities. The [roadmap](../ROADMAP.md) and [changelog](../CHANGELOG.md) track delivery;
[completed specifications](specs/README.md) preserve milestone contracts.

## Dependency rules

| Layer | Responsibility | Allowed internal dependencies |
| --- | --- | --- |
| `domain` | Models, errors, read-policy snapshots, folder matching and text clipping | Domain only |
| `application` | Use cases, access decisions, typed requests, pagers and dependency ports | Application and domain |
| `adapters` | MCP/CLI, Telethon, configuration, cursor/download storage, session leases and real time | Application, domain and other adapters |
| `bootstrap` | Select concrete adapters and assemble the application | All layers |

Outbound adapters cannot import the MCP, CLI or transport entry adapters. The stable
`telegram_mcp.cli` entry point delegates to bootstrap. Domain and application code do
not import Telethon, the MCP SDK, concrete adapters or local configuration I/O.

The `adapters.mcp` package assembles the server in `server.py` (instructions, prompts,
completion and transient resources) from feature modules in `adapters/mcp/tools/`:
watches, changes, attachments, status, discovery, catalog and messages. Each module
registers its tools on the server; the registration sequence fixes the advertised
tool order. Tool modules share parameter types and annotations from `params.py` and
error mapping from `errors.py`. They do not import each other or the server assembly.

Domain models deliberately use Pydantic. The MCP adapter owns separate compact wire
models because its public contract shares chat summaries, encodes canonical references
and omits unknown optional metadata. Scope projection limits ID previews to 20 each
and adds exact counts/truncation flags; internal authorization and cursor state retain
every ID. The `adapters.wire` package groups these DTOs by the same features as the
tool modules, with shared chats, issues, scopes and saved files in `common.py` and
the projection in `encoding.py`; a contract test keeps their fields aligned with the
domain models except for these documented projections. Other boundaries reuse domain
models where a second representation would add no useful behavior. Internal Python imports and
constructors are not a supported compatibility surface.

## Application responsibilities

The `Service` facade delegates public operations and exposes `close()`. Its constructor
assembles use cases from injected capability ports; bootstrap chooses the backend,
policy provider, clock, cursor store and download store. Individual use cases depend
on the chat, discovery, message, forum, attachment or runtime operations they consume.
The aggregate backend protocol is confined to assembly and local command wiring.

- `AccessFactory` obtains current policy and authorization before constructing a
  request-local `AccessContext`. Explicit reads bypass catalog enumeration when folder
  permissions are not involved. Public grants never extend the stored allowlist or
  authorize a linked discussion.
- `CatalogService` and `DiscoveryService` handle source selection and feature gates.
  Disabled discovery returns before any backend request.
- `QueryService` creates typed history, search, pins, topic and thread requests.
  `QueryPreparation` resolves sources, checks access/topic state and selects per-chat
  or unrestricted global search once for the cursor's lifetime.
- `ProgressivePager` visits permitted sources round-robin for searches and
  multi-source history, stops fetching once the page is full, and sorts within that
  page. A cursor may carry a cumulative `TraversalBudget` (sources and messages);
  `QueryPreparation` rejects an oversized selection before any history read. `MessagePager` supplies the opt-in
  global ordering guarantee by priming all streams before merging by date, numeric
  chat ID and message ID. They share batch validation, retry and response assembly.
  `GlobalSearchPager` owns the unrestricted native search offset and preserves Telegram's
  ordering within equal timestamps. All enforce budgets and retain fixed source/date scope.
- `TopicService` and `TopicPager` retain the topic-specific offset/order state machine.
  They share the small `RetryState` helper with message pagination, not its algorithm.
- `MessageReader` handles batches, context, albums and discussion preparation. Album
  windows retain their separate raw-row budget and unknown-completeness contract.
- `AttachmentService` uses `AttachmentPort` and `DownloadStorePort` for selected-file
  downloads and resource reads, with fresh access checks, a bounded deadline,
  serialized downloads and FloodWait cooldowns.
- `ExtractionService` obtains verified bytes through the attachment use case, parses
  them through an optional worker port and stores immutable, bounded text snapshots.
  It revalidates the current source before every delivery. Parser adapters live in a
  disposable process; cancellation kills and reaps it, without holding the attachment
  lock while parsing. The separate extraction cache reuses `TemporaryFiles` and its
  quota, expiry, private-file and integrity rules.

`ConfiguredPolicyProvider` reads current in-memory settings on every call, so policy
changes between calls revoke access before saved output is returned. It does not reload
configuration files automatically. Credentials and session paths never enter policy
snapshots; execution budgets are injected separately.

## Telegram readers and lifecycle

`TelegramBackend` composes chat, discovery, message, forum, attachment and incremental gateways.
They share one `TelegramRuntime` and chat/entity cache. Runtime owns the client,
lifecycle lock, session lease, authorization probes, timeouts and sanitized errors.
It restores disconnected clients under that lock while retaining the lease. Authorization
generation changes invalidate catalog state; ordinary network errors and reconnection
do not. Injected clients and factories support
raw-response and lifecycle tests without a Telegram account.

`MessageGateway` resolves references, reads batches and ordinary history, and composes
three specialized readers with the same runtime and `ChatGateway`:

| Reader | Responsibility |
| --- | --- |
| `MessageSearch` | Per-chat/global search requests, date anchors, native filters and continuation positions |
| `TopicHistoryReader` | Topic-specific raw history and membership checks |
| `AlbumWindowReader` | Bounded raw history windows around an album anchor |

Request construction and response parsing are separated within search. Common factual
filtering lives in `message_filters`; normalization and optional metadata mapping remain
pure helpers. Raw responses use response-local entities without Telethon's private
message initializer. Link previews remain separate from actual media attachments.
Ordinary history, topic history and album windows retain distinct exhaustion rules.

`ChatGateway` delegates catalog ownership to `DialogCatalog`. `DialogScanner` handles
bounded raw pagination independently of folder definitions. Each dependent call checks
authorization and fresh folder rules; one worker owns the resumable dialog scan and
atomically publishes complete snapshots. Defaults allow five minutes of reuse plus
five minutes while refreshing. Cold/hard-expired reads wait at most five seconds for
the worker; partial state cannot grant access. Transient failures retain progress and
stop the worker until a later call after the retry deadline. Shutdown cancels and awaits
it. Self/notification lookups needed by new folder rules are separately serialized.
Fresh public-channel metadata remains request-local. Direct reads skip the catalog
when folder policy is absent. See [freshness limits](configuration.md#catalog-freshness-and-recovery-04).

## Transports and local resources

Standalone STDIO owns its application/runtime. The foreground local HTTP process owns
one facade, runtime, policy, cursor store, checkpoint store and temporary file stores
shared by its clients.
All MCP transports share the same admission gate: four active calls, 16 FIFO waiters,
and a 120-second queue timeout. Tool and resource operations use this gate; runtime,
and download operations retain their narrower locks; watches lock only the same name.
Independent incremental reads can overlap. Cancellation releases
slots without running queued work. Disconnecting a client does not close the application;
server shutdown closes the gate before the facade and cancels outstanding calls.

`connect` proxies STDIO to the authenticated loopback HTTP endpoint, including resource
requests. It never opens a Telegram session, starts a server or falls back to standalone
mode. Accepted clients share one local user's permissions and cursor namespace.
See [configuration](configuration.md) and [client setup](clients.md).

`SessionLease` provides an exclusive nonblocking lock for reads and interactive login
on Windows, macOS and Linux. Login owns its prompting and authentication flow; read-only
startup never initiates login or creates a missing session database. Configuration is
saved atomically; private files use POSIX modes or Windows ACLs as appropriate.

The download store starts only after an authorized request. It owns private generated
files, quota reservations, expiry, cleanup and a process lease. The Telegram attachment
gateway refreshes protection/media metadata and allows one identity-checked recovery
of a stale file reference. Resource reads reauthorize the source and verify current
media identity and stored bytes. The MCP adapter exposes opaque resource URIs without
a general HTTP file endpoint.

## Durable reads and exports

`ChangesService` observes new IDs and rechecks a rolling set of 100 known IDs.
`HistoryExportService` traverses a frozen single-chat date/ID range in bounded parts.
They share `advance_changes` with `WatchService` for per-source progress and use
`IncrementalContext` for source authorization, session-wide persistent M4 FloodWait
deadlines, active-request draining and store ownership. `ReadBudget`
counts head/recheck/history operations and bounds the page work. Access failures never
release saved output or consume the caller's checkpoint.

`IncrementalPort` supplies current observations, range reads and export-protection checks.
Its Telegram gateway shares the existing runtime and entity cache, uses public history
and message-batch APIs, and includes media identity in content fingerprints. Pure domain
rules exclude volatile counters/reactions/poll votes. `ExportPart` writes complete UTF-8
records with reserved summary space; clipping applies only to inline change responses.

`CheckpointStorePort` holds versioned, immutable position snapshots and cooldown deadlines.
The SQLite adapter stores no message bodies and never unpickles data. Snapshots are bound
to the canonical session path and a digest of its authorization, with atomic writes,
30-day expiry and protected current heads. Only superseded snapshots can be evicted
under capacity pressure; branches keep independent heads and unchanged heads reuse
their token. A staged receipt retires its parent only after file publication succeeds.
Database version 2 migrates legacy tokens in place. A partial pass persists
its phase and offset without saving message buffers; continuation rereads current data.

`WatchService` owns named on-demand consumers. A separate private `WatchStorePort`
persists positions and one immutable pending batch including message text. Explicit
acknowledgement installs the candidate position; replay rechecks access and source
protection. There is no background poller, TTL or eviction. Per-name locks protect the
read/ack sequence; independent consumers share budgets and the durable M4 cooldown.
Folder membership refreshes between sweeps. New members start from the previous
completed sweep boundary; returning members keep their saved position.
With `save=true`, the pending batch records a reserved archive key before any file is
written; `ArchivePort` publishes the deterministic batch JSONL under that key, and only
then is the receipt persisted. A republished key is verified and reused, so crashes and
retries never duplicate copies; both acknowledgement paths require a verified receipt.

`WatchSweep` performs one bounded pass over the remaining sources into a private
candidate position. `WatchDelivery` owns the pending batch afterwards. The batch's
archive status is derived from its stored fields, not stored separately:

| Archive status | Stored form | Acknowledgement |
| --- | --- | --- |
| `not_requested` | No archive record | Allowed |
| `reserved` | Key without receipt | `WATCH_ARCHIVE_PENDING`; repeat with `save=true` |
| `saved` | Key and receipt | Allowed while the copy verifies, otherwise `ARCHIVE_COPY_INVALID` |

`ExportStorePort` owns a separate bounded cache of generated JSONL/Markdown files.
Like the download store, it builds on `TemporaryFiles`: a per-session private cache
directory with an exclusive process lease, expiring entries, removal of leftover
managed files and digest-verified reads. Each store keeps its own names, limits, write
path and error codes; exports add atomic publication and hashes. Resource reads reauthorize the source before reading the stored
snapshot. Existing attachment resource checks retain their stronger media-identity
verification. The MCP resource dispatcher returns text exports and binary attachments
over all transports without adding a file-serving HTTP route.

`ArchivePort` creates selected permanent copies beneath the configured private root,
with generated paths, size/checksum checks and atomic publication. It never accepts
a destination from a tool or automatically deletes successful copies. Failed publication
rolls back only files and unpublished receipts created by that call.

## Prompts and completion

The MCP adapter's `PromptCatalog` holds static `watch_digest` and `period_digest`
templates and parses their string arguments. Pure `domain.periods` rules resolve
relative periods, calendar dates in IANA zones and aware timestamps against a clock
captured once per rendering. Rendering performs no post reads or state changes; only
`folder_name` uses the narrow `CatalogService.find_folder` operation, which reads fresh
folder definitions without a dialog scan. Watch-name completion uses
`WatchService.name_suggestions`, which needs a locally known authorization binding and
reads names only from an existing store. `connect` forwards prompts and completion when
the shared server advertises them.

## Invariants and validation

- Access and topic checks precede saved-buffer output; failed checks leave the original
  cursor reusable. Cursors are deep-copy snapshots with bounded size, expiry and eviction.
  Their canonical source IDs, date ceilings and selected search engine stay fixed.
- Strict global ordering needs a head from every unfinished per-chat stream. Page
  ordering can return other sources while one is unavailable and makes no cross-page
  ordering promise. The requested ordering is part of the cursor fingerprint.
  Temporary errors retain position, clear after recovery and prevent early or same-call
  retries. An injected clock controls deadlines, cooldowns and cursor expiry.
- RPC budgets include date-anchor requests. Album scans do not refill pages, and topic
  scans do not infer exhaustion from sparse data. Text clipping and cursor advancement
  precede MCP projection; projection does not drop messages to fit a budget.
- MCP emits the same compact JSON in text and structured content. `mcp_schema_mode`
  controls advertisement of optional output schemas, not typed validation or payloads.
  Ordinary cursors and resources are process-local; server restarts invalidate them.
  Checkpoints and named subscriptions have the distinct retention rules above.

[Architecture tests](../tests/contracts/test_architecture.py) check imports and cycles.
[Contract tests](../tests/contracts/test_mcp.py) compare both MCP modes with the versioned
[fixture](../tests/fixtures/mcp_contract.json). Layer-specific tests preserve pagination,
retry, mapping and no-extra-RPC behavior. Integration tests exercise real STDIO, HTTP
and bridge delivery, including full-size resources. See the [test layout](../CONTRIBUTING.md#test-layout).

Run `make check` and require GitHub CI before squash merge. Automated checks are offline;
[bounded live validation](live-validation.md) uses separately selected sources.
Add cohesive use cases and adapters where needed; keep distinct pagination algorithms
explicit rather than building a universal flag-driven query method.
