# 0.8.1 validation

On 2026-11-20 the Linux offline suite passed Ruff formatting and lint, ty and 2,026 tests, with three platform-specific skips. Synthetic tests cover filtered folders and peer lists, cursor resumption after a folder grant changes, first attachment delivery and archiving after revocation or replacement, formatted and reply drafts, file-list retry errors, and private upload paths.

The 30-message fixture contains 10,260 text characters and produces a 32,327-byte MCP response. With the complete serialized tool listing, the 23 default tools use 28,823 bytes in compact mode and 125,719 bytes in full mode. With outbound capabilities enabled, the 36 tools use 38,294 and 152,075 bytes respectively.

The selected-source live probe read one explicitly authorized photo post, downloaded the attachment, read its MCP resource, and verified the archived bytes and source metadata. All four checks passed. The probe removed its disposable files and the shared systemd service returned to active status. Folder revocation and draft conflict cases were tested offline using synthetic data.

The first macOS CI run exposed a system temporary path containing an OS-level symlink. The probe now resolves its own newly created temporary directory before creating private stores. The regression test also exercises an aliased temporary parent where symlinks are available.

## Published artifacts

The [release workflow](https://github.com/egor-fedorov/telegram-mcp/actions/runs/44770378049) passed on Linux, macOS and Windows. The MCPB rebuilt byte for byte. OSV reported no known vulnerabilities in the 40 audited packages from its embedded lockfile. The published bundle contains `opentelemetry-api` 1.45.2 and `pydantic` 2.14.2.

All four [GitHub release assets](https://github.com/egor-fedorov/telegram-mcp/releases/tag/v0.8.1) matched their SHA-256 digests. The wheel and source distribution matched their [PyPI files](https://pypi.org/project/telegram-context-mcp/0.8.1/) byte for byte and embedded the tag commit `c032ba6bb1474860d6bb463274b21afe1bad8857`. The MCPB checksum matched, its wheel matched the standalone wheel, and the release notes matched the changelog.

A fresh PyPI invocation returned `telegram-mcp 0.8.1`. The [active MCP Registry entry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.egor-fedorov%2Ftelegram-mcp/versions/0.8.1) matched the tagged manifest, including package identity, repository and STDIO launch arguments.
