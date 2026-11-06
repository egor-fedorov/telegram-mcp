# 0.8.0 validation record

Release: 0.8.0. PyPI, GitHub Release and MCP Registry publication were verified on 2026-11-05. This record distinguishes selected-source live checks from offline protocol and platform coverage.

## Scope and environment

- Date: 2026-11-05; Arch Linux, Python 3.12.15, Telethon 1.45.0.
- Runtime source: `ccaddeab011a36e6792213b255711cbaa61289ea` (PRs #87–#89). The release candidate changes version metadata, installation pins and documentation only; all 52 locked dependency records remain identical.
- The maintainer explicitly authorized the existing broadcast test channel and private test bot, including synthetic sends without a form. No real outreach, account-wide reads, catalog, callbacks or draft modifications were included.
- A disposable MCP client exercised real STDIO servers and Telegram RPCs. Read permissions remained empty. Application state and the synthetic file were isolated under an ignored private validation directory; the existing session remained under its normal lease. The normal systemd service was stopped for the probe and restored afterward. Its configuration was not edited.
- Required one-off MCP forms were shown to the maintainer through Codex questions. Their explicit decline/accept responses were relayed to the awaiting form. This checks protocol behavior with human decisions, not native Codex, Claude or Cursor form rendering for this release.

## Selected-source live results

| Check | Observed result |
| --- | --- |
| Fresh status | Authorized and connected; version 0.8.0, process start time, direct-connection `connect=null` |
| Recipient preview | Username and canonical ID resolved to the same selected channel, without history access |
| All-chat policy, forms disabled | One synthetic text message and one 46-byte file with a caption sent without an elicitation-capable client |
| Exact send verification | Both receipts reread their exact outgoing message; text and attachment metadata matched; attachment byte integrity was explicitly not claimed |
| Read-policy separation | Ordinary selected-channel history remained denied despite successful sends |
| One-off policy | The selected private bot required a form even with the ordinary message-confirmation switch disabled; no recipient was added to configuration |
| Human refusal | `CONFIRMATION_DECLINED`; the declined message was not dispatched |
| Human acceptance | Exactly one approved synthetic bot message sent; exact-message verification passed |
| Restart and revocation | Three successful receipts recovered by idempotency key from a new server after recipient grants were removed; retries returned the same receipts, and exact-message verification remained available |
| Journal queries | Three successful operations enumerated over one-item pages without duplicates; recipient, kind and status filtering matched the synthetic file operation |
| Revoked access | A new send failed with `ACCESS_DENIED` before dispatch |

Three messages in total were sent: channel text, channel file and private-bot text. No test messages were deleted. The first probe expected the final pre-dispatch denial to be a receipt instead of an MCP tool error; a focused corrected assertion confirmed the documented `ACCESS_DENIED` response without another send. This was a harness expectation, not a server change.

## Offline and artifact checks

`make check` passed: Ruff formatting/lint, ty, 2006 tests and three platform-specific skips. Coverage includes legacy and modern MCP over STDIO, authenticated HTTP and `connect`, migration of old journal records, Windows trailing-Ctrl+Z corruption and repair, recipient isolation, read revocation, lost responses and duplicate-send prevention.

The full serialized 30-message response is 32,327 bytes for 10,260 source-text characters, including text fallback and structured content. Server instructions are 967 bytes. Tool-list sizes below include the complete serialized listing (`exclude_none=True`); full schemas remain optional.

| Configuration | Tools | Compact listing | Full-schema listing |
| --- | ---: | ---: | ---: |
| Default read-only | 23 | 28,826 bytes | 125,722 bytes |
| Outbound enabled | 36 | 38,297 bytes | 152,078 bytes |

The main CI run [42061821159](https://github.com/egor-fedorov/telegram-mcp/actions/runs/42061821159) passed all supported OS/Python combinations, package builds and the aggregate `checks` job without a rerun. On clean candidate commit `1e306408a217c916ede7746ae59bd4e1e8142a8a`, isolated wheel and sdist installations passed Registry launch, CLI, STDIO, HTTP and proxy checks in compact/full schema modes, plus extraction-extra parser/worker probes. Both archives embed that exact build commit. MCPB validation passed the official manifest validator, a byte-identical rebuild and an isolated installation/proxy smoke test. Its lock contains `pyasn1==0.6.5`. The release workflow rebuilt and checked artifacts from the tagged commit listed below.

Release notes were checked for unwrapped paragraphs/list items. Disposable live state, probe scripts and the synthetic local file were removed. The normal shared service was confirmed active, authorized, connected and read-only after restoration.

## Coverage limits

Native confirmation UI behavior from the [0.7.0 validation](validation-0.7.md) remains the existing Codex evidence. This release does not establish a new native-client UI result, live Windows/macOS Telegram behavior, a forced network-failure send, username reassignment, arbitrary attachment-byte integrity, or new callback/draft coverage. Those policies and recovery cases have offline tests; the new live checks are limited to the listed recipients and operations.

No credentials, session contents, recipient IDs, message references, operation keys or real message bodies are included in this record.

## Release CI and publication verification

The release PR [#90](https://github.com/egor-fedorov/telegram-mcp/pull/90) passed the full matrix and required `checks` in [run 42068763952](https://github.com/egor-fedorov/telegram-mcp/actions/runs/42068763952). Its initial Windows/Python 3.12 attempt encountered an HTTP `ReadTimeout` in `test_shared_transports_transfer_exports_and_independent_checkpoints[connect-legacy]`; the unchanged commit passed on retry. A macOS/Python 3.12 job remained queued during the [GitHub Actions incident](https://www.githubstatus.com/incidents/t89g2q1800fr), was cancelled and later completed successfully. The final aggregate passed on attempt 3; no runtime or test-timeout changes were made for these retries.

Tag `v0.8.0` points to squash commit `87684b7d5adc05c782e61f20367580f9f9a31b95`. The [release workflow](https://github.com/egor-fedorov/telegram-mcp/actions/runs/42100673242) passed all supported OS/Python combinations and the package/bundle checks on its first attempt, then published successfully.

- [PyPI 0.8.0](https://pypi.org/project/telegram-context-mcp/0.8.0/) contains the non-yanked wheel and source distribution; both SHA-256 hashes match the corresponding GitHub Release assets.
- [GitHub Release v0.8.0](https://github.com/egor-fedorov/telegram-mcp/releases/tag/v0.8.0) contains exactly four files: wheel, source distribution, MCPB and its checksum. Release notes match the dated changelog and retain unwrapped paragraphs/list items.
- The wheel and source distribution embed the exact tag commit. The MCPB checksum matches its bytes, its version is 0.8.0, and its embedded wheel is byte-identical to the standalone published wheel. Its locked `pyasn1` is 0.6.5.
- The active [MCP Registry entry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.egor-fedorov%2Ftelegram-mcp/versions/0.8.0) matches the tag's server/package versions, repository, PyPI identifier and STDIO launch metadata. `check_registry.py --tag v0.8.0 --check-pypi --require-published` passed.
- A fresh `uvx --refresh --python 3.12 --from telegram-context-mcp==0.8.0 telegram-mcp --version` invocation returned `telegram-mcp 0.8.0`. Refresh was needed immediately after upload because the earlier cached index did not yet list the version.

Publication does not update an already running server or client proxy. Existing permission and confirmation defaults remain unchanged when users upgrade.

## Post-publication documentation check

The publication-record PR exposed a random CLI-test failure: a generated file ID began with `-`, so the space-separated `--file-id ID` invocation was parsed as a missing argument. The [maintenance guide](outbound.md#local-maintenance) and test now use `--file-id=ID`, which already works in the published 0.8.0 CLI. Deterministic fixtures cover both ordinary and leading-hyphen IDs, including preview and removal. Follow-up `make check` passed with 2007 tests and three platform-specific skips. Runtime code and published artifacts are unchanged.
