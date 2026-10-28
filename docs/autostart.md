# Shared HTTP server autostart on Linux

[Quickstart](../README.md) · [Clients](clients.md) · [Configuration](configuration.md)

The optional [systemd user unit](../contrib/systemd/telegram-mcp.service) starts one
shared HTTP server when your user manager starts, normally at login. Clients keep
using `telegram-mcp connect`. Closing a client or terminal does not stop the service.
This starts the server; it does not schedule subscription polling.

This guide requires Linux with a working user systemd manager. It does not enable
linger or arrange startup before login. Without linger, the manager may stop after
your last login session ends. macOS launchd and Windows Task Scheduler setup remain
separate work; the server itself still supports those operating systems.

## Install and enable

Run these commands as your ordinary desktop user, without `sudo`.
Stop any manually started server and standalone STDIO clients using this session.

1. Install a persistent, pinned executable:

   ```sh
   uv tool install --python 3.12 telegram-context-mcp==0.5.1
   uv tool dir --bin
   "$HOME/.local/bin/telegram-mcp" --version
   systemctl --user is-system-running
   ```

   The supplied unit expects `~/.local/bin/telegram-mcp`. If `uv tool dir --bin`
   reports another directory, use that executable below and change `ExecStart` in
   the unit before enabling it. The service executes the installed program directly;
   startup performs no package installation or upgrade and does not depend on a
   checkout or a disposable `uvx` environment.

