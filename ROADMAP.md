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
| R2 | Implemented; release pending | Installable 0.2.0 package, Windows support, shared local HTTP process, client guides, and release automation |
| M3 | Planned after R2 | Selected attachment downloads with size limits and delivery to the calling client |
| M4 | Planned after M3 | New messages and available changes, persistent checkpoints, and source-linked export |

The [historical completion records](docs/specs/README.md#historical-completion-records)
include offline test results. They do not claim live Telegram validation.

## Current stage: R2 — Installation, local sharing, and release 0.2.0

Make the existing tools usable from a published package and several local clients
without competing for one Telegram session. Deliver the agreed work in one focused
PR before starting attachment downloads.

- Keep the package name `telegram-context-mcp` and command `telegram-mcp`. Prepare
  version 0.2.0, `--version`, wheel/source builds, clean-install smoke checks, and
  pinned `uv tool install`/`uvx` examples.
- Support Windows, macOS, and Linux on Python 3.12–3.15. Use a portable exclusive
  session lease, explicit UTF-8 files, and accurate POSIX-permission/Windows-ACL guidance.
- Preserve `serve` as standalone STDIO. Add explicit foreground
  `serve --transport http` on `127.0.0.1:8765/mcp`, with a configurable local port and
  a random bearer token in the user profile.
- Add `connect` as a STDIO proxy to that existing server. It reads local configuration
  and the token; it neither starts the server nor falls back to opening a session.
- Share one account, read policy, and in-memory cursor store across trusted local
  clients. Preserve tool schemas and semantics, protect shared call/cursor state,
  and keep the server alive when a client disconnects. This is not tenant isolation.
- Keep login and configuration local. Local `login`, `configure`, `catalog`, and
  `status` require the session owner to stop; MCP `get_status` diagnoses the running
  server. Configuration errors identify their source/field without printing secrets.
  Enter in `configure` preserves the current values, including `allow_all_chats`.
- Provide verified client examples for Codex, Claude Desktop, Claude Code, Cursor,
  and VS Code. Keep a short README and separate usage, configuration, troubleshooting,
  architecture, security, and release documents.
- Extend CI with platform/Python tests and distribution checks while retaining the
  required `checks` job. Review Dependabot PRs manually; its generated `dependabot/`
  branches are an explicit exception to human branch naming rules.
- Publish version tags through GitHub OIDC and a PyPI Trusted Publisher. Create the
  GitHub Release only after successful PyPI publication. See [releasing](docs/releasing.md)
  for the external publisher setup and exact release procedure.

Completion requires offline `make check`, the CI compatibility/package checks, and
review of the transport and credential boundaries. Actual PyPI publication additionally
requires the project name and Trusted Publisher to be configured by its owner; do not
mark publication complete from repository changes alone. Live Telegram validation is
separate and uses sources selected by the user.

The implementation is ready for release validation. Publication status is recorded by
the [Release workflow](https://github.com/egor-fedorov/telegram-mcp/actions/workflows/release.yml)
and [published releases](https://github.com/egor-fedorov/telegram-mcp/releases).

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
