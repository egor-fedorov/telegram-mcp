# Outbound workflows

The default server remains read-only. These tools require server and proxy version
0.7.0 or later, explicit capability flags and permitted recipients. No OCR, joining channels, folder edits,
bulk sends, scheduled sends or editing/deleting sent messages are included.

The additional recipient modes and per-action confirmation settings below are
**unreleased**. They require a server built from this checkout; the published 0.7.0
package supports only the original read+write allowlist with required forms.

## Enable only the required capabilities

Edit the session-owning server's local configuration and restart it:

```toml
allowed_chat_ids = ["123456789"]
allowed_write_chat_ids = ["123456789"]
allow_drafts = true
allow_send_messages = false
allow_send_files = false
allow_bot_callbacks = false
allowed_upload_directories = ["/absolute/private/cv"]
```

By default each recipient must satisfy the ordinary read policy **and** the explicit
write IDs. A folder may supply the read grant, but does not supply a write grant.
Public-channel read exceptions do not grant writes. Telegram service notifications
(`777000`) remain inaccessible. Forum/topic writes are unsupported.

### Recipient modes and independent forms

These are the defaults; edit the session-owning server's TOML and restart it:

```toml
allow_all_write_chats = false
allow_one_off_sends = false
require_send_message_confirmation = true
require_send_file_confirmation = true
require_bot_callback_confirmation = true
require_draft_overwrite_confirmation = true
```

| Recipient mode, in priority order | Permission and confirmation |
| --- | --- |
| `allow_all_write_chats = true` | Every supported recipient, subject to Telegram rights; forms follow each action's setting |
| Recipient in `allowed_write_chat_ids` | Ordinary read access is also required; forms follow each action's setting |
| `allow_one_off_sends = true`, outside the write IDs | One message or file to a person/private bot, always with a form |
| Otherwise | No write access |

The existing action flags remain mandatory. To send text to any supported chat without
a form, enable `allow_send_messages` and `allow_all_write_chats`, then set
`require_send_message_confirmation = false`. File sending, callbacks and draft overwrites
have their own independent switches. Disabling a form allows that enabled action without
an additional MCP approval; it does not enable the action or grant recipient access.
One-off permission always needs a form, even if the corresponding switch is false.

Use an exact ID, `@username` or `https://t.me/username` as `chat_ref`. The server resolves
the peer and, when required, shows the name, username, ID, type, content and file digest
in the form. It does not add the recipient to configuration or automatically start bots.

All-chat writes apply to sends, drafts and private-bot callbacks, including groups,
channels and Saved Messages where supported. They do not join chats, enable paid sends,
support forum/topic writes or expand read access. Drafts, callbacks and `reply_to` still
require ordinary read permission before accessing existing content. Telegram attachment
imports and reuse keep their source-read checks. One-off permission applies only to sends.

Only actual TOML booleans are accepted. There are no MCP arguments, CLI switches or
environment overrides for these six policies. `configure` preserves them without prompting
to broaden access. Proxy settings never override the session-owning server's policy.

With every write flag off, the extra ten tools are absent from `tools/list`. With any
write flag on, refresh client tool discovery. Each tool still checks its own flag.
`get_status` reports `read_write`, individual flags, write IDs, active cooldown scopes
and whether the current MCP client advertises form confirmation. This is server policy,
not a guarantee that Telegram will accept a particular action.
The six policy fields report exact settings. `confirmation_required` is an aggregate:
at least one enabled action may require a form (including one-off sends). A client without
elicitation can execute only actions whose effective policy does not require a form.

## Register one reusable file

Call `register_file(path="/absolute/private/cv/resume.pdf")`, or select an
`attachments[].id` from a message and pass `attachment_id` instead. Exactly one selector
is required. Local paths must be regular files under `allowed_upload_directories`;
symlinks and junctions are refused. Telegram imports require attachment-download
permission, current source access and an unrestricted, non-authentication source.

