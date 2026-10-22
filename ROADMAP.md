# Telegram MCP roadmap

The project aims to provide general-purpose Telegram access for agents: discover sources,
retrieve original messages and files, and later track changes. The calling agent
performs content analysis. Reading, downloading, writing, and administration remain
separate capabilities with separate permissions.

This file tracks priorities and delivery status. Completed implementation contracts
are preserved in [docs/specs](docs/specs/README.md); current behavior is described in
[usage](docs/usage.md) and [configuration](docs/configuration.md). Planned milestones
need a detailed contract before implementation; no delivery dates are assigned.

## Milestones

| ID | Status | Outcome |
| --- | --- | --- |
| M0 | Done | Reliable references, explicit public reads, batches, recovery, and fresh authorization status |
| M1 | Done | Opt-in public-channel search and recommendations without granting post access |
| M2 | Done | Forum topics, pins, counters, reactions, polls, and bounded selected-album retrieval |
| R1 | Done | Domain/application/adapter layers and explicit dependencies, preserving all 14 tools |
| R2 | Done | Installable 0.2.0 package, Windows support, shared local HTTP process, client guides, and release automation |
| R3 | Implemented; CI/publication pending | Recoverable connections, bounded catalogs/search, and compact typed 0.3 responses |
| M3 | Planned after R3 | Selected attachment downloads with size limits and delivery to the calling client |
| M4 | Planned after M3 | New messages and available changes, persistent checkpoints, and source-linked export |

The [historical completion records](docs/specs/README.md#historical-completion-records)
include offline test results. They do not claim live Telegram validation.

## Current stage: R3 — Reliable retrieval and compact responses (0.3.0)

Implementation is complete and prepared for one focused PR before attachment downloads. It adds:

- Bounded connection recovery and fresh authorization checks before saved-buffer output.
- A completed-catalog cache (default/max 30 seconds), incremental cold loads, and a
  direct-source path independent of catalog scans when folder policy is not involved.
- Native account-dialog global search with all-chat permission, fair restricted-source priming with explicit
  progress, safe native media/link filters, and webpage preview metadata.
- Raw payload mapping without Telethon's private message initializer.
- A compact typed contract for all 14 tools: shared chat summaries, canonical string
  references, sparse optional metadata, and complete mirrored text/structured JSON.
- Reproducible response/schema size checks and a bounded selected-source live probe.

See [migration](docs/migration-0.3.md), [usage](docs/usage.md), and the
[validation record](docs/validation-0.3.md). Package and installation examples target
0.3.0; publication is verified separately through the [release workflow](docs/releasing.md).

Acceptance requires full offline `make check`, GitHub `checks`, diff review, at least
60% response-size reduction on the fixed 30-post fixture, at least 25% reduction in
`tools/list`, and bounded user-selected live validation before publication. Global
live search requires separate account-wide permission; it is not part of the selected
sources probe. No message content or private source identifiers belong in public reports.

Local `make check` passed with 891 tests and one skipped; wheel and source-distribution
installation smoke checks passed. The response/schema measurements passed:
61.6% and 29.8% smaller respectively.
The selected-source live probe completed 24 checks without issues, including reconnect
and a subsequent history read. Global search, catalogs, forums, and authorization
revocation remain covered offline only. Final GitHub checks, merge, and publication
are pending; implementation completion does not imply a published release.

## Next: M3 — Selected attachment downloads

Use message references and existing attachment/album metadata to select files.
Before implementation, define size and content limits, temporary-file lifetime,
resource delivery for both transports, expired Telegram file-reference recovery,
and behavior for protected or unavailable media. Downloads must enforce current
source permissions and must not silently fetch whole conversations or archives.

## Later decisions

M4 will define persistent checkpoints and explicit export coverage. Other directions
remain proposals: saved searches and source collections, local text/semantic indexes,
OCR or transcription, monitoring and notifications, optional writes or administration,
and remote access or separately isolated account profiles.

Preserve originals and source links; summaries must not replace them. No basic
retrieval capability should require a built-in LLM or a persistent message database.

## Working process

Select a bounded item, finalize its contract, and use a Conventional Branch from the
latest `main`. Add appropriate offline checks and documentation, open a focused PR,
wait for the required GitHub `checks` job, and squash-merge. Record completed work
here and public changes in [CHANGELOG.md](CHANGELOG.md). Archive detailed completed
specifications instead of growing this status document. See [CONTRIBUTING.md](CONTRIBUTING.md).
