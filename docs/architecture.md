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
`tests/fixtures/mcp_contract.json` records the pre-refactor input/output schemas and
annotations for every tool; both MCP modes are checked against that immutable baseline.

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