The returned `file_id`, original name, size, MIME type and SHA-256 identify an immutable
private copy. Editing the original local file does not alter this copy. `list_files`
lists the account's accessible copies. Telegram provenance is checked again before reuse.
The library allows 20 MiB per file, 100 MiB total and 32 files, without FIFO eviction.
Copying, hashing and SQLite writes run outside the event loop. Files persist until
explicit local removal; they are independent of 15-minute download resources.

## Native drafts

- `get_draft(chat_ref)` reads the ordinary dialog's one native draft and its revision.
- `create_draft(chat_ref, text)` refuses to replace a nonempty draft.
- `update_draft(chat_ref, text, expected_revision)` replaces a matching draft.
- `list_drafts(limit, cursor?)` scans the configured write IDs, or readable sources with
  all-chat writes enabled; inspect per-chat issues. Each page reads at most `limit` drafts.

Text is plain text, at most 4096 UTF-16 units. Use an empty text in `update_draft` to clear
an existing draft. A server-owned draft can be updated without confirmation while its
native revision is unchanged. Replacing a user-created or user-modified draft follows
`require_draft_overwrite_confirmation`. Turning it off preserves revision checks, conflict
detection and the previous snapshot saved locally before replacement.

File drafts are deferred. The optional `file_id` argument remains recognized but returns
`DRAFT_ATTACHMENT_UNSUPPORTED` before any file read, upload, confirmation or draft write.
Registered files remain available for a separately confirmed `send_file`; a draft request
never falls back to sending. Native drafts containing media or other unsupported rich
content remain readable but cannot be replaced by these text-only tools.

The implementation uses Telegram's raw `messages.saveDraft` for text. A live document
draft was rejected with `MediaInvalidError`; the presence of a `media` field in the
TL schema was insufficient evidence for arbitrary file-draft support.
Draft operations serialize per peer and recheck before and after saving. Telegram has
no atomic draft compare-and-swap: another Telegram client can still edit during the
save. A detected mismatch reports `DRAFT_CONFLICT`; do not overwrite it automatically.

## Confirm and recover sends

Use `send_message(chat_ref, text, idempotency_key, reply_to?)` or
`send_file(chat_ref, file_id, caption, idempotency_key, reply_to?)`. The optional reply
must belong to the selected recipient. There is no splitting, truncation, parse mode,
automatic preview, paid-send opt-in or implicit clearing of a user's draft.

When the effective policy requires a form, the client must support MCP form elicitation
and explicitly accept the exact action. No file is uploaded before required approval.
The form identifies recipient, content, file/hash and operation. A plain tool argument
cannot approve it. Confirmation expires after 120 seconds. Legacy clients use a
back-channel; modern MCP clients use a one-use `input_required` round bound to the
action. HTTP uses sessions/SSE when writes are enabled. `connect` isolates each
confirmation-capable upstream call and routes it to its originating downstream client.
Human waits do not hold Telegram peer locks or consume all tool slots.

Elicitation is a client trust boundary: a client can implement automatic acceptance.
The server cannot prove that a human saw its UI. Use a client that shows confirmation
and does not auto-approve. Actual Codex/Claude UI support must be checked locally;
the automated tests use real MCP transports with synthetic client callbacks.

Receipts contain an operation ID, `pending`, `sent`, `failed` or `unknown`, and the exact
message reference/time when Telegram supplied them. Inspect `error` and `retry_at`.
`get_send_status(operation_id)` reads the journal without dispatching anything. Its
response is `{"result": {...}}`, where `result` is a send or callback receipt; inspect
`result.status`, `result.error` and `result.retry_at`. Direct send/callback calls return
the receipt at the top level. This envelope matches the advertised MCP union schema.

- Reuse the **same key and arguments** after an interrupted send. A different payload
  under the same key fails with `IDEMPOTENCY_CONFLICT`.
- After `sent`, a repeat returns the stored receipt, including after restart and file
  removal. After `unknown`, the same request reuses its persisted MTProto `random_id`.
