# MCP client setup

Complete [login and configuration](../README.md#install-and-log-in), then start the
shared server in a terminal:

```sh
telegram-mcp serve --transport http
```

Keep that terminal open. Each client below launches a STDIO `connect` proxy to
`http://127.0.0.1:8765/mcp`. The proxy reads the bearer token from your local user
profile; no Telegram credentials or HTTP token belong in these client configurations.
It never starts the HTTP server automatically.

The examples target package version 0.4.0 and Python 3.12 through `uvx`. Check
[PyPI](https://pypi.org/project/telegram-context-mcp/) for published versions. For an
unreleased revision, use an absolute executable path from a [source checkout](../README.md#run-from-source),
with `args = ["connect"]` or its JSON equivalent. A persistent `uv tool install`
also provides `telegram-mcp`; you can use that executable with just the `connect` argument.
Keep the proxy and shared server on the same package version. When upgrading from
0.3, restart the server and refresh the client's tool schemas: the
[0.4 contract](migration-0.4.md) changes scope previews and search defaults. For older
installations, also review the [0.3 response migration](migration-0.3.md).

Version 0.4 adds downloads, exports and named subscriptions. If you keep an
explicit `enabled_tools` list, add `download_attachment`, `get_changes`, `export_history`,
`create_watch`, `list_watches`, `get_watch_changes`, `ack_watch_changes`, and `delete_watch`
to use them. See the [0.4 migration guide](migration-0.4.md) before upgrading.

## Codex

Merge this table into your existing `~/.codex/config.toml`, preserving other settings:

```toml
[mcp_servers.telegram]
command = "uvx"
args = ["--python", "3.12", "--from", "telegram-context-mcp==0.4.0", "telegram-mcp", "connect"]
startup_timeout_sec = 20
tool_timeout_sec = 120
enabled_tools = [
  "get_status", "list_folders", "list_chats", "get_chat", "search_messages",
  "get_history", "get_messages", "get_album", "get_context", "get_thread",
  "search_public_channels", "get_similar_channels", "list_topics", "get_pinned_messages",
  "download_attachment", "get_changes", "export_history", "create_watch", "list_watches",
  "get_watch_changes", "ack_watch_changes", "delete_watch"
]
```

Codex uses `[mcp_servers.NAME]` for STDIO command/argument configuration. Check the
server in Codex's MCP settings and call `get_status`. See the
[official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## Claude Desktop

Open Settings → Developer → Edit Config and merge this `mcpServers` entry into
`claude_desktop_config.json`. Restart Claude Desktop after saving.

```json
{
  "mcpServers": {
    "telegram": {
      "command": "uvx",
      "args": ["--python", "3.12", "--from", "telegram-context-mcp==0.4.0", "telegram-mcp", "connect"]
    }
  }
}
```

Use the file opened by the app; the standard locations include
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and
`%APPDATA%\Claude\claude_desktop_config.json` on Windows. See the
[official local-server setup guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## Claude Code

Add the proxy in user scope so it is available across your projects:

```sh
claude mcp add --transport stdio --scope user telegram -- uvx --python 3.12 --from telegram-context-mcp==0.4.0 telegram-mcp connect
claude mcp get telegram
```

The `--` separates Claude's options from the proxy command. In an interactive session,
use `/mcp` to inspect the connection. If you prefer a project-specific `.mcp.json`,
the configuration is:

```json
{
  "mcpServers": {
    "telegram": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--python", "3.12", "--from", "telegram-context-mcp==0.4.0", "telegram-mcp", "connect"]
    }
  }
}
```

Choose one configuration scope for this server. See
[Claude Code's MCP documentation](https://code.claude.com/docs/en/mcp) for scope and
project-configuration approval behavior.

## Cursor

Merge the following into `~/.cursor/mcp.json` for personal use across projects.
Cursor also supports project-local `.cursor/mcp.json`.

```json
{
  "mcpServers": {
    "telegram": {
      "command": "uvx",
      "args": ["--python", "3.12", "--from", "telegram-context-mcp==0.4.0", "telegram-mcp", "connect"]
    }
  }
}
```

Enable the server in Cursor's MCP settings, then call `get_status`. See the
[official Cursor MCP documentation](https://cursor.com/docs/mcp).

## VS Code

Use the current guided setup for the selected agent harness:

1. Run **MCP: Add Server** from the Command Palette.
2. Choose **Command (stdio)** and enter:

   ```sh
   uvx --python 3.12 --from telegram-context-mcp==0.4.0 telegram-mcp connect
   ```

3. Name the server `telegram`. For a personal Copilot setup, choose **Global**, then
   **Copilot Global**. This stores portable configuration in
   `~/.copilot/mcp-config.json`, or under `$COPILOT_HOME` when configured.
4. Review the trust prompt and verify a `get_status` call.

The portable format has a top-level `mcpServers` object, like the Claude Code example
above. Harnesses can have different configuration destinations; use the location
chosen by the guided flow. See [Add MCP servers](https://code.visualstudio.com/docs/agent-customization/mcp-servers).

### Local-harness compatibility

If your setup uses VS Code's user-profile `mcp.json` or workspace `.vscode/mcp.json`,
use its `servers` format instead. It is distinct from the portable Copilot format:

```json
{
  "servers": {
    "telegram": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--python", "3.12", "--from", "telegram-context-mcp==0.4.0", "telegram-mcp", "connect"]
    }
  }
}
```

See the [MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)
for which format applies to your harness. Do not copy a `servers` block into a file
that expects `mcpServers`.

## Standalone alternative

For one client, stop the shared HTTP server and change the final `connect` argument
to `serve` in that client's configuration. The complete command becomes:

```sh
uvx --python 3.12 --from telegram-context-mcp==0.4.0 telegram-mcp serve
```

The client now starts and owns a standalone STDIO server. Do not start another
standalone server, the shared server, or local login/configuration commands against
the same Telegram session. Both modes expose the same tools and source permissions.

## Reading files and resources

`download_attachment` and export tools return an exact `resource_uri` plus a resource
link. A link does not transfer the file: the client must issue `resources/read` for
that URI. This server intentionally keeps `resources/list` empty rather than listing
private files. A client that only browses the resource catalog may therefore not offer
the returned file in its picker.

Official client documentation checked on 2026-10-26 establishes the following general
capabilities. It does **not** establish successful binary delivery from this project:

| Client | Documented resource support | Project verification |
| --- | --- | --- |
| Codex | The [App Server API](https://learn.chatgpt.com/docs/app-server) has `mcpServer/resource/read` for an exact URI. Availability in a particular host UI is separate. | Client UI not tested |
| Claude Code | [MCP resources](https://code.claude.com/docs/en/mcp) can be referenced with `@server:protocol://resource/path`; automatic handling of returned file links is not established here. | Client UI not tested |
| Cursor | The [MCP capability table](https://cursor.com/docs/mcp) lists resource support; exact-URI file-link handling needs a client check. | Client UI not tested |
| VS Code | [MCP Resources](https://code.visualstudio.com/docs/agent-customization/mcp-servers) can be attached via Add Context or browsed using `MCP: Browse Resources`. Catalog-only discovery is insufficient for this server. | Client UI not tested |
| Claude Desktop | The [local-server setup guide](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop) does not establish this exact-URI/binary workflow. | Resource delivery unverified |

Offline integration tests use the Python MCP SDK and verify text and binary resources
over STDIO, HTTP and `connect`, including the 20 MiB limit. They do not verify the apps
above, their MIME support, or their file-size limits. Do not assume every listed client
can attach a 20 MiB resource to a conversation.

If resource reading is unavailable, configure `archive_directory` and request
`save=true`. Open or attach `saved_file.local_path` using the client's local-file
capabilities. That path belongs to the server's computer; container/remote clients
need access to that same filesystem. Permanent copies survive shutdown, while the
ordinary `local_path` and resource expire after 15 minutes or server shutdown.
See [permanent copies](usage.md#keep-selected-files).

The server defaults to `mcp_schema_mode="compact"`: input schemas stay visible,
optional output schemas are omitted, and typed server-side validation stays enabled.
Set `"full"` in the server configuration if your client needs advertised output schemas.
Refresh the client's tools after restarting the server. Both modes return identical JSON.

## Prompts and completion

*(Unreleased.)* The server offers two user-selected prompts, `watch_digest` and
`period_digest`, and completion for `watch_digest`'s `watch_name` from local
subscription storage. `connect` forwards both and advertises them only when the shared
server supports them. Clients decide how prompts are shown; documentation for
[Claude Code](https://code.claude.com/docs/en/mcp) and
[VS Code](https://code.visualstudio.com/docs/agent-customization/mcp-servers) describes
MCP prompts as chat commands. This project has not verified prompt or completion UI in
any client. Offline tests use the Python MCP SDK over STDIO, HTTP and `connect`.

Without prompt support, use the
[copyable recipes](usage.md#monitoring-prompts-unreleased): the same tool calls, budgets
and trust rules typed as an ordinary request. Prompts do not add permissions; tools,
access policy and acknowledgements behave exactly as when called directly.

## Executable paths and custom configuration

Graphical applications may not inherit your terminal's `PATH`. Find `uvx` with
`command -v uvx` on POSIX or `where.exe uvx` on Windows, then put the absolute path
in `command`. JSON requires escaped backslashes, for example
`"C:\\Users\\you\\.local\\bin\\uvx.exe"`. Do not put the whole command line in
the `command` field; keep arguments as separate array entries.

For a custom configuration, insert `--config` and its absolute path after
`telegram-mcp` and before `connect` or `serve`:

```json
["--python", "3.12", "--from", "telegram-context-mcp==0.4.0", "telegram-mcp", "--config", "/absolute/private/config.toml", "connect"]
```

Run the HTTP server with that same configuration. All clients share its account,
scope, and cursors; changing a proxy's allowlist does not create a separate policy.
The first `uvx` launch may need to download Python or dependencies. Run the pinned
`--version` command in a terminal first if the client startup timeout is too short.
For connection and authorization errors, see [troubleshooting](troubleshooting.md).
