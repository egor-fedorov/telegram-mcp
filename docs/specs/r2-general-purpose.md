# R2 — Installation, local sharing, and release 0.2.0

Make the existing tools usable from a published package and several local clients
without competing for one Telegram session. Deliver the agreed work in one focused
PR before starting attachment downloads.

- Keep the package name `telegram-context-mcp` and command `telegram-mcp`. Prepare
  version 0.2.0, `--version`, wheel/source builds, clean-install smoke checks, and
  pinned `uv tool install`/`uvx` examples.
- Support Windows, macOS, and Linux on Python 3.12–3.15. Use a portable exclusive
  session lease, explicit UTF-8 files, and accurate POSIX-permission/Windows-ACL guidance.
- Preserve `serve` as standalone STDIO. Add explicit foreground
  `serve --transport http` on `127.0.0.1:8765/mcp`, with a configurable local port and
  a random bearer token in the user profile.
- Add `connect` as a STDIO proxy to that existing server. It reads local configuration
  and the token; it neither starts the server nor falls back to opening a session.
- Share one account, read policy, and in-memory cursor store across trusted local
  clients. Preserve tool schemas and semantics, protect shared call/cursor state,
  and keep the server alive when a client disconnects. This is not tenant isolation.
- Keep login and configuration local. Local `login`, `configure`, `catalog`, and
  `status` require the session owner to stop; MCP `get_status` diagnoses the running
  server. Configuration errors identify their source/field without printing secrets.
  Enter in `configure` preserves the current values, including `allow_all_chats`.
- Provide verified client examples for Codex, Claude Desktop, Claude Code, Cursor,
  and VS Code. Keep a short README and separate usage, configuration, troubleshooting,
  architecture, security, and release documents.
- Extend CI with platform/Python tests and distribution checks while retaining the
  required `checks` job. Review Dependabot PRs manually; its generated `dependabot/`
  branches are an explicit exception to human branch naming rules.
- Publish version tags through GitHub OIDC and a PyPI Trusted Publisher. Create the
  GitHub Release only after successful PyPI publication. See [releasing](../releasing.md)
  for the external publisher setup and exact release procedure.

Completion requires offline `make check`, the CI compatibility/package checks, and
review of the transport and credential boundaries. Actual PyPI publication additionally
requires the project name and Trusted Publisher to be configured by its owner; do not
mark publication complete from repository changes alone. Live Telegram validation is
separate and uses sources selected by the user.

The implementation is ready for release validation. Publication status is recorded by
the [Release workflow](https://github.com/egor-fedorov/telegram-mcp/actions/workflows/release.yml)
and [published releases](https://github.com/egor-fedorov/telegram-mcp/releases).