- New operations bind the original selector to a numeric peer before any form or dispatch.
  Repeating that selector or ID cannot switch to a new owner of the username. Another
  selector must resolve to the same peer. The bound permission basis is rechecked before
  further dispatch; a retry cannot silently switch between recipient modes.
- One-off and all-chat send receipts remain readable for the same account after permission
  revocation; revocation blocks further sending. The journal distinguishes form approval
  from local configuration authorization. Enabling a required form later forces approval
  before continuing an operation that was never form-approved.
- Do not create a new key to recover `unknown`: it could send twice. A known `failed`
  operation remains a terminal receipt; a deliberately new attempt needs a new key.
- Respect Telegram cooldowns. They are persisted by account/peer as applicable and
  returned without sleeping in an MCP call. A retry during the wait never dispatches.

The private `account.outbound.sqlite3` beside the session holds reusable bytes, draft
snapshots, cooldowns and the journal. It is bound to the Telegram account, not a temporary
session authorization key. Keep it with the session when migrating installations.
Deleting it loses deduplication protection. Terminal message bodies are cleared while
keys, digests, random IDs and receipts remain. The journal refuses new operations at
100,000 records or 128 MiB of operation JSON; it does not evict old keys. Telegram's
[random-ID contract](https://core.telegram.org/api/updates#updatemessageid-updates) supplies
remote deduplication; network uncertainty is reported instead of guessed from message text.

## Bot buttons

Read `reply_markup.rows` and the message's `revision`, then call
`invoke_button(message_ref, button_id, expected_revision, idempotency_key)` only on
the user's instruction. Only ordinary inline callbacks in allowed **private bot dialogs**
are supported. Password, URL-auth, WebApp, payment, game, contact and location actions
are excluded. A label such as “Apply” is Telegram data, not authorization to click.

Forms follow `require_bot_callback_confirmation`. The button and revision are checked
again after any required confirmation and immediately before
the RPC. Telegram callbacks have no idempotency primitive: a timeout is `unknown` and
the same key will never invoke it again. `get_send_status` also accepts callback operation
IDs. A successful callback returns its text/alert/URL and at most 20 same-peer message
references observed during five seconds. `observation_complete=false` is always explicit;
those references are not proof of causality or a complete response thread.

## Local maintenance

Stop the session-owning server first; these commands acquire its session lease and
make no Telegram requests:

```sh
telegram-mcp files list
telegram-mcp files remove --file-id FILE_ID
telegram-mcp files remove --file-id FILE_ID --apply
telegram-mcp operations list
telegram-mcp operations prune-payloads --apply
```

Removal without `--apply` is a preview. Removing a file may prevent continuation of an
uncertain file send; retain files while operations need them. Payload cleanup preserves
all deduplication records and does not remove pending/unknown operation payloads.

## Validation before release

Offline tests cover permissions, draft races, immutable files, restart recovery, quota
refusal, Telegram request/receipt mapping, button filtering, and confirmations over
STDIO, HTTP and `connect` in legacy and modern MCP modes. Before publishing, use an
explicitly chosen test recipient/private bot to check a text draft and revision conflict,
one confirmed text send, one file+caption, duplicate-key recovery, and one callback.
Check acceptance and refusal in the intended real MCP client's UI. Never use login
notifications or real job applications as test data. The [0.7 validation record](validation-0.7.md)
records selected-channel checks in Codex, discovered failures and remaining coverage.
The new recipient/form policies have offline regression coverage, but live validation is
still pending. Before release, use explicitly selected test recipients to validate a one-off
send, a test-channel send with all-chat access, acceptance/refusal, a locally disabled form
and replay after restart. Use synthetic file contents, not real applications or CVs.

The earlier [PR #32](https://github.com/egor-fedorov/telegram-mcp/pull/32) supplied a
useful write-permission/confirmation proposal. Its documentation-only design was
adapted to the current layered server; it was not merged as an implementation.
