# Troubleshooting

[Quickstart](../README.md) · [Configuration](configuration.md) · [Clients](clients.md)

Start with `telegram-mcp --version` to identify the installed executable. In shared
mode, leave `telegram-mcp serve --transport http` running and use the MCP `get_status`
tool for diagnosis. Stop the server before using the local `telegram-mcp status`
command, which needs its own access to the Telegram session.

## The client cannot start the command

Use an absolute executable path if a graphical client cannot find `uvx` or
`telegram-mcp`. Check the command in a terminal with `--version`, and keep the executable
and arguments in separate configuration fields. JSON Windows paths need escaped
backslashes. The first pinned `uvx` launch may need network access to install Python
and packages; complete that first launch in a terminal before connecting the client.

If PyPI reports that 0.2.0 is unavailable, check the release status. The package is
`telegram-context-mcp`, while the command is `telegram-mcp`. Before publication, use
the [source setup](../README.md#run-from-source). Installing the repository and publishing
the version to PyPI are separate steps.

## The shared server cannot be reached

`connect` does not start a server. Start `telegram-mcp serve --transport http` yourself
and keep the process running. The endpoint is `http://127.0.0.1:8765/mcp` by default.
Check that the server and proxies use the same configuration, `http_port`, and
`http_token_path`. They must run on the same local machine and user profile.

If the port is already in use, identify and stop the obsolete server or choose a
different `http_port` in TOML, then restart the server and proxies. There is no CLI
port override. WSL, containers, remote editors, and separate user accounts may have
different loopback endpoints and user directories; the local examples assume one
host environment.

A missing token usually means the HTTP server has not initialized that configuration.
Start it before the proxies. For a token mismatch, stop and reconnect proxies using
the same token file as the server. See [token rotation](configuration.md#files-and-credentials).
Do not paste the token into an issue, chat, client JSON, or command-line argument.

In the 0.4 development version, all transports allow four active tool/resource calls
and 16 FIFO waiters. `SERVER_BUSY` means the queue is full or its 120-second wait expired.
Retry after `retry_after_seconds`; it is not an authorization or session-lock error.

## `SESSION_IN_USE`

Another process owns the Telegram session. Multiple standalone MCP servers cannot
share its SQLite session file. Use one HTTP server plus `connect` proxies for multiple
clients, or stop all but one standalone client.

Local `login`, `configure`, `catalog`, and `status` also require the server to be stopped.
Use MCP `get_status` while serving. Do not delete the lock file while its owning process
is running. A leftover filename alone does not mean the operating-system lock is held.

## `AUTH_REQUIRED` and authorization status

Run `telegram-mcp login` yourself in an interactive terminal after stopping the
session-owning process. A missing or revoked Telegram session cannot be repaired by
retrying a tool or supplying login codes to the model.

`get_status` probes Telegram authorization afresh:

| `authorized` | Meaning |
| --- | --- |
| `true` | Telegram confirmed authorization |
| `false` | Authorization is missing or has been revoked |
| `null` | Verification could not finish, for example because of a network error |

Inspect `error` for the cause and `connected` for connection state. Network failure
is not evidence that the account has logged out. MCP tools remain discoverable before
login; their availability alone does not confirm authorization.

## `ACCESS_DENIED`, `FEATURE_DISABLED`, or no permitted chats

Check the server's configuration, not only the proxy's. The default allowlists are
empty. Stop the server, run `configure`, and select folders/chats or explicitly enable
`allow_all_chats`. Enter preserves existing values; edit TOML to clear a list.

Public-channel discovery and reading are separate flags. Discovery does not grant
post access; public reading does not grant access to groups or linked comments.
Restart the server after changing settings. `get_status` reports the active access
configuration. See [access policy](configuration.md#choose-the-permitted-sources).

## Configuration and local file errors

Configuration errors identify the file or environment variable and the affected field,
without echoing submitted values. Check TOML syntax, UTF-8 encoding, quoted chat IDs,
and the field's accepted range. Unknown settings are rejected. An environment override
can still supply an invalid value after the TOML field has been corrected.

For local I/O errors, check the reported path, parent directory, free space, and your
account's permissions. On Windows, check the directory ACL; `chmod 0600` does not
establish Windows access isolation. Avoid shared or synchronized directories for
sessions and tokens. On POSIX, keep private files readable only by their owner.
For `HTTP_TOKEN_ERROR`, also check that the token file supports the required permission
mode or current-user Windows ACL. The server fails rather than using an unprotected token.

## Temporary Telegram failures and cursors

For `RATE_LIMITED`, wait for `retry_after_seconds`. For `CONNECTION_ERROR` or
`TELEGRAM_UNAVAILABLE`, restore connectivity or retry later. The server does not
sleep through FloodWait inside a tool call.

Version 0.3 reconnects a disconnected Telegram client on the next call and checks
authorization again. A failed recovery imposes a five-second cooldown; FloodWait uses
Telegram's requested delay. A laptop waking from sleep should not require restarting
the server. Failed requests are not replayed automatically: continue the original cursor.

History, search, topics, and pins can return `next_cursor` with no messages after a
temporary failure or while preparing ordered output. Repeat the same selectors and
filters with that cursor. Do not replace it with a new query unless it has expired,
been evicted, or the server has restarted. A failed access/catalog check leaves the
original cursor available for another attempt; it does not authorize buffered output.

If an album scan reports a temporary issue, retry the whole `get_album` call after
the delay. Album results have no cursor and retain `completeness="unknown"` even
when both neighboring history boundaries were observed.

For `CURSOR_MISMATCH`, restore the original filters and source. If a username now
identifies a different channel, continuation deliberately does not switch channels.
For an unknown numeric source, resolve it using its username or Telegram URL first.

## `CATALOG_LOADING` or slow first use

The initial dialog scan is shared across calls in one process. Repeat the original
arguments after `retry_after_seconds`; the error message includes the loaded-dialog
count. Do not restart the server between retries. No partial catalog or saved message
buffer is returned while required access metadata is unavailable.

The 0.4 defaults reuse dialog metadata for five minutes, then allow another five minutes
while a shared worker refreshes it. Folder definitions and authorization are checked on
each dependent call. Direct reads independent of folders bypass that scan. Local
`catalog` and `configure` wait up to five minutes; `CATALOG_TIMEOUT` means that wait expired.
See [freshness settings](configuration.md#catalog-freshness-and-recovery-04-unreleased).

Restricted search defaults to `ordering="page"`, returning matches without waiting
for every source. Use `ordering="global"` only when ordering across pages is needed;
it can require many empty preparation pages. Inspect `progress.sources_started` and
continue `next_cursor` for full coverage. Unrestricted newest-first searches without
chat/folder/sender selectors retain native global search. Broader permissions are not
needed for early restricted-search results.

## Response fields changed after upgrading

Version 0.3 returns compact JSON with string message references, a shared chat map,
and omitted unknown optional metadata. The complete JSON is available in both text and
structured content. See the [0.3 migration guide](migration-0.3.md); there is no legacy
response-format switch. Source version 0.4 also limits scope ID previews and changes
the default search ordering; see its [migration guide](migration-0.4.md). Refresh client
tool schemas after restarting the server.

## Checkpoints or subscription storage

`CHECKPOINT_EXPIRED` does not create a baseline. Recover the missing date range with
`get_history` or `export_history` first; a new `get_changes(chat_ref=...)` starts at the
current head and reports `baseline_created=true`. Current heads are protected for 30
days, while superseded tokens can be evicted sooner. Preserve the latest returned token,
or use a named subscription so a later agent session can find it with `list_watches`.

`CHECKPOINT_STORAGE_FULL` and `WATCH_STORAGE_FULL` preserve the input position.
Subscription storage never evicts names or unacknowledged batches. Acknowledge processed
batches and explicitly delete unwanted subscriptions to free space. Deleting a watch
also discards its unread pending batch; recreating it is a new selection.

Repeated `get_watch_changes` responses with the same `batch_id` are intentional until
`ack_watch_changes` confirms processing. A stale or unrelated acknowledgement returns
`WATCH_BATCH_MISMATCH` and cannot advance a newer batch. `WATCH_CONFLICT` means creation
arguments differ from the existing name; use that existing subscription or another name.
Source failures preserve positions and appear in issues. If permissions are revoked,
the entire pending response is withheld; it stays available if access is restored.

## File resources and permanent copies

A `resource_link` does not itself transfer bytes. The client must read the exact returned
URI; this server does not enumerate private files in `resources/list`. Check the
[client capability table](clients.md#reading-files-and-resources-unreleased).

Temporary resources expire after 15 minutes or server shutdown. Use `save=true` when
creating a selected download/export to keep a permanent local copy. `ARCHIVE_DISABLED`
means `archive_directory` is unset; configure it and restart the server. For
`ARCHIVE_STORAGE_ERROR`, check directory permissions and disk space. Failed saving does
not consume the input checkpoint. The permanent path is on the server's machine;
clients in a different container or filesystem cannot open it automatically.

## Text appears incomplete

Check `text_total_length` and `next_text_offset`. Call `get_messages` for the same
reference with that offset to read the next slice. Offsets count Unicode code points;
Telegram entities retain their original UTF-16 coordinates. Do not treat an omitted
caption slice or unavailable message as evidence that Telegram deleted it.

## Reporting a reproducible problem

Include the package version, operating system, Python version, transport, MCP client,
sanitized error code, and a minimal synthetic reproduction. State whether the failure
also occurs in the offline tests. Keep credentials, session/token files, login codes,
phone numbers, and private messages out of reports. Use [private vulnerability reporting](../SECURITY.md)
for credential exposure or access-policy bypasses.
