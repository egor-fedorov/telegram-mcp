# 0.6.0 validation

Release: 0.6.0. PyPI, GitHub Release and MCP Registry publication were verified on 2026-10-29.

## Autonomous checks

`make check` passed on Linux with Python 3.12.15: Ruff formatting/lint, ty, 1682 tests
and two platform-specific skips. Dependency pins are unchanged from the reviewed
pre-release fixes; only the project version changes in the lockfile.

Coverage includes recovered PDF xref/stream defects and prefixes, OPC-based DOCX
parts, unsupported DOCM, expansion limits, a real Linux address-space cap, Python/native
allocation errors, cancellation and worker cleanup. Access changes during download or
parsing prevent publication. STDIO, HTTP and `connect`, both MCP modes and schema modes,
preserve source hashes, physical PDF pages and recovery issues through continuation.
Skill/API drift, service-notification denial and previous state fixtures also pass.

Wheel and source-distribution installs passed outside the checkout, both without and
with the extraction extra: CLI/QR rendering, Registry launch, STDIO, HTTP, bridge,
compact/full schemas and document-worker checks. The MCPB passed the pinned official
manifest validator, byte-for-byte rebuild and isolated uv installation/proxy/profile
checks. Build/probe temporary directories were removed; generated artifacts stay in
ignored `dist/`. The release workflow repeats these checks on the tagged commit.

## Selected-document live checks

On 2026-10-29 the release candidate read exactly two previously approved messages
in one test channel, using the production service/backend and existing authorization.
No dialog catalog, global search, other source, history sweep or archive write was used.
The existing configuration already enabled attachment downloads; its bytes were unchanged.

| Format | Input bytes | Extracted characters | PDF pages | Issues |
| --- | --- | --- | --- | --- |
| DOCX | 10,257 | 3,538 | Not applicable | None |
| PDF | 74,381 | 6,341 | 1 | None |

Each format passed initial extraction, Unicode continuation from offset one, replay,
stable source SHA-256/expiry/message identity and an empty final slice. PDF physical
page references were present; DOCX did not invent pages. Temporary stores and inputs
were isolated under `dist/` and removed. The shared systemd service resumed with
`authorized=true` and `connected=true`. No document text, names, selectors or secrets
are retained in this record.

## Client and platform coverage

The [Windows Desktop report](mcpb.md#windows-report-2026-10-29) covers the earlier
identified preview artifact on Windows 11 25H2 / Claude Desktop 4.29.0: installation,
QR login, status and catalog. It does not claim manual testing of the final 0.6.0
bundle or Desktop lifecycle/resource/prompt UI. macOS Desktop remains unverified.
The [skill report](clients.md#agent-skill) covers synthetic Claude Code 2.1.312 checks.

Live extraction covers one text PDF page and one DOCX, not damaged/encrypted files,
scans or large/multipage documents; those cases use synthetic offline tests. Only
Linux workers have an OS-enforced 1 GiB address-space cap. Windows/macOS retain the
input, expansion, output and time limits without that cap.

## Publication verification

Annotated tag `v0.6.0` points to release commit
`4ebd53606fa1e84344caba33e5a49bbcb2347aaa` from [PR #73](https://github.com/egor-fedorov/telegram-mcp/pull/73).
The [release workflow](https://github.com/egor-fedorov/telegram-mcp/actions/runs/40916890623)
passed the Linux/Python 3.12–3.15 and Windows/macOS 3.12/3.15 matrix, package/MCPB checks,
PyPI publishing, GitHub Release creation and Registry publication.

- [PyPI 0.6.0](https://pypi.org/project/telegram-context-mcp/0.6.0/) has a non-yanked wheel and source distribution. Their SHA-256 values match the corresponding GitHub Release assets.
- [GitHub Release v0.6.0](https://github.com/egor-fedorov/telegram-mcp/releases/tag/v0.6.0) contains exactly four assets: wheel (194,624 bytes), sdist (520,376 bytes), MCPB (342,565 bytes) and its checksum. Notes match the tagged changelog without soft-wrapped paragraphs or list items.
- MCPB SHA-256 is `57ac418b64930defa735d01fcfee22e03f7bd897f5bced114ed56dabe26d7618`. Its manifest version is 0.6.0; the embedded wheel is byte-identical to the published wheel. Downloaded verification copies were removed.
- The [Registry entry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.egor-fedorov%2Ftelegram-mcp/versions/0.6.0) is active and matches the tag's identity, version, PyPI package and STDIO launch metadata. `check_registry.py --tag v0.6.0 --check-pypi --require-published` passed.
- A fresh PyPI invocation with the extraction extra returned `telegram-mcp 0.6.0`. This did not upgrade the maintainer's separately installed shared server.

Release notes for 0.2.0, 0.3.0, 0.4.0, 0.5.0 and 0.5.1 were also unwrapped. Their words
and release assets were preserved; no old tags or packages were replaced.
