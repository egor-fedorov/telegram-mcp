# R6: Structural consolidation

Status: in progress after 0.5.0. Feature work is frozen until R6 completes; `main`
accepts only R6 refactoring, bug fixes and reviewed dependency updates.

In the week before 0.5.0 the source grew from about 4,000 to 12,100 lines. Feature PRs
touched 16–29 source files, six to eight of them the same central modules: the MCP tool
surface (changed in 10 PRs), the application facade (8), the wire DTOs, domain models,
Telegram backend, configuration and CLI (7 each). Complexity concentrates in a few
functions, and a full local test run takes about four and a half minutes. The layered
architecture and its import guard remain sound; R6 reorganizes code within them.

## Invariants

Every R6 change preserves public behavior:

- `tests/fixtures/mcp_contract.json` stays unchanged, and `tools/list` keeps its order.
- Persisted formats stay readable and unchanged: checkpoint and watch databases, watch
  state (`schema_version` 1), archive `metadata.json` and managed file names.
- Assertions in existing application, MCP, integration and contract tests stay unchanged.
  Import paths and `monkeypatch` targets of moved internal modules may change.
- No new runtime dependency, CLI option or configuration field.
- Error codes, messages and the order of argument checks stay the same.

The `Service` facade remains the stable application entry point for adapters and
tests (about 540 test call sites). The aggregate Telegram backend remains an assembly
detail of the composition root. Both are intentionally kept.

## Changes

1. **Guardrails.** Enforce Ruff `C901` with a maximum complexity of 15; mark the
   existing offenders explicitly and remove each marker when its function is split.
   Run tests in parallel with pytest-xdist locally and in CI, mark the slowest tests,
   and add `make test-fast`. Pin the `tools/list` order, check that wire DTO fields
   match their domain models except documented projections, and check that 0.5 state
   fixtures still load and serialize identically.
2. **MCP tools by feature.** Turn the MCP adapter into a package: server assembly,
   shared parameter types, error exposure and one registration module per feature
   (watches, changes, attachments, status, discovery, catalog, messages).
3. **Wire DTOs by feature.** Turn the wire module into a package with the same
   features, keeping every public name importable from `telegram_mcp.adapters.wire`.
   Split the response projection into smaller functions.
4. **Query preparation.** Split preparation into validation, source resolution, cursor
   resumption, topic checks and first-page selection. Move history selector rules into
   a pure, separately tested function.
5. **Incremental pipeline.** Share the forward history read loop of changes and history
   export, the export publication transaction (temporary file, permanent copy and
   checkpoint confirmation with compensation) and protected-record omissions.
6. **Watches.** Separate the source sweep and the pending-batch delivery lifecycle from
   the watch service. Derive an explicit archive status of a pending batch without
   changing its stored form.
7. **Local storage.** Share the leased cache directory, expiring token files and private
   exclusive file creation of download, export and archive stores. Where implementations
   differ, keep the stricter behavior. Split checkpoint persistence steps.
8. **Telegram readers.** Split the remaining complex topic, album and global-search readers.
9. **Documentation.** Update the architecture, contributor guide, roadmap and changelog.

## Out of scope

- Watch poll performance on large selections: each poll copies and serializes the whole
  watch state. A separate change can address it after R6.
- Splitting the shared test fake and domain model module by feature.
- New tools, parameters, prompts or configuration.

## Completion

- `C901` is enforced at 15 with no exceptions, or at most three documented exceptions
  in Telegram readers.
- MCP and wire modules stay below about 250 lines each. Adding a tool needs no change to
  server assembly or shared wire code beyond one registration line.
- The publication transaction, forward read loop and expiring file store each have one
  implementation.
- A full parallel `make check` is substantially faster than before, and `make test-fast`
  takes under a minute.
- `make check` and the required GitHub `checks` job pass for every change.
