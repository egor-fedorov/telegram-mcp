# 0.6.0 validation

Release target: 0.6.0. PyPI, GitHub Release and MCP Registry publication are pending.

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
