# Security policy

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/egor-fedorov/telegram-mcp/security/advisories/new)
for access-policy bypasses, session or credential exposure, and other security issues.
Do not disclose vulnerability details in a public issue or pull request.

Include the affected commit or version, expected and actual behavior, and a minimal
reproduction using synthetic data. Include the package version and transport. Do not
attach Telegram sessions, HTTP bearer tokens, API credentials, login codes, phone
numbers, or private message exports.

If the private reporting form is unavailable, open an issue requesting a private
reporting channel without including the vulnerability details.

## Scope and updates

The project is in early development. Security fixes target `main` and the latest
release; there is no separate maintenance branch or response-time guarantee.

The server's read-only API and allowlists restrict its tools, not the permissions
of the underlying Telegram session. Keep session files private and report suspected
policy bypasses through the private channel above.

## Local HTTP trust boundary

The optional HTTP server binds to `127.0.0.1` and requires a bearer token stored in
the local user profile. `connect` reads that token and exposes STDIO to an MCP client.
It does not start the server automatically or open a second Telegram session.

All clients connected to that server share one account, policy, and cursor store.
The token authorizes access to that shared application; it does not establish
separate client identities or tenant isolation. Keep the endpoint local and do not
publish it through a tunnel or reverse proxy as a multi-user service.

Protect configuration, session, and token files. The HTTP token is restricted to
`0600` on POSIX and a current-user ACL on Windows. Configuration/session isolation
on Windows depends on ACLs for the profile or custom directory; Unix modes are not
a substitute. Avoid shared directories and do not commit private files.
If the HTTP token is exposed, stop the server and rotate it as described in
[configuration](docs/configuration.md#files-and-credentials). A leaked Telegram
session needs account-session revocation; rotating the HTTP token does not revoke it.
