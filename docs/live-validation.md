# Selected-source live validation

The offline suite is the default verification path. `scripts/probe_telegram.py`
adds an opt-in smoke check against explicitly selected sources using an existing
authorized Telegram session. It is a development script, not an installed CLI
command or a CI job.

Global search is excluded from live validation. The harness also excludes dialog
catalogs, folders, discovery, similar channels, linked discussions, and reply-chain
traversal. Global search behavior remains covered by offline tests.

## Run a bounded probe

Run `make check` first. Stop the process that owns the configured session before
starting the probe. The script acquires the ordinary session lease; on
`SESSION_IN_USE` it stops without disconnecting the other process, removing its
lock, or changing session ownership.

From a checkout with dependencies installed:

```sh
uv run --locked python scripts/probe_telegram.py --help
uv run --locked python scripts/probe_telegram.py \
  --chat @selected_source \
  --chat https://t.me/selected_channel \
  --message-ref https://t.me/selected_channel/123
```

Replace the examples with sources explicitly authorized for the check. Select
one to three chats with repeated `--chat`; IDs, exact usernames and channel links
use the normal resolver. An unknown numeric ID may require its username or link.
Repeat `--message-ref` up to three times. Each message must belong to an explicitly
selected chat. The script resolves all selectors and rejects mismatches before
reading any message content. Comment links are rejected because they refer to a
linked discussion.

`--config PATH` chooses an existing local configuration. `--query TEXT` changes
the short keyword used for keyword and media searches; its default is `link` and
its maximum length is 64 characters. `--topics` additionally checks selected
forum supergroups. There is no global-search option.

The script clones settings in memory, replaces the allowlist with the selected
canonical IDs, and disables folder grants, unrestricted access, public-channel
read exceptions, and discovery. It does not save configuration changes. The
selected sources may therefore be checked without adding them to the permanent
allowlist. Normal session and lease files are still used by the Telegram client.

## Bounds and checks

| Check | Maximum work |
| --- | --- |
| History | Two pages of five messages per selected chat |
| Keyword search | One page of five messages per selected chat |
| Link-only search | Two pages of five messages per selected chat, with an empty query and `has_link=true` |
| Photo, voice and audio search | One page of five messages for each type per selected chat |
| Explicit messages | Three references in total |
| Context | Anchor, two messages before and two after each available reference; no reply-chain traversal |
| Album | Only when an explicit message has an album ID; five history slots in each direction, at most eleven members including the anchor |
| Optional forums | Three topics per selected forum, then five messages from its first returned topic |
| Reconnect | Disconnect the probe's own client, then check status and five history messages in the same process |

Each paged application call has a one-page backend budget. Empty pages may carry
a cursor; link-only search can use its first page to initialize the Telegram
search offset. The probe follows at most two cursors for each history/link check
and does not attempt to exhaust a source. The bounds above describe application
pages and requested messages, not a promise of one total network request: entity
resolution, authorization checks, and the configured bounded Telethon retries
also perform RPCs.

The probe has a 180-second overall work timeout, followed by normal client
cleanup under its RPC timeout. It stops on authorization, session-ownership,
network, server-availability, or rate-limit failures. It does not sleep through a
FloodWait or automatically rerun the probe. Respect the server's wait before a
later manual run; the report intentionally omits error details.

The reconnect check retains the same runtime and session lease while its own
client is disconnected. The next status call reconnects, and the subsequent
history read checks that this process can continue. It does not revoke
authorization or disconnect any other client.

No check sends messages, joins chats, marks messages read, downloads attachments,
or changes Telegram data. Message text and metadata are held only long enough to
exercise the normal service and count its results; no report or temporary file
is written.

## Read the report

Standard output contains one JSON object per check:

```json
{"check":"history_0_0","count":5,"issue_codes":[]}
```

Check names use numeric selector positions. Reports contain only the check name,
result count, and known issue codes. They omit message text, chat titles, source
references, cursor tokens, credentials, and raw exception details. Third-party
logging is disabled in this dedicated script process. Exit status is `0` when
all executed checks have no issues, `1` when a check or setup reports an issue,
and `130` after interruption.

An empty result can be valid: the chosen query or content type may have no
matches. Successful execution verifies the exercised paths, not exhaustive
coverage, album completeness, or that every optional check ran. Inspect the
reported check names when recording validation results. Do not describe global
search, unselected sources, or authorization revocation as live-tested by this
harness.

`tests/integration/test_live_probe.py` verifies bounds, source rejection, output redaction,
failure handling, and reconnect ownership using fake backends without a Telegram
account.
