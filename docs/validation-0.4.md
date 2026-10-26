# 0.4.0 validation record

This record covers the 0.4.0 release candidate on 2026-10-26 (UTC). The release
changes package metadata and documentation; application code is unchanged from
`1ca096cd2eb533c705e91b5cd00bb89a8fe18778`, the selected-source live-tested revision.
Its implementation [CI run](https://github.com/egor-fedorov/telegram-mcp/actions/runs/40389941237)
passed all jobs, including `checks`. Release preparation and publication follow
[the release procedure](releasing.md). Verify published artifacts through
[PyPI](https://pypi.org/project/telegram-context-mcp/0.4.0/) and
[GitHub Releases](https://github.com/egor-fedorov/telegram-mcp/releases/tag/v0.4.0).

## Offline verification

Final local `make check` passed Ruff formatting/lint, ty and **1255 tests, with two
platform-specific skips** on Linux/Python 3.12. Dependency versions are unchanged
by the release PR. Wheel and source distribution installations pass metadata, CLI,
STDIO, HTTP and bridge checks with compact and full tool schemas in isolated environments.
The release workflow repeats the required CI matrix before publication: Linux on
Python 3.12–3.15, plus macOS and Windows on Python 3.12 and 3.15.

Coverage includes large catalogs/scopes, both search orderings, source revocation,
FloodWait and network recovery, durable checkpoints, independent consumers, subscription
replay/acknowledgement, archive cancellation and 20 MiB resource transfers over all
three transports. Quota tests exercise 4200 polls or fresh baselines; only those
high-volume tests disable SQLite disk flushing, while restart/publication tests retain
production durability settings. The original Windows timeout was resolved without
reducing those iterations or changing production database settings.

## Response and discovery sizes

These measurements serialize the complete MCP result, including text fallback and
structured content, as UTF-8. They use the existing offline regression fixtures,
not real account content or estimates of model tokens.

| Fixture | Bytes |
| --- | ---: |
| `tools/list`, 22 tools, default compact schemas | 25,782 |
| `tools/list`, 22 tools, full schemas | 110,812 |
| Server instructions | 851 |
| History: 30 posts retaining all 10,260 text characters | 32,327 |
| Search: 10 short messages selected from 3000 chats | 7,939 |
| Chat listing: 30 chats selected from 3000 | 20,811 |
| Scope object in each 3000-chat result | 896 |

Compact discovery is about 77% smaller than full discovery for the same 22 tools.
Scope previews retain exact counts and bounded ID lists. Result sizes depend on
content and metadata; these are fixture measurements rather than universal limits.
Reproduce the contract and budget assertions with:

```sh
uv run --locked pytest tests/adapters/mcp/test_wire.py \
  tests/adapters/mcp/test_large_scope.py tests/adapters/mcp/test_watches.py \
  tests/contracts/test_mcp.py
```

## Selected-source live checks

Checks used explicitly authorized sources and the existing session. Global searches,
dialog catalogs and folder scans were excluded. Source identities, message bodies,
batch/checkpoint tokens and credentials are omitted from this public record.

| Exercised path | Evidence |
| --- | --- |
| Retrieval and controlled reconnect | Agent-run bounded probes passed history, selected keyword/link/media requests, explicit message/context reads, and reconnection of their own client followed by history |
| Video through Telegram's document attachment path | Agent-run download of 13,168,891 bytes; resource MIME, size/SHA-256, permanent-copy bytes and manifest source/hash passed |
| Photo attachment path | Agent selected the latest post of the approved test channel; download of 140,229 bytes passed the same resource/archive checks |
| Changes baseline and restart | Maintainer-provided `--m4` report passed baseline creation, checkpoint-store reopen and two continuation calls; both continuations returned zero changes |
| History export | The same M4 report passed two JSONL parts, first empty and then five records, with resource/archive integrity and framing checks |
| Album | The maintainer report found two members in its bounded neighborhood; completeness was not established |

Attachment checks enabled downloads only for the probe in memory and used a temporary
archive root. The normal configuration was not rewritten. Disposable files, archives
and probe state were removed after each completed check. M4 evidence is explicitly
attributed to the maintainer's supplied log; it was not independently rerun by the agent.

## Manual subscription check through MCP

An agent-run, disposable STDIO MCP harness used one explicitly approved test channel,
an isolated watch store and the production server/service/backend. The maintainer
published and edited the test post manually; the harness never sent Telegram messages.
Each stage opened a new server process using the same isolated state and pinned source.

1. Create a watch and finish the initial empty pass.
2. Receive one newly published post with `kind="new"` and keep its batch unacknowledged.
3. Restart the server, find the saved watch and verify exact equality of the replayed
   payload and batch ID.
4. After the maintainer edits that post, acknowledge the first batch together with
   polling. Receive `kind="updated"` for the same message, without publishing a new post.
5. Retry the old acknowledgement and verify exact replay of the new pending batch.
6. Acknowledge the edit, observe an empty completed pass using one budgeted peer-metadata
   RPC, and delete the disposable watch. Authorization RPCs are additional to that count.

All stages passed. The harness, isolated database and comparison payloads were removed.
This verifies persisted subscription behavior across real processes; it is not a
validation of any particular desktop client's UI or file-handling capabilities.

## Coverage limits

No account-wide search/catalog reads, live folder changes, large-folder load test,
20–40-username resolution run, authorization revocation, real FloodWait injection,
protected/paid media, or historical deletion claims were exercised. Applicable
behavior remains covered by offline tests. Selected media searches sometimes returned
zero results; that does not validate live normalization of every media type.
Reconnect was a controlled disconnect, not a simulated network outage.

Live artifact checks used application resource reads; binary transfer over actual
MCP transports is covered offline. The separate manual subscription check used real
STDIO MCP calls. No client GUI resource compatibility beyond the
[documented capability table](clients.md#reading-files-and-resources) is claimed.
