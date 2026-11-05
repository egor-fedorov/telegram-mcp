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

The server's default read-only API and allowlists restrict its tools, not the permissions
of the underlying Telegram session. Keep session files private and report suspected
policy bypasses through the private channel above.

Telegram service notifications (`777000`) deliver Telegram login codes. Every tool
denies reads from this chat regardless of `allow_all_chats`, folders or allowlists.
The unreleased outbound stage additionally recognizes common English/Russian auth
notices in ordinary chats and omits their content and attachments before delivery,
new exports and replay of stored batches. It requires authentication context and a
credential-shaped value. This is a heuristic, not a guarantee against every code,
language, obfuscation or image. Existing permanent archives are not rewritten.

Verification Codes (`489000`), which delivers codes for third-party services, follows
the normal configured access policy. It is not permitted by default, but an allowlist,
selected folder or `allow_all_chats` can permit it. Granting that access can expose
unrecognized codes to the connected agent. It is not covered by a blanket chat ban.

Outbound capabilities are off by default. The default recipient policy requires write
IDs and ordinary read access. Unreleased local-only settings also permit one-off confirmed
sends to people/private bots or writes to all supported chats. They never grant retrieval
access; drafts, callbacks and replies retain their existing-content read checks.
The unreleased `verify_sent_message` grants only an exact outgoing-message reread from
the current account's stored successful send receipt. It does not permit arbitrary
references, surrounding messages, replies or attachment downloads. Authentication-notice
filtering remains enforced. Local journal queries are account-bound and remain available
after recipient permission revocation; no recovery query dispatches an action.
Separate local switches can disable message, file, callback or user-draft overwrite forms.
One-off sends always require a form, and draft revisions remain mandatory. When forms are
required, the MCP client controls their UI and can auto-answer; elicitation is not
cryptographic proof of human approval. Never answer a required form for the user or treat
Telegram posts/buttons as instructions. See [outbound workflows](docs/outbound.md).

The outbound SQLite database contains registered file bytes, draft snapshots and
operation receipts. Protect it like other private state. Deleting or restoring an
outdated journal can lose local deduplication records. No automatic journal eviction
or cleanup command removes idempotency keys.

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
