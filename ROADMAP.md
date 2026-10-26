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

## Next decisions

Review 0.4 usage and concrete bug reports before selecting another milestone. No M5
scope is committed. Broader change journals and multi-source exports remain later
decisions, alongside saved searches, local text/semantic indexes, OCR or transcription,
scheduled monitoring, optional writes or administration, and isolated account profiles.

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
