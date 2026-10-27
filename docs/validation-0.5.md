# 0.5 validation record

This record covers the 0.5.0 release candidate on 2026-10-27 (UTC). M5 was validated against
source revision
`ca8fbe2e70ce5ea68698792860bc8d6359cb79f8`, after [PR #43](https://github.com/egor-fedorov/telegram-mcp/pull/43)
was merged. The release changes package metadata and documentation; application code
is unchanged from that live-tested revision. Publication follows the
[release procedure](releasing.md); verify the version on
[PyPI](https://pypi.org/project/telegram-context-mcp/0.5.0/) and
[GitHub Releases](https://github.com/egor-fedorov/telegram-mcp/releases/tag/v0.5.0).

## Offline verification

`make check` passed Ruff formatting/lint, ty and **1403 tests, with two platform-specific
skips**. The implementation's [CI run](https://github.com/egor-fedorov/telegram-mcp/actions/runs/40474978338)
passed the required `checks` job and the supported Python/platform matrix. Coverage
includes multi-source budgets, access revocation, archive failure/recovery, prompt
validation, completion without Telegram calls, and STDIO/HTTP/`connect` parity.

## Response and discovery sizes

Release measurements use synthetic offline fixtures. Full result sizes serialize
the MCP result as UTF-8, including both text fallback and structured content; the
JSON-RPC envelope is excluded.

| Fixture | Bytes |
| --- | ---: |
| `tools/list`, 22 tools, compact schemas | 27,050 |
| `tools/list`, 22 tools, full schemas | 114,041 |
| `prompts/list`, two prompts | 2,037 |
| Single-chat history, 30 posts retaining 10,260 text characters | 32,327 |

Multi-source fixtures contain five posts per source and request `limit=50` with
`ordering="page"`. The source budget is explicitly raised for 100-source fixtures.

| Sources | Message budget | MCP calls | History reads | Messages | Coverage | Full result bytes |
| ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 20 | 100 | 2 | 20 | 100 | Complete | 99,816 |
| 100 | 100 | 2 | 20 | 100 | Partial, budget exhausted | 100,962 |
| 100 | 1000 | 10 | 100 | 500 | Complete | 503,796 |

Each first page was nonempty after ten synthetic history reads. Local fixture time
to that page was 6.7–9.4 ms; this is not a Telegram latency estimate. The compact JSON
payload alone totaled 48,304, 48,846 and 243,754 bytes respectively. Clients determine
which representations enter model context. Fixtures live in
`tests/adapters/mcp/test_wire.py` and `test_multi_source_history.py`.

## Agent-run selected-source checks

A disposable harness used the production MCP server, service and Telegram backend
with the existing authorized session on Linux/Python 3.12.15. The MCP SDK 2.3.0 client
started independent STDIO server processes. Access was restricted to two explicitly
approved channels; subscriptions and archives used isolated temporary storage.

All 14 reported checks passed:

| Exercised path | Observed result |
| --- | --- |
| Prompt discovery and rendering | Both prompts advertised; bounded period rendered; invalid arguments rejected; no Telegram calls |
| Authorization | Connected and freshly authorized |
| Source budget | Selecting two sources with `max_sources=1` rejected before any history read |
| Multi-source period | Two pages of three messages; fixed seven-day interval; no duplicate references; both sources read |
| Message budget | Six messages exhausted the traversal budget; coverage remained partial with a cursor; continuing at the same cap performed no additional history read |
| Watch completion | Local prefix completion returned the selected watch without Telegram calls |
| Archive before acknowledgement | One change saved as a 1324-byte JSONL batch with matching sidecar, size and SHA-256 |
| Restart before acknowledgement | Same pending batch and archive replayed; no duplicate copy; batch identity recovered independently from retained files |
| Acknowledgement and another restart | Acknowledgement derived from the archive persisted; the batch file remained readable and valid |
| Cleanup/recreation | Retained archive survived deletion of the disposable watch; a fresh isolated watch was prepared for the client check |

The multi-source traversal used two backend history-reader calls. Authorization and
entity resolution are additional operations; this is not a total Telegram RPC count.
Restarts used controlled process shutdown, not forced termination or power failure.

## Maintainer-run Claude Code check

The maintainer reported a successful `watch_digest` command in **Claude Code 2.1.310**
and confirmed watch-name argument completion. It used the isolated STDIO server on
Linux described above; the client's host OS was not separately reported.

With a budget of five changes and `save=true`, the client processed one change,
reported zero omissions/issues and a complete polling pass, provided the source link
and saved-file path, then acknowledged the batch. Independent local checks confirmed
the reported file's 1324-byte size, SHA-256, watch/batch identity and complete batch
header. The isolated database contained the matching acknowledgement, no pending
batch and no remaining sources.

This confirms the observed client workflow. It does not establish that a model always
follows the template or that `kind="new"` proves a post's original publication history.

## Coverage and cleanup

Live checks excluded account-wide searches, dialog/folder enumeration, folder-name
resolution, folder-selected history, HTTP/`connect`, and other client UIs. Period
prompt rendering was checked through the SDK; its execution in Claude Code was not
reported. Relevant protocol and folder behavior remains covered offline. These checks
do not extend the resource/binary compatibility claims in the [client guide](clients.md).

No source identities, post bodies, batch tokens, local archive paths or credentials
are included in this record. The disposable harness, subscription database, archives
and Claude Code test-server entry were removed after verification. Production access
configuration and existing subscriptions were preserved.
