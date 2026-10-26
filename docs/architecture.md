# Architecture decision: explicit layers and dependency injection

Status: accepted for the R1 refactor, before attachment downloads (M3).

## Context

The original application already separated MCP, service policy, and a backend protocol.
After M2, one service coordinated access, source selection, several pagination algorithms,
retry checkpoints, and message assembly. The Telegram backend also owned connection
lifecycle, locking, entity resolution, mapping, discovery, and all message queries.
Adding another capability required understanding unrelated responsibilities.

The refactor preserves all 14 MCP tools, their schemas, error codes and payloads, CLI
commands, local configuration, STDIO, and locked dependencies. Internal Python imports
and constructors are not a supported compatibility surface. No new Telegram feature,
database, DI framework, or duplicate domain/transport model hierarchy is introduced.

## Dependency rules

| Layer | Responsibility | Allowed internal dependencies |
| --- | --- | --- |
| `domain` | Pydantic contracts, errors, read-policy snapshots, folder matching and text clipping | Domain only |
| `application` | Use cases, access decisions, typed requests, pagers and dependency ports | Application and domain |
| `adapters` | MCP/CLI, Telethon, configuration, cursor storage, session leases and real time | Application, domain and other adapters |
| `bootstrap` | Select concrete adapters and assemble the application | All layers |

Outbound adapters cannot import the MCP or CLI adapters. The stable `telegram_mcp.cli`
entry point delegates to bootstrap. Neither domain nor application imports Telethon,
the MCP SDK, concrete adapters, or local configuration I/O. Pydantic is deliberately
retained in the shared domain contracts; separate copies of each model would add
conversion work without a current consumer requiring it.

`tests/test_architecture.py` checks absolute and relative imports and dependency cycles.
`tests/fixtures/mcp_contract.json` records versioned input/output schemas and annotations
for every tool; both MCP modes are checked against the current contract. R1 preserved
the original baseline; 0.3 deliberately replaces it with the documented compact contract.

## Application responsibilities

The `Service` facade delegates public operations and exposes `close()`. Its constructor
assembles application use cases from injected ports; bootstrap chooses the concrete
backend, policy provider, clock and cursor store. Consumers depend on capability ports
for chats, discovery, messages, forums and runtime, rather than the combined backend.
The aggregate backend protocol is confined to assembly and local command wiring.

- `AccessFactory` loads the current catalog and a fresh credential-free policy snapshot.
  `AccessContext` distinguishes explicit public sources, ordinary linked sources and
  forums. Grants live only within that request and never extend the stored allowlist.
- `CatalogService` and `DiscoveryService` handle their respective selection and feature
  gates. Disabled discovery still returns before any backend request.
- `QueryService` creates typed history, search, pins, topic and thread requests.
  `QueryPreparation` resolves sources, checks access/topic state and loads checkpoints.
  `MessagePager` advances only those authorized streams, respecting the RPC budget,
  deadline, cooldown and global order.
- `TopicService` and `TopicPager` retain the topic-specific offset/order state machine.
  They share the small `RetryState` helper with message pagination, not its algorithm.
- `MessageReader` handles batches, context, albums and discussion preparation. Album
  windows keep their separate raw-row budget and unknown-completeness contract.

`ConfiguredPolicyProvider` reads current in-memory settings on every call. Changes
between calls therefore revoke access before saved buffers can be returned. It does
not reread the configuration file automatically. Credentials and session paths never
enter application policy snapshots. Execution budgets are injected separately.

## Telegram and local resources

`TelegramBackend` composes chat, discovery, message and forum gateways. They share one
`TelegramRuntime` and one chat/entity cache. Runtime owns the client, connection mutex,
session lease, fresh authorization probes, timeout and sanitized error translation.
Client and client-factory injection lets raw-response tests avoid a real account while
lifecycle tests still exercise acquisition, cancellation and cleanup.

`SessionLease` provides the same exclusive nonblocking lock for reads and interactive
login. Login retains its own prompting/authentication flow and restrictive permissions;
read-only startup never initiates login or creates a missing session database.
Configuration persistence remains atomic. MCP closes the application through its
facade rather than reaching into the backend.

Pure mapping functions preserve Telegram content, formatting and optional fields.
Ordinary history, topic history and album windows keep distinct exhaustion rules.
Only their common data structures and factual mapping helpers are shared.

## Invariants and tradeoffs

- Access and topic checks precede saved-buffer output. Failed checks leave the original
  cursor reusable; public-channel permission never authorizes a linked discussion.
