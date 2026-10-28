# Telegram MCP roadmap

The project provides general-purpose Telegram access for agents: discover permitted
sources, retrieve original messages and files, and track changes. The calling agent
performs content analysis. Reading, downloading, writing and administration remain
separate capabilities with separate permissions.

This file tracks priorities and delivery status. Historical implementation contracts
are preserved in [docs/specs](docs/specs/README.md); current behavior is described in
[usage](docs/usage.md) and [configuration](docs/configuration.md). Future milestones
need a concrete contract before implementation; no delivery dates are assigned.

## Milestones

| ID | Status | Outcome |
| --- | --- | --- |
| M0 | Done | Reliable references, explicit public reads, batches, recovery and fresh authorization |
| M1 | Done | Opt-in public-channel search and recommendations without granting post access |
| M2 | Done | Topics, pins, counters, reactions, polls and bounded album retrieval |
| R1 | Done | Domain/application/adapter layers with explicit dependencies |
| R2 | Done (0.2) | Installable package, Windows support, shared local HTTP, client guides and release automation |
| R3 | Done (0.3) | Recoverable connections, bounded catalogs/search and compact typed responses |
| M3 | Complete for 0.4 | Selected attachment downloads with limits and resource delivery |
| M4 | Complete for 0.4 | Durable checkpoints, recent revisions and source-linked JSONL/Markdown exports |
| R4 | Complete for 0.4 | Bounded scope previews, early restricted search, reusable catalogs and concurrent calls |
| R5 | Complete for 0.4 | Named subscriptions, protected positions, permanent selected files and compact discovery |
| M5 | Complete for 0.5 | Bounded multi-source history, archived watch batches and usable monitoring prompts |
| R6 | Done | Feature-organized MCP and wire modules, bounded complexity, shared storage and faster tests |

