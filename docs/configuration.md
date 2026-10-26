# Configuration and access

[Quickstart](../README.md) · [Clients](clients.md) · [Troubleshooting](troubleshooting.md)

Configuration is local UTF-8 TOML. Start with [config.example.toml](../config.example.toml)
or let `telegram-mcp login` create it. Login prints the exact path. Default locations
come from `platformdirs` for the current operating-system user, outside the checkout.
The Telegram session defaults to `account.session` in the application's user-data
directory; the HTTP token defaults to `mcp.token` in the same directory.

## Select a configuration

Place `--config` before the command:

```sh
telegram-mcp --config /absolute/private/config.toml configure
telegram-mcp --config /absolute/private/config.toml serve --transport http
```

Each client proxy must use that same configuration:

```sh
telegram-mcp --config /absolute/private/config.toml connect
```

The path selection order is `--config`, then `TELEGRAM_MCP_CONFIG`, then the default
user configuration path. These environment variables override their TOML fields:

| Environment variable | TOML field |
| --- | --- |
| `TELEGRAM_API_ID` | `api_id` |
| `TELEGRAM_API_HASH` | `api_hash` |
| `TELEGRAM_SESSION_PATH` | `session_path` |

`session_path` and `http_token_path` expand `~` and become absolute paths. A session
path without the `.session` suffix receives it automatically. Use absolute paths
when clients may start in different working directories. In TOML, Windows paths can
use literal strings, for example `session_path = 'C:\Users\you\private\account.session'`.

The server reads its configuration on startup. Restart it after editing permissions,
port, or paths. A proxy's settings select the endpoint and token; they do not override
the server's read policy or create per-client permissions.

## Choose the permitted sources

```toml
allowed_folder_ids = [2]
allowed_chat_ids = ["-1001234567890"]
allow_all_chats = false
allow_public_channel_reads = false
allow_public_channel_discovery = false
allow_attachment_downloads = false
```

Folder IDs are integers; chat IDs are strings containing Telethon marked peer IDs.
Run `telegram-mcp catalog` while the server is stopped to inspect your account's
folder/chat metadata. The local catalog does not output message text.

| Setting | Effect |
| --- | --- |
| `allowed_folder_ids` | Permits chats selected by those Telegram folder rules |
| `allowed_chat_ids` | Adds explicitly permitted chats |
| `allow_all_chats` | Permits all account chats instead of restricting reads to the allowlist |
| `allow_public_channel_reads` | Additionally permits an explicitly selected public broadcast channel |
| `allow_public_channel_discovery` | Enables public-channel search and recommendations; grants no post-reading permission |
| `allow_attachment_downloads` | Enables selected downloads from already permitted sources; default `false` (unreleased) |

All four flags default to `false`; both allowlists default to empty. `configure`
prompts separately for each flag, including `allow_all_chats`. Enter keeps the existing
value. Enter also preserves existing folder/chat lists; to clear a list, set it to
`[]` in the TOML file. Choose `n` explicitly to turn off an enabled flag.

Public-channel access is checked against fresh metadata: the source must be a broadcast
channel with a primary or active additional username. It does not permit private dialogs,
groups, or linked discussion groups. It applies only to explicit references or `chat_ids`
within the request; it does not expand `list_chats` or searches without a selector.
Discovery remains independent even when `allow_all_chats` is enabled.

Folders form a union; explicit `chat_ids` intersect with selected folders. Query filters
cannot expand server permissions. Comments and parent replies from another chat require
permission for that other chat. No operation automatically joins a source.

## M4 local state (unreleased)

Change tracking and text export use ordinary read permissions; they do not enable
attachment downloads or add source grants. Source builds store checkpoint metadata
under `platformdirs.user_data_path("telegram-mcp")/checkpoints/<session-path-hash>/`
and temporary exports under the corresponding user-cache `exports/<session-path-hash>/`
directory. These are outside the checkout and protected with private POSIX modes or
Windows ACLs. No new TOML setting is required.

