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

The examples pin package version 0.2.0 and Python 3.12 through `uvx`. Until 0.2.0 is
published, use an absolute executable path from a [source checkout](../README.md#run-from-source),
with `args = ["connect"]` or its JSON equivalent. A persistent `uv tool install`
also provides `telegram-mcp`; you can use that executable with just the `connect` argument.
Keep the proxy and shared server on the same package version.

## Codex

Merge this table into your existing `~/.codex/config.toml`, preserving other settings:

```toml
[mcp_servers.telegram]
command = "uvx"
args = ["--python", "3.12", "--from", "telegram-context-mcp==0.2.0", "telegram-mcp", "connect"]
startup_timeout_sec = 20
tool_timeout_sec = 120
enabled_tools = [
  "get_status", "list_folders", "list_chats", "get_chat", "search_messages",
  "get_history", "get_messages", "get_album", "get_context", "get_thread",
  "search_public_channels", "get_similar_channels", "list_topics", "get_pinned_messages"
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
      "args": ["--python", "3.12", "--from", "telegram-context-mcp==0.2.0", "telegram-mcp", "connect"]
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
claude mcp add --transport stdio --scope user telegram -- uvx --python 3.12 --from telegram-context-mcp==0.2.0 telegram-mcp connect
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
      "args": ["--python", "3.12", "--from", "telegram-context-mcp==0.2.0", "telegram-mcp", "connect"]
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
      "args": ["--python", "3.12", "--from", "telegram-context-mcp==0.2.0", "telegram-mcp", "connect"]
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
   uvx --python 3.12 --from telegram-context-mcp==0.2.0 telegram-mcp connect
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
      "args": ["--python", "3.12", "--from", "telegram-context-mcp==0.2.0", "telegram-mcp", "connect"]
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
uvx --python 3.12 --from telegram-context-mcp==0.2.0 telegram-mcp serve
```

The client now starts and owns a standalone STDIO server. Do not start another
standalone server, the shared server, or local login/configuration commands against
the same Telegram session. The two modes expose the same 14 tools and read policy.

## Executable paths and custom configuration

Graphical applications may not inherit your terminal's `PATH`. Find `uvx` with
`command -v uvx` on POSIX or `where.exe uvx` on Windows, then put the absolute path
in `command`. JSON requires escaped backslashes, for example
`"C:\\Users\\you\\.local\\bin\\uvx.exe"`. Do not put the whole command line in
the `command` field; keep arguments as separate array entries.

For a custom configuration, insert `--config` and its absolute path after
`telegram-mcp` and before `connect` or `serve`:

```json
["--python", "3.12", "--from", "telegram-context-mcp==0.2.0", "telegram-mcp", "--config", "/absolute/private/config.toml", "connect"]
```

Run the HTTP server with that same configuration. All clients share its account,
scope, and cursors; changing a proxy's allowlist does not create a separate policy.
The first `uvx` launch may need to download Python or dependencies. Run the pinned
`--version` command in a terminal first if the client startup timeout is too short.
For connection and authorization errors, see [troubleshooting](troubleshooting.md).
