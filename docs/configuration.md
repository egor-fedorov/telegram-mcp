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

`session_path`, `http_token_path` and `archive_directory` expand `~` and become absolute paths. A session
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
| `allow_attachment_downloads` | Enables selected downloads from already permitted sources; default `false` |

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

## M4 local state

Change tracking and text export use ordinary read permissions; they do not enable
attachment downloads or add source grants. Source builds store checkpoint metadata
under `platformdirs.user_data_path("telegram-mcp")/checkpoints/<session-path-hash>/`
and temporary exports under the corresponding user-cache `exports/<session-path-hash>/`
directory. These are outside the checkout and protected with private POSIX modes or
Windows ACLs. No new TOML setting is required.

Checkpoint state contains canonical IDs, positions, dates and fingerprints, not message
bodies. The authorization binding prevents reusing positions with a replacement login.
Keep the client token to resume; deleting its local database loses those positions.
Current checkpoint heads are protected from capacity eviction for 30 days; continuing
an unchanged head renews its expiry. Superseded tokens may be evicted under the 4096
snapshot / 64 MiB limit. If only protected positions remain, writes fail with
`CHECKPOINT_STORAGE_FULL` and preserve the input. Independent branches retain separate
heads. Expiration never silently creates a new baseline.

Named subscriptions use a separate `watches/<session-path-hash>/watches.sqlite3` in the
user-data directory. They store positions and one unacknowledged response per name,
including its message text. They have no TTL or automatic eviction; the shared limit
is 64 subscriptions / 64 MiB. A full store fails without discarding state. Names and
positions belong to the authorization, not a client; use different names for independent
consumers. See [monitoring usage](usage.md#monitor-a-folder-or-selected-sources).

Temporary exports expire after 15 minutes or shutdown, with a separate 100 MiB / 32-file
quota. These tools use the existing read budgets and never run background Telegram polling.

## Permanent copies and tool schemas

```toml
# Disabled unless a directory is configured AND the tool receives save=true.
archive_directory = "/absolute/private/telegram-archive"
mcp_schema_mode = "compact"
```

`save=true` on `download_attachment`, `export_history`, or file-output `get_changes`
creates a private, uniquely named copy under `archive_directory`. No caller-supplied
destination is accepted. The result's `saved_file` identifies its path, size, SHA-256
and save time. Copies survive expiry and shutdown and are never automatically removed.
The ordinary 20 MiB per-file limit remains; there is no total archive quota. Manage disk
space and delete unwanted copies yourself. Configuring the directory does not enable
downloads or expand source access. `get_status.archive_enabled` reports its availability.

`get_watch_changes(save=true)` *(unreleased)* uses the same directory for each pending
watch batch: one JSONL copy plus `metadata.json` naming the watch, batch and sources,
written before acknowledgement and reused on retries. See
[saving watch batches](usage.md#save-watch-batches-before-acknowledging-unreleased).

`mcp_schema_mode="compact"` omits optional output schemas from `tools/list`; `"full"`
advertises them. Both preserve input schemas, typed server-side output validation,
and identical response JSON. Restart the server and refresh client tools after changing
this setting. `configure` preserves these TOML fields; it does not prompt for them.

## Prompt time zone (unreleased)

```toml
timezone = "Europe/Moscow"  # IANA name; default "UTC"
```

The `period_digest` prompt interprets calendar dates (`YYYY-MM-DD`) as midnight in its
`timezone` argument, falling back to this setting, and shows rolling periods in that zone.
Only IANA zone names are accepted; unknown names are configuration errors. Skipped or
repeated local midnights are rejected instead of guessed. Aware timestamps keep their own
offsets. Windows installations include the `tzdata` package because Windows has no system
zone database. Restart the server after changing it; `configure` preserves the field.

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
server invalidates all cursors. All transports admit up to four tool/resource calls at
once, with a FIFO queue of 16 additional calls and a 120-second queue wait. Queue
overflow or timeout returns `SERVER_BUSY` with a five-second retry delay. Queued work
does not run after cancellation. Downloads remain serialized. Named subscriptions
serialize calls only for the same name; independent watches, checkpoints and exports
can overlap within the common admission limit.

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
| `catalog_cache_ttl_seconds` | `300` | 0–300 seconds; 0 disables completed-snapshot reuse |
| `catalog_cache_stale_seconds` | `300` | 0–300 seconds after the TTL; 0 disables serving an older snapshot during refresh |
| `rpc_pages_per_call` | `12` | 1–100 |
| `cursor_ttl_seconds` | `900` | Positive integer |
| `max_cursors` | `128` | Positive integer |

Cursors additionally share a fixed 64 MiB serialized-data budget. They are process-local
and may expire or be evicted. Increasing a timeout or page budget does not bypass
Telegram's FloodWait. See [usage](usage.md#retrieval-rules) for continuation and text budgets.

## Catalog freshness and recovery (0.4)

Each catalog-dependent call checks authorization and retrieves current folder definitions
with `GetDialogFilters`. Removing a folder, changing its categories, or explicitly
excluding a chat affects the next call, including cursor continuation. A failed check
does not release buffered messages; the original cursor remains reusable. Explicit
public-channel grants still use fresh channel metadata.

Dialog metadata has a separate cache. By default, a completed snapshot is reused for
five minutes. The first access after that starts one shared background refresh and may
serve the previous complete snapshot for up to another five minutes. Age is measured
from scan completion; Telegram pagination is not atomic. Unread, mute, archive and
contact state, and newly joined dialogs, can lag until the next completed scan. Current
folder rules are applied to that cached state. Lower the TTL/grace or set the TTL to
zero to require new scans. Existing explicit TTL settings retain their value.

A cold load or a snapshot beyond the hard age limit waits up to five seconds for the
shared worker, then returns `CATALOG_LOADING`. The worker scans in batches of up to five
RPC steps or five seconds of work before yielding; each RPC keeps its configured timeout.
It publishes only complete snapshots and stops on failure. Network errors preserve
committed progress and an age-valid snapshot. A later call can resume after any retry
deadline; there is no periodic polling or automatic delayed retry. Inactive unfinished
builds expire after 15 minutes, and shutdown cancels the worker. Local `catalog` and
`configure` wait for progress for up to five minutes.

Explicit source reads skip the catalog when no configured folder grant or requested
folder selector is involved. An unknown numeric entity requires a username/link;
it does not silently start a complete dialog scan. A normal timeout or reconnection
with unchanged authorization preserves the snapshot. Authorization loss or replacement
invalidates both completed and unfinished state. See [migration guidance](migration-0.4.md).

## Selected download limits

`allow_attachment_downloads` is the only additional setting; `configure` preserves
its current value on Enter. Fixed limits are 20 MiB per attachment, 60 seconds per
operation, 100 MiB / 32 temporary files, and a 15-minute lifetime. `get_status` exposes
`attachment_max_bytes`, `download_max_bytes`, `download_max_files`, and
`download_ttl_seconds`. See [download usage](usage.md#download-a-selected-attachment)
for selection, storage cleanup and resource delivery. Proxy settings do not grant downloads.