The [0.5 changelog](CHANGELOG.md#050---2026-10-27) and
[validation record](docs/validation-0.5.md) describe the current release scope.
Publication of 0.5.0 was verified on
[PyPI](https://pypi.org/project/telegram-context-mcp/0.5.0/),
[GitHub Releases](https://github.com/egor-fedorov/telegram-mcp/releases/tag/v0.5.0), and the
[MCP Registry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.egor-fedorov%2Ftelegram-mcp/versions/0.5.0).
The [0.4 migration guide](docs/migration-0.4.md) applies to upgrades from 0.3;
[0.4](docs/validation-0.4.md) and [0.3](docs/validation-0.3.md) retain earlier validation.

## 0.5.1: security patch (prepared)

Telegram's service-notification chat `777000` delivers login codes. In 0.5.0,
all-chat access or a selected folder containing it made it readable. 0.5.1 excludes
it from all-chat and folder access, denies explicit, monitoring, export and download
requests with `ACCESS_DENIED`, and rejects allowlisting it. The fix was contributed in
[#34](https://github.com/egor-fedorov/telegram-mcp/pull/34) and ported onto R6 in
[#59](https://github.com/egor-fedorov/telegram-mcp/pull/59). The patch also ships R6;
tools, schemas and persisted state are unchanged. The
[0.5.1 validation record](docs/validation-0.5.1.md) tracks offline and selected-source
checks; publication remains pending under the [release procedure](docs/releasing.md).
The follow-up in [#62](https://github.com/egor-fedorov/telegram-mcp/pull/62)
checks canonical ASCII chat IDs before any grant,
closing a Unicode-digit alias bypass. Configuration rejects noncanonical allowlist
IDs. Regression tests exercise message and context reads through both MCP modes and
the real Telegram adapter with synthetic responses, asserting no entity or message
read occurs for a rejected ID. No live service-notification content is used.

## R6: structural consolidation (done)

[R6](docs/specs/r6-structural-consolidation.md) reorganized the code after 0.5.0 without
changing public behavior: the MCP contract, persisted formats and existing test
assertions stayed fixed. The feature freeze has ended. Offline `make check` passes with
Ruff (`C901` at 15, no exceptions), ty and 1431 tests (two platform-specific skips) in
about 65 seconds on four cores; `make test-fast` takes about 40 seconds. MCP and wire
modules are at most 235 lines. No live Telegram validation was needed or performed.

Steps:

- Guardrails: Ruff `C901` at 15 with explicit markers on existing offenders, parallel
  pytest-xdist runs (`make check` from about 4.5 to 1.2 minutes on four cores), a `slow`
  marker with `make test-fast`, and fixtures pinning tool order and descriptions, wire/domain
  field parity and 0.5 persisted state.
- MCP tools by feature: `adapters.mcp` is a package whose server assembly registers seven
  feature modules in the advertised order; tool modules cannot import each other.
- Wire DTOs by feature: `adapters.wire` is a package with the same feature modules, shared
  `common` types and a split projection in `encoding`; schemas are unchanged.
- Query preparation: `QueryPreparation.prepare` runs named steps (source resolution, cursor
  resumption, topic check, first-page selection) and `history_spec` validates `get_history`
  arguments as a pure, separately tested function.
- Incremental pipeline: changes and history export share one forward reader and one
  publication transaction (export, permanent copy, checkpoint confirmation with rollback);
  protected-record omissions come from one domain helper.
- Watches: `WatchSweep` runs one bounded pass, `WatchDelivery` owns replay, archiving and
  acknowledgement of the pending batch, and its archive status is derived without changing
  stored state.
- Local storage: download and export stores share `TemporaryFiles` (lease, expiry, leftover
  cleanup, verified reads); checkpoint persistence is split into schema migrations and
  named write steps. Error codes and stored formats are unchanged.
- Telegram readers: topic history, album windows and global search pages are split into
  request, position and message steps with one shared page-issue collector. No function
  needs a `C901` exemption any more.

## 0.4 completion

Monitoring now batches selected peer metadata, supports acknowledgement with the next
poll, and bounds explicit watch creation. One unused baseline per source remains
protected; consumed chains retain independent protection. Conservative migration and
local preview/cleanup commands recover already full stores. Permanent copies include
source metadata and publish through bounded worker threads with cancellation cleanup.

Selected-source live checks exercised photo and video/document downloads, checkpoint
reopen/continuation, JSONL export integrity and owned-client reconnect. A manual test
through real STDIO MCP processes verified new-message delivery, exact replay after
restart, edits without a new post, duplicate acknowledgements and an empty final poll.
The validation record distinguishes agent-run checks from the maintainer-provided M4
report and records the limits of live coverage. Source content and credentials are not
part of the public record; disposable test state and files were removed.

Offline coverage includes 4200-poll and fresh-baseline retention regressions, large
catalog/scope fixtures, edit and retry behavior, access revocation, archive cancellation,
all transports and both schema modes. Tests are organized by layer, with focused
Telegram readers and shared explicit fixtures. Dependency and development-tool updates
receive manual review; CI checks supported Python/platform combinations and fresh
wheel/source installations.

## 0.5: monitoring workflows (released)

[M5](docs/specs/m5-monitoring-workflows.md) was released in 0.5.0 on 2026-10-27.

- `get_history` reads selected `chat_ids`/`folder_ids` over a fixed period in one
  traversal, with early `ordering="page"` results, `max_sources`/`max_messages` budgets
  (defaults 20/100), `SOURCE_LIMIT_EXCEEDED` before any read and resumable exhaustion.
- `get_watch_changes(save=true)` publishes each pending batch as a permanent JSONL copy
  before either acknowledgement path can commit it; retries, restarts and crashes reuse
  the same copy, and missing copies are rebuilt from the pending batch.
- `watch_digest` and `period_digest` prompts, with relative periods, calendar dates in a
  prompt or configured IANA `timezone`, exact folder titles and local watch-name
  completion, forwarded through STDIO, HTTP and `connect`. Templates treat Telegram
  content as data and bound each workflow. The vacancy digest is a documented preset.

Offline `make check` passes: Ruff, ty and 1403 tests (two platform-specific skips).
Selected-source live checks on 2026-10-27 verified bounded multi-source history,
prompt/completion discovery, archive replay across server restarts and recovery after
acknowledgement. The maintainer confirmed a bounded `watch_digest` workflow and
watch-name completion in Claude Code 2.1.310; archive integrity and persisted
acknowledgement were checked independently. The [0.5 validation record](docs/validation-0.5.md)
records environments, evidence and coverage limits. The release workflow passed its
full matrix, published both package artifacts and registered the matching MCP metadata.
Fresh PyPI installation and both CLI names were verified under the
[release procedure](docs/releasing.md).

## Distribution

The [MCP Registry integration (#39)](https://github.com/egor-fedorov/telegram-mcp/issues/39)
is complete with 0.5.0: the active entry points to the verified PyPI artifacts and
the package-named STDIO executable. The OIDC publication workflow remains independently
retryable; see the [release procedure](docs/releasing.md#mcp-registry-publication).

Remaining distribution work can ship independently:

1. [Support opt-in shared HTTP server autostart (#40)](https://github.com/egor-fedorov/telegram-mcp/issues/40), starting with a tested user-level systemd service.
2. [Ship a validated MCPB bundle (#41)](https://github.com/egor-fedorov/telegram-mcp/issues/41), after the setup and session-ownership model is settled.

## Later decisions

A candidate for 0.6 is optional text extraction from text-based PDF and DOCX
attachments, with bounded work and document provenance. OCR and voice transcription
need separate decisions about engines, dependencies and local versus external processing.

Optional Telegram writes remain a later candidate: drafts first, then separately
authorized sending, with editing/deletion considered afterward. The
[sending proposal in PR #32](https://github.com/egor-fedorov/telegram-mcp/pull/32) is a
discussion input, not the M5 contract. Confirmation defaults, client/bridge support,
existing draft preservation and recovery after an uncertain send need decisions
before implementation. No 0.7 scope is committed.

Broader change journals, multi-source exports, saved searches, local text/semantic
indexes, scheduled monitoring, administration and isolated account profiles also
remain later decisions. Prioritize concrete usage and bug reports from 0.5.

Preserve original content and links; summaries must not replace them. Basic retrieval
should require neither a built-in LLM nor a persistent message database. Features
should address demonstrated needs and retain explicit permissions and bounded work.

## Working process

Start a Conventional Branch from current `main` for one bounded change. Add appropriate
offline checks and documentation, open one focused PR, wait for the required GitHub
`checks` job and squash-merge. Record completed work here and public changes in
[CHANGELOG.md](CHANGELOG.md). Archive detailed completed contracts rather than growing
this status document. See [CONTRIBUTING.md](CONTRIBUTING.md) and the separate
[release procedure](docs/releasing.md).