- Cursor snapshots are deep copies, with the existing expiry, eviction and size limits.
  Upper dates, canonical IDs and fingerprint inputs remain unchanged. Cursors remain
  process-local and expire on restart; there is no persisted-data migration.
- Every unfinished stream needs a head before merged output is chosen. Temporary errors
  preserve position, clear after recovery, and prevent early or same-call retries.
- RPC requests, timeouts, retries, limits and source content stay unchanged. In particular,
  album scans do not refill pages and topic scans do not infer exhaustion from sparse data.
- The injected clock covers pagination deadlines, cooldowns and cursor expiry. A fake
  clock replaces module-level time monkeypatches in tests.

The extra modules make dependencies explicit at the cost of some assembly and delegation
code. There is no class-per-tool rule, inheritance hierarchy, generic repository, event
bus, or line-count threshold. Future capabilities should add a cohesive use case and
the required port/adapter behavior without growing a universal flag-based query method.

## Validation and delivery

R1 is delivered in one PR with a contract-baseline commit followed by the implementation
and architecture documentation. Existing behavioral checks remain, including native TL
fixtures, no-extra-RPC assertions, policy revocation, pagination recovery, Unicode text,
both MCP modes and a real STDIO process. New checks cover the schema baseline, layer
boundaries, shared runtime, concurrent connection, cancellation and login cleanup.

Run `make check` and require GitHub CI before squash merge. All automated validation is
offline; live Telegram checks remain a separate task using user-selected sources.

## R2: Shared local transport

R2 retains these layer boundaries and adds transport composition around the same
application facade. Standalone STDIO still owns its application/runtime. The optional
foreground HTTP process owns one facade, Telegram runtime, read policy, and cursor
store for all connected local clients. Tool calls coordinate access to that shared
state. Client disconnects do not close the application; server shutdown does.

The `connect` command is a STDIO-to-HTTP adapter. It reads the configured loopback
endpoint and token file without opening the Telegram session. It does not start a
server or fall back to standalone operation. Bearer authentication protects the
local endpoint; all accepted clients share the same user trust boundary, rather
than receiving separate account permissions or cursor namespaces.

Session leases use portable locking on Windows, macOS, and Linux. Private-file
handling uses POSIX modes where applicable and the user's filesystem ACLs on
Windows. These transport and platform changes do not introduce new Telegram tools,
attachment downloads, persistent cursors, or multiple account profiles. See
[configuration](configuration.md) and [client setup](clients.md) for operational details.

## R3: Reliable retrieval and compact transport contracts

R3 keeps the layers and 14 tools while separating public MCP DTOs from domain models.
The MCP adapter projects message references and shared chat summaries, omits unknown
optional metadata, and emits a single minified JSON payload in both text and structured
content. Typed output schemas remain self-contained. Domain text clipping and cursor
advancement happen before projection; the transport never drops messages to fit a budget.

Explicit source reads independent of folders bypass catalog enumeration. Required catalogs
use one private resumable builder and atomically published snapshots, with a maximum
30-second reuse TTL. Fresh authorization and current local policy precede buffered output;
fresh public metadata remains request-local. Expired or partial catalogs never grant access.
The runtime restores a disconnected client under its lifecycle lock while retaining the
session lease, and exposes generation changes to invalidate cached access metadata.

`QueryPreparation` chooses per-chat or unrestricted global search once. `MessagePager`
primes sources fairly and merges by date, numeric chat ID, and message ID. `GlobalSearchPager`
owns a compound offset and preserves native ordering across pages. Both keep immutable
cursor checkpoints and temporary-failure cooldowns. Raw search calls expose explicit
page budgets, including the date-anchor step for filter-only per-chat search.

Raw payload normalization uses response-local entity maps rather than Telethon's private
message initializer. Link previews remain separate from actual media attachments.
Autonomous tests cover these contracts; a bounded user-selected live probe is a separate
release gate and does not run in CI.

## M3: Selected attachment downloads

`AttachmentService` owns explicit access checks, a bounded operation deadline,
serialization and FloodWait cooldown through `AttachmentPort` and `DownloadStorePort`.
The Telegram gateway refreshes current protection/media metadata and uses public
Telethon downloads with one identity-checked file-reference recovery. The independent
store adapter owns quota, private generated files, expiry, cleanup and a process lease.
It starts only after an authorized request; ordinary retrieval creates no download files.

MCP adds one tool and opaque temporary resources, using public SDK resource hooks.
The bridge forwards resource requests. Resource reads reauthorize the source and verify
media identity and stored bytes. The existing 14 retrieval tools keep their contracts;
no general HTTP file endpoint or persistent storage layer is introduced.
