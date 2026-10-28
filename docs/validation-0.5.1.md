# 0.5.1 validation record

This record covers the 0.5.1 security patch, prepared on 2026-10-28 (UTC). Since the
[0.5.0 record](validation-0.5.md), `main` gained two changes:

- [#59](https://github.com/egor-fedorov/telegram-mcp/pull/59), ported from
  [#34](https://github.com/egor-fedorov/telegram-mcp/pull/34): Telegram's
  service-notification chat `777000`, which delivers login codes, is never readable.
- [R6](specs/r6-structural-consolidation.md), PRs #48–#58: retrieval, monitoring,
  export, download and storage code was reorganized without changing the MCP contract
  or persisted formats.

Application code matches source revision `20340f566d8ffebee80f310205a81d796d37ed9d`,
after #59 was merged; the release changes package metadata and documentation only.
Publication is pending. PyPI, GitHub Release and MCP Registry checks will be recorded
after the release workflow completes.

## Offline verification

`make check` passed Ruff formatting/lint, ty and **1437 tests, with two
platform-specific skips**. The required GitHub `checks` job covers Python 3.12–3.15 on
Linux and Python 3.12/3.15 on macOS and Windows.

The service-notification fix is covered offline. Six new tests fail without it:

- catalog, search, `get_messages` and `get_chat`;
- all-chat and folder `get_history`, including explicit `chat_ids` and `folder_ids`;
- `create_watch`, folder watches, `get_changes`, `export_history` and
  `download_attachment`;
- folder computation and configuration validation.

R6 kept its invariants under fixtures: the MCP schema contract, tool order and
descriptions, wire/domain field parity, and 0.5.0 checkpoint, watch and archive state
loading and serializing byte for byte.

## Response and discovery sizes

Measured with the 0.5.0 fixtures on Linux/Python 3.12. Every value is identical to
0.5.0.

| Fixture | Bytes |
| --- | ---: |
| `tools/list`, 22 tools, compact schemas | 27,050 |
| `tools/list`, 22 tools, full schemas | 114,041 |
| `prompts/list`, two prompts | 2,037 |
| Single-chat history, 30 posts retaining 10,260 text characters | 32,327 |

## Distribution

The wheel (175,316 bytes) and source distribution (435,880 bytes) were built locally.
`scripts/smoke_distribution.py` installed both outside the checkout and passed the
Registry launch, metadata, CLI, STDIO, HTTP and bridge checks with compact and full
schemas. `scripts/check_registry.py --sync-version` validated
`io.github.egor-fedorov/telegram-mcp@0.5.1`.

## Selected-source checks

Pending: a maintainer-run [bounded probe](live-validation.md) on explicitly approved
sources. R6 changed the internal structure of these paths, so the release requires
history, search, explicit messages, context, an attachment, M4 changes and export, and
monitoring metadata. Forum topics are included when an approved source is a forum.

The service-notification chat is excluded from live checks by design; offline tests
cover it.
