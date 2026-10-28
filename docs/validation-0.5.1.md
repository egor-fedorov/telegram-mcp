# 0.5.1 validation record

This record covers the 0.5.1 security patch, prepared on 2026-10-28 (UTC). Since the
[0.5.0 record](validation-0.5.md), `main` gained these changes:

- [#59](https://github.com/egor-fedorov/telegram-mcp/pull/59), ported from
  [#34](https://github.com/egor-fedorov/telegram-mcp/pull/34): Telegram's
  service-notification chat `777000`, which delivers login codes, is never readable.
- [#62](https://github.com/egor-fedorov/telegram-mcp/pull/62): noncanonical numeric IDs
  are rejected before access grants, closing the Unicode-digit alias bypass of #59.
  Configuration also rejects noncanonical allowlist IDs.
- [R6](specs/r6-structural-consolidation.md), PRs #48–#58: retrieval, monitoring,
  export, download and storage code was reorganized without changing the MCP contract
  or persisted formats.

Application code matches source revision `c85f60b6e35959738b9dfa2154acc487a0fc73ae`,
after #62 was merged. Checks ran on release candidate `ae974b4`, which incorporates
that fix; subsequent release edits change documentation only.
Publication is pending. PyPI, GitHub Release and MCP Registry checks will be recorded
after the release workflow completes.

## Offline verification

`make check` passed Ruff formatting/lint, ty and **1478 tests, with two
platform-specific skips**. The required GitHub `checks` job covers Python 3.12–3.15 on
Linux and Python 3.12/3.15 on macOS and Windows.

The service-notification fix is covered offline. Six new tests fail without it:

- catalog, search, `get_messages` and `get_chat`;
- all-chat and folder `get_history`, including explicit `chat_ids` and `folder_ids`;
- `create_watch`, folder watches, `get_changes`, `export_history` and
  `download_attachment`;
- folder computation and configuration validation.

The follow-up adds 41 regression cases that fail without #62. They cover canonical
ID validation in configuration and every access-grant path, plus Unicode-digit
message/context references through both MCP modes and the real Telegram adapter with
synthetic responses. Rejected references cause no entity or message RPC. Real login
codes were not read.

R6 kept its invariants under fixtures: the MCP schema contract, tool order and
descriptions, wire/domain field parity, and 0.5.0 checkpoint, watch and archive state
loading and serializing byte for byte.

## Response and discovery sizes

Remeasured with the 0.5.0 fixtures on Linux/Python 3.12.15. Every value is identical to
0.5.0.

| Fixture | Bytes |
| --- | ---: |
| `tools/list`, 22 tools, compact schemas | 27,050 |
| `tools/list`, 22 tools, full schemas | 114,041 |
| `prompts/list`, two prompts | 2,037 |
| Single-chat history, 30 posts retaining 10,260 text characters | 32,327 |

## Distribution

The wheel and source distribution were rebuilt locally with the #62 fix included.
`scripts/smoke_distribution.py` installed both into isolated environments and passed the
Registry launch, metadata, CLI, STDIO, HTTP and bridge checks with compact and full
schemas. `scripts/check_registry.py` validated
`io.github.egor-fedorov/telegram-mcp@0.5.1`.

## Selected-source checks

Agent-run live checks passed on 2026-10-28, using Linux/Python 3.12.15 and the existing
authorized session. The [bounded probe](live-validation.md) exercised two previously
approved channels with `--m4` and one explicit photo attachment; its separate
`--monitoring` mode checked the same sources. A disposable additional harness checked
saved-watch delivery and one previously approved document/video attachment from a
third selected channel. These checks used the production service and Telegram backend.

| Exercised path | Observed result |
| --- | --- |
| Authorization and history | Fresh authorization; two five-message pages from one source and three messages from the test source |
| Selected search | Keyword, link-only and photo results; an empty link page continued successfully; voice/audio queries were valid but empty |
| Messages, context and album | Two explicit messages, both context windows and a two-member album |
| Photo download | 140,229 bytes; MCP resource and permanent-copy bytes, SHA-256 and source metadata matched |
| Document/video download | 13,168,891 bytes; resource and permanent-copy bytes, SHA-256 and source metadata matched |
| Changes and exports | Both baselines reopened; bounded checkpoint continuations passed; four JSONL parts, including a five-record part, passed resource/archive, checksum and framing checks |
| Connection recovery | The probe disconnected only its own client, then reauthorized and read five messages in the same runtime |
| Monitoring metadata | Both selected peer states returned in each of two batches; watch creation was idempotent and the disposable watch was deleted |
| Saved-watch recovery | One change archived before acknowledgement; after closing and rebuilding the service and stores, the same batch and 1,333-byte archive replayed exactly |
| Acknowledgement persistence | Duplicate acknowledgement succeeded; another service/store reopen retained it; the next poll was empty and complete; deleting the watch retained its valid archive |

The watch check backfilled an existing test post, with at most four polling calls of
five changes per phase. It did not send a new post, edit Telegram content, kill a
process or simulate a power failure. Process/transport restart and crash cases remain
covered offline; the earlier live client workflow is recorded in the 0.5.0 record.

Live checks excluded global search, dialog/folder enumeration, folder-selected reads,
unselected sources and service-notification content. No approved source was a forum,
so topics were not exercised. Client UIs and live HTTP/`connect` were not repeated;
offline transport and schema checks passed. Empty query results do not validate the
parsing of a matching voice/audio message.

Downloads were enabled only in the probe's in-memory settings. Production configuration
and subscriptions were preserved. All probe databases, downloaded content, archives and
temporary environments were isolated beneath ignored `dist/` and removed on exit.
Source identities, message bodies, tokens and credentials are absent from this record.
