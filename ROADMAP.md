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
| M5 | Complete, unreleased | Bounded multi-source history, archived watch batches and usable monitoring prompts |

The [0.4 changelog](CHANGELOG.md#040---2026-10-26), [migration guide](docs/migration-0.4.md)
and [validation record](docs/validation-0.4.md) describe the complete release scope.
A merged PR is not proof of publication: verify the version on
[PyPI](https://pypi.org/project/telegram-context-mcp/0.4.0/) and
[GitHub Releases](https://github.com/egor-fedorov/telegram-mcp/releases/tag/v0.4.0).
The [0.3 validation record](docs/validation-0.3.md) retains earlier measurements.

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

## 0.5: monitoring workflows (complete, unreleased)

[M5](docs/specs/m5-monitoring-workflows.md) is implemented and validated, but not released.

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
records environments, evidence and coverage limits. Release preparation targets 0.5.0;
package and Registry publication remain pending verification under the
[release procedure](docs/releasing.md).

## Distribution follow-ups

These can ship independently and do not gate M5:

1. [Publish the PyPI package to the MCP Registry (#39)](https://github.com/egor-fedorov/telegram-mcp/issues/39).
   Metadata, ownership marker, package-named CLI alias, offline checks and an
   independently retryable OIDC publication workflow are implemented. Publication
   remains pending the first package release containing these changes; 0.4.0 lacks
   the required PyPI ownership marker. Clean-install CI exercises the declared STDIO
   entry point without a Telegram account. See the [release procedure](docs/releasing.md#mcp-registry-publication).
2. [Support opt-in shared HTTP server autostart (#40)](https://github.com/egor-fedorov/telegram-mcp/issues/40), starting with a tested user-level systemd service.
3. [Ship a validated MCPB bundle (#41)](https://github.com/egor-fedorov/telegram-mcp/issues/41), after the setup and session-ownership model is settled.

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
remain later decisions. Prioritize concrete usage and bug reports from 0.4.

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
