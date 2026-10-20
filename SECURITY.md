# Security policy

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/egor-fedorov/telegram-mcp/security/advisories/new)
for access-policy bypasses, session or credential exposure, and other security issues.
Do not disclose vulnerability details in a public issue or pull request.

Include the affected commit or version, expected and actual behavior, and a minimal
reproduction using synthetic data. Do not attach Telegram sessions, API credentials,
login codes, phone numbers, or private message exports.

If the private reporting form is unavailable, open an issue requesting a private
reporting channel without including the vulnerability details.

## Scope and updates

The project is in early development. Security fixes target `main`; there is no
separate maintenance branch or response-time guarantee.

The server's read-only API and allowlists restrict its tools, not the permissions
of the underlying Telegram session. Keep session files private and report suspected
policy bypasses through the private channel above.
