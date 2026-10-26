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
| R3 | Done | Recoverable connections, bounded catalogs/search, and compact typed 0.3 responses |
| M3 | Implemented, unreleased | Selected attachment downloads with size limits and delivery to the calling client |
| M4 | Planned after M3 | New messages and available changes, persistent checkpoints, and source-linked export |
| M5 | Proposed | Optional plain-text message sending with a separate allowlist and explicit delivery status |

The [historical completion records](docs/specs/README.md#historical-completion-records)
include offline test results. They do not claim live Telegram validation.

The published 0.3.0 retrieval work is recorded in the [changelog](CHANGELOG.md#030---2026-10-22),
[migration guide](docs/migration-0.3.md) and [validation record](docs/validation-0.3.md).
Release and live-validation evidence belongs there, rather than in this priority list.

## Completed maintenance: repository organization

Tests now follow the domain, application, adapter, integration and contract boundaries;
shared helpers are explicit support modules. Telegram search, topic history and album
windows have focused readers sharing the existing runtime and entity cache. Existing
MCP contracts and retrieval behavior are preserved. The architecture and contributor
guides describe the current structure, and the usage guide has a navigation index.

Development maintenance also updates ty to 0.0.92 and Ruff to 0.17.2, corrects async context-manager
annotations, and separates development-tool updates in Dependabot while retaining
manual review and CI. See [contributor guidance](CONTRIBUTING.md#dependencies-and-releases).

Dependency maintenance updates MCP to 2.3.0, the HTTP stack and platformdirs, plus PyJWT 2.15.1
and OpenTelemetry API 1.45.1 for the reported transitive dependency vulnerabilities.
Updates receive manual compatibility review and offline/CI validation.
CI and release workflows also use reviewed patch updates of the SHA-pinned setup
and artifact actions.

## M3 — Selected attachment downloads (unreleased)

Adds opt-in `download_attachment` for one selected attachment up to 20 MiB, with a
temporary local file and MCP resource delivered over STDIO, HTTP, or `connect`.
Downloads expire after 15 minutes; storage is bounded to 100 MiB / 32 files. Access,
protection and media identity are rechecked before resource output. Temporary files
are cleaned up, and stale Telegram references receive one controlled refresh.
See the [M3 contract](docs/specs/m3-attachment-downloads.md) and
[usage](docs/usage.md#download-a-selected-attachment-unreleased).

Local `make check` passed with 1,036 tests and two Windows-only skips on Linux,
including full 20 MiB resource transfers over all three connection paths.

Live attachment validation on explicitly selected photo/document sources is separate
and remains required before publishing this capability. No version tag or package
publication is included in the implementation PR; published 0.3.0 is unchanged.

## Next: M4 — Changes and export

Define persistent checkpoints, retrieval of new messages and available changes, and
export with original source links. Its detailed contract remains to be planned.

## Proposed: M5 — Optional message sending

Adds `send_message` for plain text in explicitly allowlisted chats, separate from read
permissions and off by default, with optional per-message confirmation and a reported
delivery status that is never retried automatically. Under review; see the
[M5 proposal](docs/specs/m5-message-sending.md).

## Later decisions

M4 will define persistent checkpoints and explicit export coverage. Other directions
remain proposals: saved searches and source collections, local text/semantic indexes,
OCR or transcription, monitoring and notifications, other writes or administration,
and remote access or separately isolated account profiles.

Preserve originals and source links; summaries must not replace them. No basic
retrieval capability should require a built-in LLM or a persistent message database.

## Working process

Select a bounded item, finalize its contract, and use a Conventional Branch from the
latest `main`. Add appropriate offline checks and documentation, open a focused PR,
wait for the required GitHub `checks` job, and squash-merge. Record completed work
here and public changes in [CHANGELOG.md](CHANGELOG.md). Archive detailed completed
specifications instead of growing this status document. See [CONTRIBUTING.md](CONTRIBUTING.md).
