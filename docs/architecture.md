# Architecture

The server separates domain contracts, application policy and concrete adapters.
This document describes the current source tree, including the unreleased attachment
capability. The [roadmap](../ROADMAP.md) and [changelog](../CHANGELOG.md) track delivery;
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

Domain models deliberately use Pydantic. The MCP adapter owns separate compact wire
models because its public contract shares chat summaries, encodes canonical references
and omits unknown optional metadata. Other boundaries reuse domain models where a
second representation would add no useful behavior. Internal Python imports and
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
- `MessagePager` fairly primes permitted streams and merges by date, numeric chat ID
  and message ID. `GlobalSearchPager` owns the native compound offset and preserves
  Telegram's ordering within equal timestamps. Both enforce budgets and retry state.
- `TopicService` and `TopicPager` retain the topic-specific offset/order state machine.
  They share the small `RetryState` helper with message pagination, not its algorithm.
- `MessageReader` handles batches, context, albums and discussion preparation. Album
  windows retain their separate raw-row budget and unknown-completeness contract.
- `AttachmentService` uses `AttachmentPort` and `DownloadStorePort` for selected-file
  downloads and resource reads, with fresh access checks, a bounded deadline,
  serialized downloads and FloodWait cooldowns.

`ConfiguredPolicyProvider` reads current in-memory settings on every call, so policy
changes between calls revoke access before saved output is returned. It does not reload
configuration files automatically. Credentials and session paths never enter policy
snapshots; execution budgets are injected separately.

## Telegram readers and lifecycle

`TelegramBackend` composes chat, discovery, message, forum, attachment and incremental gateways.
They share one `TelegramRuntime` and chat/entity cache. Runtime owns the client,
lifecycle lock, session lease, authorization probes, timeouts and sanitized errors.
It restores disconnected clients under that lock while retaining the lease; generation
changes invalidate cached access metadata. Injected clients and factories support
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

`ChatGateway` owns a resumable catalog builder and publishes only completed snapshots,
reused for at most 30 seconds. Expired or partial catalogs never grant access. Fresh
public-channel metadata remains request-local, and authorization is checked before
saved-buffer output. Direct-source reads do not depend on a cold catalog when folder
policy is absent.

## Transports and local resources

Standalone STDIO owns its application/runtime. The foreground local HTTP process owns
one facade, runtime, policy, cursor store, checkpoint store and temporary file stores
shared by its clients.
The tool gate coordinates calls to shared state; disconnecting a client does not close
the application. Server shutdown closes it through the facade.

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
a general HTTP file endpoint or persistent message database.

## Durable reads and exports

`ChangesService` observes new IDs and rechecks a rolling set of 100 known IDs.
`HistoryExportService` traverses a frozen single-chat date/ID range in bounded parts.
They use `IncrementalContext` for source authorization, session-wide persistent M4
FloodWait deadlines, serialization and ownership of the two stores. `ReadBudget`
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
30-day expiry and oldest-first eviction under the state budget. A partial pass persists
its phase and offset without saving message buffers; continuation rereads current data.

`ExportStorePort` owns a separate bounded cache of generated JSONL/Markdown files.
It reuses private-path protection, has a process lease, atomic publication, hashes,
expiry and cleanup. Resource reads reauthorize the source before reading the stored
snapshot. Existing attachment resource checks retain their stronger media-identity
verification. The MCP resource dispatcher returns text exports and binary attachments
over all transports without adding a file-serving HTTP route.

## Invariants and validation

- Access and topic checks precede saved-buffer output; failed checks leave the original
  cursor reusable. Cursors are deep-copy snapshots with bounded size, expiry and eviction.
  Their canonical source IDs, date ceilings and selected search engine stay fixed.
- Every unfinished per-chat stream needs a head before merged output can be chosen.
  Temporary errors retain position, clear after recovery and prevent early or same-call
  retries. An injected clock controls deadlines, cooldowns and cursor expiry.
- RPC budgets include date-anchor requests. Album scans do not refill pages, and topic
  scans do not infer exhaustion from sparse data. Text clipping and cursor advancement
  precede MCP projection; projection does not drop messages to fit a budget.
- MCP emits the same compact JSON in text and structured content with self-contained
  schemas. Ordinary cursors and resources are process-local; server restarts invalidate
  them. M4 checkpoints are distinct durable tokens, subject to retention and eviction.

[Architecture tests](../tests/contracts/test_architecture.py) check imports and cycles.
[Contract tests](../tests/contracts/test_mcp.py) compare both MCP modes with the versioned
[fixture](../tests/fixtures/mcp_contract.json). Layer-specific tests preserve pagination,
retry, mapping and no-extra-RPC behavior. Integration tests exercise real STDIO, HTTP
and bridge delivery, including full-size resources. See the [test layout](../CONTRIBUTING.md#test-layout).

Run `make check` and require GitHub CI before squash merge. Automated checks are offline;
[bounded live validation](live-validation.md) uses separately selected sources.
Add cohesive use cases and adapters where needed; keep distinct pagination algorithms
explicit rather than building a universal flag-driven query method.
