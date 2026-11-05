# 0.7.0 validation record

Release: 0.7.0. PyPI, GitHub Release and MCP Registry publication were verified on
2026-11-05. The checks below establish the listed coverage; other clients' outbound
confirmation UIs remain unverified.

The release candidate changes metadata, installation pins and documentation only;
runtime behavior and locked dependency versions match the live-validated code.

## Scope and environment

The maintainer selected one broadcast test channel and a synthetic 34-byte text file,
then explicitly selected one private bot's `Show Details` callback for the remaining
checks. No account-wide search, real outreach or real login notices were included.
Telegram credentials, message bodies and operation keys are not recorded here.

- Initial implementation: `5212de82170137e18c8c9a5cca8c975c5324cee2` (PR #75).
- Date: 2026-11-05; Arch Linux; Telethon 1.45.0, TL layer 229.
- Client: the maintainer's Codex session using the source checkout's `connect` proxy
  and shared local HTTP server. The Codex UI build was not recorded; the separately
  installed local CLI reported 0.166.0.
- The normal systemd service was stopped. A separate test configuration reused the
  existing authorized session, with read/write access limited to the selected channel.
  Drafts, text sends and file sends were enabled; callbacks initially remained disabled.
  The later callback check enabled them and added only the selected bot to the test
  configuration's read/write allowlists. The normal configuration was unchanged.

## Observed live results

| Check | Result |
| --- | --- |
| Fresh status and tool discovery | Authorized and connected; 33 tools; confirmation support advertised |
| Selected channel metadata | Correct canonical ID, joined membership and send permission |
| Immutable local file registration | Filename, size and SHA-256 matched; registration survived restart |
| Native text draft | Created and visible in the maintainer's Telegram client |
| Manual edit and stale revision | `DRAFT_CONFLICT`; the maintainer's change remained intact |
| Form acceptance | Maintainer confirmed seeing and accepting the form; one text message sent |
| Form refusal | `CONFIRMATION_DECLINED`; no new message appeared |
| File send and download | Caption and metadata preserved; downloaded SHA-256 matched the original |
| Duplicate text/file sends after restart | Original operation receipts and message references returned; no duplicates |
| Changed payload under an existing key | `IDEMPOTENCY_CONFLICT`; no dispatch |
| Draft preservation during sends | The manually edited draft remained unchanged |
| Outgoing metadata | Both test publications had `outgoing=true` |
| `get_send_status` | Failed at MCP response validation; journal replay itself worked |
| Native draft with a document | Failed with `MediaInvalidError`; no draft was created |

The first send was intended as a refusal exercise, but the maintainer accepted its
form. It is counted only as an acceptance check. A separate operation then verified
refusal. No conclusion about automatic approval is drawn from that first call.

## Follow-up changes

`get_send_status` now returns the receipt in `result`, matching the already advertised
union schema. Tests cover stored text/file/callback receipts in compact/full schema
modes and legacy/modern MCP, plus receipt retrieval through STDIO, HTTP and `connect`.
The journal format and direct send responses remain unchanged.

The maintainer chose to defer file drafts. `file_id` remains a recognized argument but
returns `DRAFT_ATTACHMENT_UNSUPPORTED` before any backend/file call or confirmation.
Text drafts remain supported; media/rich drafts are readable but cannot be overwritten
by these tools. Registered files and separately confirmed file sends remain supported.

Targeted regression tests passed (119 tests). Full `make check` passed: Ruff format/lint,
ty and 1846 tests, with two platform-specific skips. The updated skill passed its validator.

After restarting the same test server with the follow-up code, `get_send_status` returned
the existing text/file `sent` receipts and the `CONFIRMATION_DECLINED` receipt inside
`result`. File-draft create and update calls both returned `DRAFT_ATTACHMENT_UNSUPPORTED`.
The existing manually edited draft retained its exact text and revision, and the latest
channel message remained unchanged. No new sends were needed for these checks.

## Additional selected-source checks

On `c460285d231bee2eafb2f6e3caccea740aaf9112` from PR #81, a bounded search of the two synthetic send
messages returned both with `direction=outgoing` and neither with `direction=incoming`.
Both searches completed without issues.

A clearly labelled synthetic authentication notice was sent to the same test channel
through the normal confirmation path. No real credential was used. `get_messages`
returned an empty-text placeholder with `AUTH_NOTIFICATION_FILTERED`.
Both a checkpoint continuation and a time-bounded history export were saved as JSONL.
Their temporary files and permanent archive copies matched byte-for-byte, and their
SHA-256 values matched the returned metadata. Both contained the omission marker;
neither contained the synthetic notice text or code. This checks one supported notice
pattern, not arbitrary credential detection or immunity to prompt injection.

The selected bot's message exposed inline button labels, callback types, opaque IDs
and a revision. An intentionally mismatched revision returned `MESSAGE_CONFLICT`
before confirmation or dispatch. The authorized `Show Details` call then completed
through the normal confirmation path, and its bounded observation contained one new
same-dialog message. A separate read confirmed that this message replied to the
selected source. Its `Hide Details` button was inspected but not invoked.

`get_send_status` returned the callback receipt inside `result`. Repeating the same
key and arguments returned the identical receipt and observed reference; the bot's
latest message remained unchanged. The source's unchanged text/buttons retained
their revision. This live check covers revision rejection and stability; changing only
button payloads remains covered by offline tests. Callback observations still report
`observation_complete=false` and do not guarantee causality or complete bot activity.

Final status was authorized and connected with no active cooldown. The maintainer's
manually edited native draft retained its exact text and revision. Full `make check`
passed again: Ruff format/lint, ty, 1846 tests and two platform-specific skips.

## Release candidate checks

On Linux/Python 3.12.15, `make check` passed with version 0.7.0: Ruff format/lint, ty,
1846 tests and two platform-specific skips. All 52 dependency records in `uv.lock`
match the previous revision; only the project version changed. Registry identity,
launch metadata and version synchronization passed. Release notes were extracted
from the dated changelog and checked for unwrapped paragraphs/list items.

Wheel and source-distribution installations outside the checkout passed CLI, Registry
launch, STDIO, HTTP and bridge checks in compact/full schema modes. Both installations
also passed the extraction-extra document and worker probes. The MCPB passed the pinned
official manifest validator, a byte-identical rebuild and an isolated uv install/proxy
check with external-profile preservation. Temporary package and bundle environments
were removed; generated candidates remain in ignored `dist/release-0.7.0/`.

Synthetic wire measurements use 30 messages containing 10,260 text characters. The
complete MCP response, including text fallback and structured content, is 32,327 bytes
in both schema modes; every text character is preserved.

| Tool surface | Tools | Compact `tools/list` bytes | Full `tools/list` bytes |
| --- | ---: | ---: | ---: |
| Default reading | 23 | 28,819 | 124,641 |
| Outbound tools enabled | 33 | 35,314 | 142,087 |

The GitHub release pipeline repeated autonomous checks and distribution/bundle probes
on the tagged commit, including Windows/macOS and Python 3.12–3.15. Publication was
verified separately following [releasing](releasing.md). Other clients' outbound
confirmation UIs remain unverified; the earlier Windows Desktop read-only report does
not imply write support.

## Publication verification

Annotated tag `v0.7.0` points to release commit
`b78237698b85508d8f16340ef35e7ea9e0f9133e` from [PR #83](https://github.com/egor-fedorov/telegram-mcp/pull/83).
The [release workflow](https://github.com/egor-fedorov/telegram-mcp/actions/runs/42012691679)
passed the Linux/Python 3.12–3.15 and Windows/macOS 3.12/3.15 matrix, package/MCPB
checks, PyPI publishing, GitHub Release creation and Registry publication.

- [PyPI 0.7.0](https://pypi.org/project/telegram-context-mcp/0.7.0/) contains a non-yanked wheel and source distribution. Their SHA-256 values match the corresponding GitHub assets.
- [GitHub Release v0.7.0](https://github.com/egor-fedorov/telegram-mcp/releases/tag/v0.7.0) contains exactly four assets: wheel (230,872 bytes), sdist (575,751 bytes), MCPB (378,813 bytes) and its checksum. Notes match the tagged changelog without manual wrapping inside paragraphs or list items.
- MCPB SHA-256 is `7fa1bd8186f095896b6833d94532c197ad8cd20265830c765f140712dafa6953`. Its manifest version is 0.7.0, and the embedded wheel is byte-identical to the published wheel. Temporary verification downloads were removed.
- The [Registry entry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.egor-fedorov%2Ftelegram-mcp/versions/0.7.0) is active and matches the tagged manifest. `check_registry.py --tag v0.7.0 --check-pypi --require-published` passed.
- A fresh, isolated PyPI invocation with `[extraction]` returned `telegram-mcp 0.7.0`. Updating the persistent shared-server installation remains a separate local upgrade step.

## Dependency follow-up identified after publication

GitHub created four high-severity `pyasn1` alerts during the 2026-11-05 release workflow;
they were noticed on the post-publication documentation push.
The 0.7.0 lockfile and MCPB pin 0.6.4. The advisories cover excessive decoder CPU use
in [schemaless containers](https://github.com/advisories/GHSA-fm9c-5rcf-2q5p),
[strings/ANY](https://github.com/advisories/GHSA-9m75-vc3g-4x39),
[BIT STRING](https://github.com/advisories/GHSA-2qm8-x737-gmw5) and
[REAL](https://github.com/advisories/GHSA-mm57-4v3p-rchx); all are fixed in 0.6.5.
The verified fresh PyPI installation resolved 0.6.5 already.

Inspection of the installed Telethon 1.45.0 path found ASN.1 decoding through `rsa`
for its built-in public keys; no path from Telegram message/attachment content to
that decoder was identified. This is a bounded reachability review, not a dismissal
of the dependency alerts. [Dependabot PR #84](https://github.com/egor-fedorov/telegram-mcp/pull/84)
tracks the lockfile update; a subsequent bundle release is needed to ship that fix.