Checkpoint state contains canonical IDs, positions, dates and fingerprints, not message
bodies. The authorization binding prevents reusing positions with a replacement login.
Keep the client token to resume; deleting its local database loses those positions.
Checkpoints expire after at most 30 days and may be evicted earlier at 4096 snapshots
or 64 MiB of serialized state. Export files expire after 15 minutes or shutdown and
have their own 100 MiB / 32-file quota. Both facilities use the existing RPC budget;
they never run background Telegram polling. See [usage](usage.md#changes-and-exports-unreleased).

## Transports and the shared session

| Command | Owner of the Telegram session | Lifetime |
| --- | --- | --- |
| `serve` or `serve --transport stdio` | This process | Managed by one MCP client |
| `serve --transport http` | This process | Foreground terminal process, stopped with Ctrl+C |
| `connect` | The existing HTTP server | Proxy managed by an MCP client |

HTTP binds to `127.0.0.1` only and serves Streamable HTTP at `/mcp`. Configure its port
and optional token-file location in TOML:

```toml
http_port = 8765
# http_token_path = "/absolute/private/mcp.token"
```

`http_port` accepts 1–65535. There is no public-bind or remote-server mode. Start the
server explicitly before connecting clients. `connect` reads the token from the local
file and sends it as bearer authentication; it does not create the token, start a daemon,
or fall back to standalone STDIO if the server is unavailable.

Every connected client belongs to the same local user and trust boundary. They share
one account, one read policy, and in-memory cursor state. A cursor is not a per-client
authorization boundary. Stopping a proxy does not stop the server; restarting the
server invalidates all cursors. Concurrent clients may wait for shared server work.

Only one process can hold the Telegram session lease. Stop the shared HTTP server or
standalone STDIO process before local `login`, `configure`, `catalog`, or `status`.
Use MCP `get_status` during normal operation. The lock works on Windows, macOS, and
Linux; removing its file while a process is running is not a way to release it safely.

## Files and credentials

The HTTP server creates a random bearer token when its token file does not exist.
It restricts that token to `0600` on POSIX and a current-user ACL on Windows. Failure
to protect or read the token prevents HTTP startup. Configuration and session files
also need a private user directory: their Windows access depends on profile or custom
directory ACLs. Unix permission bits do not replace Windows ACLs. Do not put these
files in shared folders, source control, or client JSON.

To rotate the HTTP token, stop the server and its proxies, remove the configured token
file, then start the server and reconnect clients. A new token will be generated.
This rotates local MCP access only; it does not revoke the Telegram account session.

The Telegram session contains account access keys. The read-only tools and allowlists
constrain this application, not the account credentials themselves. Login codes and
2FA passwords are entered interactively and are not saved in the TOML file.

## Retrieval budgets

| Field | Default | Accepted values |
| --- | --- | --- |
| `rpc_timeout_seconds` | `20` | Greater than 0, at most 60 |
| `max_dialogs` | `10000` | 1–100000 |
| `catalog_cache_ttl_seconds` | `30` | 0–30 seconds; 0 disables completed-snapshot reuse |
| `rpc_pages_per_call` | `12` | 1–100 |
| `cursor_ttl_seconds` | `900` | Positive integer |
| `max_cursors` | `128` | Positive integer |

Cursors additionally share a fixed 64 MiB serialized-data budget. They are process-local
and may expire or be evicted. Increasing a timeout or page budget does not bypass
Telegram's FloodWait. See [usage](usage.md#retrieval-rules) for continuation and text budgets.

## Catalog freshness and recovery (0.3)

A completed catalog is reused for at most `catalog_cache_ttl_seconds`. Every request
still checks current local policy and authorization; explicit public-channel grants
use fresh Telegram metadata. Folder-derived access can reflect cached membership until
refresh. Telegram pagination is not an atomic snapshot, so early-page unread, mute,
and contact metadata can be older than the reuse TTL. Folder definitions are fetched
at the end of the scan.

Large catalogs load incrementally: up to five RPC steps per call, stopping before
another step after five seconds of work. Each RPC retains its configured timeout.
`CATALOG_LOADING` reports progress and a retry delay. Repeat the same call in the same
server process, including an existing cursor if supplied. No partial or expired catalog
grants access. Network errors preserve committed progress; inactive builds expire after
15 minutes. Local `catalog` and `configure` wait for this progress for up to five minutes.

Explicit source reads skip the catalog when no configured folder grant or requested
folder selector is involved. An unknown numeric entity requires a username/link;
it does not silently start a complete dialog scan. Connection loss invalidates the
completed snapshot; authorization loss also clears unfinished catalog state.

## Selected download limits (unreleased)

`allow_attachment_downloads` is the only additional setting; `configure` preserves
its current value on Enter. Fixed limits are 20 MiB per attachment, 60 seconds per
operation, 100 MiB / 32 temporary files, and a 15-minute lifetime. `get_status` exposes
`attachment_max_bytes`, `download_max_bytes`, `download_max_files`, and
`download_ttl_seconds`. See [download usage](usage.md#download-a-selected-attachment-unreleased)
for selection, storage cleanup and resource delivery. Proxy settings do not grant downloads.
