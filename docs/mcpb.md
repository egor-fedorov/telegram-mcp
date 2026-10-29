# Claude Desktop bundle

[Clients](clients.md) · [Configuration](configuration.md) · [Releasing](releasing.md)

The `.mcpb` extension connects Claude Desktop to an already configured HTTP server
on the same computer. It runs `telegram-mcp connect` and shares that server's account,
permissions, tools, resources and prompts. Closing or removing the extension leaves
the HTTP server running. The extension does not start a daemon or perform Telegram login.

**Preview:** the first bundle is under validation in
[#41](https://github.com/egor-fedorov/telegram-mcp/issues/41). A successful build or
CI run is not a completed Claude Desktop installation check. Until publication, use
the reviewed PR's `mcpb` Actions artifact; published 0.5.1 predates this bundle.

One archive targets Windows, macOS and Linux through the MCPB `uv` runtime. It
contains the release wheel and locked dependencies' metadata, not an installed
Python environment. A compatible host installs Python 3.12 and dependencies on
first use; this needs network access and may take longer than subsequent starts.
The manual acceptance check is on Windows. macOS Desktop validation remains
unverified and does not block the first bundle; automated macOS checks cover the
extracted program, not the Desktop UI.

## Prepare the shared server

Complete local [installation, login and configuration](../README.md#install-and-log-in).
Keep its configuration, session, token and archive outside the extension directory,
and use absolute paths inside TOML. Match the server package version to the bundle
version. Existing setups can reuse their configuration and session.

For a new Windows setup, install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and run these commands in **PowerShell**, using the version of the selected bundle
(0.5.1 is the initial validation candidate):

```powershell
$version = "0.5.1"
$config = Join-Path $env:USERPROFILE ".config\telegram-mcp\config.toml"
uv tool install --python 3.12 "telegram-context-mcp==$version"
telegram-mcp --config "$config" login
telegram-mcp --config "$config" configure
telegram-mcp --config "$config" serve --transport http
```

If `telegram-mcp` is not on PATH, run `uv tool update-shell`, open a new PowerShell
window and repeat the remaining commands. Enter credentials and login codes only in
the local login prompt. Choose the sources you intend to read in `configure`.
Leave the server terminal open. To show the configuration path again, evaluate
`$config` in the terminal where it was assigned, before starting the server.

On macOS or Linux, the [client guide](clients.md) uses the same local CLI and
`connect` model. The optional [systemd unit](autostart.md) is Linux-only; no Windows
or macOS autostart installer is included here. A Linux service on another computer
does not serve this loopback-only Windows extension.

## Install the extension

1. Download the matching `telegram-mcp-VERSION.mcpb` and `.mcpb.sha256` from the
   reviewed PR artifact or [GitHub Release](https://github.com/egor-fedorov/telegram-mcp/releases).
   Extract the Actions artifact ZIP first. In PowerShell, compare the bundle's hash
   with the first field in the checksum file:

   ```powershell
   Get-FileHash .\telegram-mcp-0.5.1.mcpb -Algorithm SHA256
   Get-Content .\telegram-mcp-0.5.1.mcpb.sha256
   ```

2. In Claude Desktop, open **Settings → Desktop app → Manage** beside Extensions
   (older versions use **Settings → Extensions**), then **Advanced settings →
   Install Extension…**. Select the `.mcpb` file. See the
   [official custom-extension instructions](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop).
3. Select the existing `config.toml` in **Shared server configuration**. This is
   the only extension setting. Disable any duplicate manual Telegram entry in
   this client, retaining its settings for rollback.
4. Enable the extension, open a conversation and ask it to call `get_status`.
   Expect `authorized: true` and `connected: true` after local login. Confirm that
   the reported permissions match your configuration.

Custom installation does not imply inclusion in Anthropic's curated directory.
The first bundle is unsigned and includes a SHA-256 checksum alongside the artifact.

## Update, remove and diagnose

To update, disable the extension and stop the shared server. Install the selected
server package version, restart it with the same external configuration, and install
the matching new `.mcpb` through Desktop's extension settings. Reconnect and check
`get_status`. Keep the previous bundle and package version for rollback. Installation
from a downloaded file has no project-managed automatic update mechanism.

Removing the extension removes its private Python environment and launcher. Leave
the external configuration/session directory in place to preserve your Telegram
login and monitoring state. Stop or uninstall the separate shared server only when
you intend to remove it too.

| Symptom | Action |
| --- | --- |
| Unsupported manifest or `uv` runtime | Update Claude Desktop to a version supporting MCPB 0.4 `uv` extensions, or use the [manual client setup](clients.md#claude-desktop). Record the actual client version during validation. |
| `MCPB_SETUP_REQUIRED` | Select an existing absolute configuration path outside the extension directory; complete local setup first. |
| `SHARED_SERVER_UNAVAILABLE` | Start the HTTP server on this computer with the same configuration and token, then reconnect the extension. |
| `AUTH_REQUIRED` | Stop the shared server, run local `login`, then restart it. |
| `INVALID_CONFIGURATION` or `LOCAL_IO_ERROR` | Fix the indicated TOML field or file permission in the external configuration. |
| First installation fails downloading Python/dependencies | Restore network access and retry; never enter Telegram credentials to resolve a package download error. |
| Resource or prompt UI unavailable | Record the client version and limitation separately from successful protocol delivery. |

## Build and validation

From a checkout, with uv and Node.js/npx installed:

```sh
uv build --no-sources
uv run --locked python scripts/build_mcpb.py --validate
uv run --locked python scripts/build_mcpb.py --check
uv run --locked python scripts/smoke_mcpb.py dist/mcpb/telegram-mcp-0.5.1.mcpb
```

The builder derives versions from `pyproject.toml`, accepts only the matching pure
Python wheel and a fixed file list, and exports runtime constraints from `uv.lock`.
It checks the generated lock's versions, artifact hashes and portable sources.
Archive metadata is fixed; `--check` rebuilds in another temporary directory and compares exact bytes
and SHA-256. The official validator is pinned to `@anthropic-ai/mcpb@2.1.2`; review
updates to that build tool explicitly. Node.js is not part of the installed extension.
The [manifest template](../contrib/mcpb/manifest.json) uses `0.0.0` as a build placeholder.

CI validates the manifest, rebuilds the archive, and tests isolated uv installations
on Linux, Windows and macOS. Offline checks exercise both MCP modes, two concurrent
proxies, tools, prompts, completion, resource errors and binary resource delivery,
missing-server diagnostics and preservation of the external profile during removal
and reinstallation. They use synthetic state and no Telegram account. Temporary
build and probe directories are removed; only the candidate bundle and checksum remain.

Before the first bundle is published, record a Windows Desktop report:

- OS, CPU architecture, Claude Desktop version, bundle version and checksum.
- Installation without a checkout or preconfigured Python environment; correct
  configuration-file selection and successful authorized `get_status`.
- One bounded read from a maintainer-selected permitted source, prompt discovery
  and rendering, and a small resource read if the client exposes that capability.
- Disable/re-enable and close/reopen Desktop while the separate server keeps running.
- Remove/reinstall the bundle and confirm the external login, configuration and
  monitoring state remain usable. Check an actual version upgrade when both versions
  are available; distinguish this from reinstalling the same candidate.
- Stop the shared server, confirm the actionable connection error, restart it and
  reconnect. Report unsupported client UI capabilities explicitly.

Share statuses and issue codes, not credentials, tokens, session files or source
content. macOS Desktop has not been manually tested. Keep #41 open until the agreed
Windows acceptance checks are complete.

### Windows report: 2026-10-29

The maintainer tested the preview on **Windows 11 25H2** with **Claude Desktop 4.29.0**.
The installed `telegram-mcp-0.5.1.mcpb` came from
[Actions run 40758992755, artifact 12846173766](https://github.com/egor-fedorov/telegram-mcp/actions/runs/40758992755/artifacts/12846173766),
built at commit `e75e14cbef5f7749223cfceed5011f7242993de0`. The downloaded bundle's
SHA-256 matches its included checksum:
`3c9a12894196302be78ed59760a47dad3838bd0442ed72c5dc2c20c2686af54b`.

- The extension connected to the local shared server. `get_status` returned
  `authorized: true`, `connected: true`, and `mode: read_only`.
- Local login, including QR approval, succeeded. QR login used the separate local
  setup; the preview bundle predates `login --qr` and does not perform authorization.
- The catalog completed with 499 dialogs after an initial `CATALOG_LOADING` response;
  folder listing returned 10 folders. Folder names and source content are omitted.

The maintainer confirmed that this report covers login, status and the catalog only.
CPU architecture and installed server/Python versions were not reported. Bounded post
reading, prompt/resource UI, lifecycle/reinstallation and server-restart recovery
remain unverified in Desktop, as does installation without a checkout or preconfigured
Python. The reported checksum identifies this candidate only; later builds require
their own artifact identity. The PR remains a draft and #41 stays open.

Format references: [MCPB specification](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md#uv-runtime-v04),
[official uv example](https://github.com/modelcontextprotocol/mcpb/tree/main/examples/hello-world-uv).