2. Complete [login and configuration](../README.md#install-and-log-in) if needed,
   then check authorization while the service is stopped:

   ```sh
   "$HOME/.local/bin/telegram-mcp" --config "$HOME/.config/telegram-mcp/config.toml" status
   ```

   Expect `authorized: true`. The default unit uses that explicit configuration
   path. Store credentials through the local `login` command, and use absolute
   `session_path`, `http_token_path`, and `archive_directory` values in TOML. A
   systemd service does not source your interactive shell configuration. Do not put
   credentials or the bearer token in the unit, its arguments, or client settings.

3. Download the unit; no repository clone is required. If a unit already exists,
   review it before replacing your local settings:

   ```sh
   telegram_mcp_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
   install -d -m 700 "$telegram_mcp_unit_dir"
   curl --fail --location \
     https://raw.githubusercontent.com/egor-fedorov/telegram-mcp/main/contrib/systemd/telegram-mcp.service \
     --output "$telegram_mcp_unit_dir/telegram-mcp.service"
   ```

   From a checkout, copying `contrib/systemd/telegram-mcp.service` to that directory is
   equivalent. To pin the unit itself, replace `main` in the URL with a reviewed
   commit that contains it; the 0.5.1 tag predates this unit.

   Review the file before enabling it. For a custom configuration location, including
   a different `XDG_CONFIG_HOME`, change both `AssertPathExists` and the `--config`
   argument. Use the same explicit `--config` in every client proxy. Keep paths
   quoted in `ExecStart`, but leave the full `AssertPathExists` value unquoted,
   including paths containing spaces. systemd expands `%h` to your home directory,
   not `$HOME`. Literal percent signs in paths must be written as `%%`; literal
   dollar signs in `ExecStart` arguments need `$$`.

4. Verify and enable the service:

   ```sh
   systemd-analyze --user verify "$telegram_mcp_unit_dir/telegram-mcp.service"
   systemctl --user daemon-reload
   systemctl --user enable --now telegram-mcp.service
   systemctl --user status telegram-mcp.service --no-pager
   ```

   The existing loopback endpoint and bearer authentication still apply. Connect
   your MCP clients and call `get_status`; a running process alone does not prove
   Telegram authorization. The runtime connects to Telegram on demand.

## Operate and diagnose

```sh
systemctl --user status telegram-mcp.service --no-pager
journalctl --user -u telegram-mcp.service -n 50 --no-pager
systemctl --user restart telegram-mcp.service
systemctl --user stop telegram-mcp.service
systemctl --user start telegram-mcp.service
```

After a crash, systemd waits five seconds before restarting. Three starts within
one minute exhaust the start limit. An explicit stop stays stopped until you start
the service again or a subsequent user-manager startup activates the enabled unit.
After correcting a repeated startup failure:

```sh
systemctl --user reset-failed telegram-mcp.service
systemctl --user start telegram-mcp.service
```

Restart clients/proxies after a server restart. In-flight calls are not replayed;
in-memory cursors and temporary resources do not survive. Durable checkpoints,
named subscriptions, pending batches, and saved archive files remain on disk.

| Symptom | Action |
| --- | --- |
| Cannot connect to the user bus | Run from your logged-in user's session on a system with a user systemd manager; do not use `sudo`. |
| `AssertPathExists` failed | Check the unit's configuration path and complete local setup. |
| `203/EXEC` | Check the installed executable path and permissions; reinstall the pinned package if its environment was removed. |
| `INVALID_CONFIGURATION` or `LOCAL_IO_ERROR` in the journal | Fix the reported field or file permissions, then reset the failure and restart. |
| `HTTP_BIND_FAILED` | Stop the other listener or change `http_port` in the shared configuration. |
| MCP `get_status` reports `AUTH_REQUIRED` | Stop the service, run interactive `login` with the same configuration, then start it. |
| MCP `get_status` reports `SESSION_IN_USE` | Stop the other session-owning process; do not remove its lock file. |
| `authorized: null` with a connection error | Restore connectivity and retry; this does not mean authorization was revoked. |
| `SHARED_SERVER_UNAVAILABLE` | Check service status and that proxies use the same configuration, port, and token path. |

The service has no interactive stdin. For local `login`, `configure`, `catalog`, or
`status`, stop it first and start it afterward. Configuration changes also require
a restart. See [troubleshooting](troubleshooting.md) for the existing error contracts.

## Upgrade or remove

Stop the service before changing its installed environment. Install your chosen
published version with `uv tool install --python 3.12 telegram-context-mcp==VERSION`,
check the executable's `--version`, then start the service. Update pinned client
commands to that same version and reconnect them. Keep the previous version number
so the same stop/install/start sequence can roll back the executable. Package
installation does not update the separately installed unit; review unit changes
explicitly and run `daemon-reload` if you replace it.

To disable autostart and remove only the unit:

```sh
systemctl --user disable --now telegram-mcp.service
rm "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/telegram-mcp.service"
systemctl --user daemon-reload
```

This preserves the executable, configuration, session, HTTP token, checkpoints,
subscriptions, and archives. `uv tool uninstall telegram-context-mcp` additionally
removes the executable and its environment; it does not delete those external data.

## Validation

Checked on 2026-10-28 with Arch Linux, systemd 262.1, Python 3.12.15, and the
published `telegram-context-mcp==0.5.1` installed through `uv tool install`.
The fresh installation resolved MCP 2.3.0 and Telethon 1.45.0.

- `systemd-analyze --user verify` accepted the shipped unit. An isolated copy also
  worked with a configuration path containing spaces.
- Missing configuration failed its assertion. Invalid configuration exhausted
  exactly three starts; an occupied port reported `HTTP_BIND_FAILED`.
- Missing authorization and an existing session owner were reported through MCP
  as `AUTH_REQUIRED` and `SESSION_IN_USE`. The competing lease remained held.
- An isolated service recovered from `SIGKILL` with a new PID after the restart
  delay. Explicit stop stayed stopped; failure recovery needed no package install.
- Two independent `connect` subprocesses, using MCP auto and legacy modes, both
  confirmed live Telegram authorization and discovered all 22 tools. Closing one
  or both clients left the service running; concurrent starts retained one PID.
- A request for the previously selected test post returned `ACCESS_DENIED` under
  the installed configuration. Permissions were preserved; this is not a claim
  of successful live post retrieval.
- Unit removal and a pinned 0.5.1 package reinstall preserved configuration,
  session, and token bytes. After restoring the unit and explicitly restarting,
  a fresh client confirmed authorization. A change between package versions was
  not tested.
- Journal checks found neither the synthetic credential canary nor the actual
  configured API hash or bearer token. Temporary units and files were removed.

Repository `make check` passed Ruff, ty, and 1478 tests with two platform-specific
skips, using the locked development dependencies. The user service was left
enabled and active, with linger disabled.

The maintainer confirmed startup after reboot and login on 2026-10-28. Before any
manual start, `systemctl --user status telegram-mcp.service --no-pager` reported
the unit enabled and active since 18:23:40 UTC. A subsequent `get_status` call in
Claude Code returned `authorized: true`, `connected: true`, and `mode: read_only`;
the configured access policy and archive setting were unchanged. This completes
the login-startup check for
[#40](https://github.com/egor-fedorov/telegram-mcp/issues/40). Other Linux
distributions have not been validated.

References: [systemd service lifecycle](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html),
[uv tool installation](https://docs.astral.sh/uv/concepts/tools/).
