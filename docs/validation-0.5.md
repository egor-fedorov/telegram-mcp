# 0.5 validation record

M5 was validated on 2026-10-27 against source revision
`ca8fbe2e70ce5ea68698792860bc8d6359cb79f8`, after [PR #43](https://github.com/egor-fedorov/telegram-mcp/pull/43)
was merged. This records implementation acceptance, not package publication. Version
0.5.0 still needs release preparation under the [release procedure](releasing.md).

## Offline verification

`make check` passed Ruff formatting/lint, ty and **1403 tests, with two platform-specific
skips**. The implementation's [CI run](https://github.com/egor-fedorov/telegram-mcp/actions/runs/40474978338)
passed the required `checks` job and the supported Python/platform matrix. Coverage
includes multi-source budgets, access revocation, archive failure/recovery, prompt
validation, completion without Telegram calls, and STDIO/HTTP/`connect` parity.

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
