# 0.7 validation record

Status: unreleased; the selected-source checks below passed after the follow-up fixes.
Release-version and distribution checks remain pending. This record is not a
publication announcement or a claim that every client supports confirmed writes.

## Scope and environment

The maintainer selected one broadcast test channel and a synthetic 34-byte text file,
then explicitly selected one private bot's `Show Details` callback for the remaining
checks. No account-wide search, real outreach or real login notices were included.
Telegram credentials, message bodies and operation keys are not recorded here.

- Initial implementation: `5212de82170137e18c8c9a5cca8c975c5324cee2` (PR #75).
- Date: 2026-11-05; Arch Linux; Telethon 1.45.0, TL layer 229.
- Client: the maintainer's Codex session using the source checkout's `connect` proxy
  and shared local HTTP server. The Codex UI build was not recorded; the separately
  installed local CLI reported 0.166.0.
- The normal systemd service was stopped. A separate test configuration reused the
  existing authorized session, with read/write access limited to the selected channel.
  Drafts, text sends and file sends were enabled; callbacks initially remained disabled.
  The later callback check enabled them and added only the selected bot to the test
  configuration's read/write allowlists. The normal configuration was unchanged.

## Observed live results

| Check | Result |
| --- | --- |
| Fresh status and tool discovery | Authorized and connected; 33 tools; confirmation support advertised |
| Selected channel metadata | Correct canonical ID, joined membership and send permission |
| Immutable local file registration | Filename, size and SHA-256 matched; registration survived restart |
| Native text draft | Created and visible in the maintainer's Telegram client |
| Manual edit and stale revision | `DRAFT_CONFLICT`; the maintainer's change remained intact |
| Form acceptance | Maintainer confirmed seeing and accepting the form; one text message sent |
| Form refusal | `CONFIRMATION_DECLINED`; no new message appeared |
| File send and download | Caption and metadata preserved; downloaded SHA-256 matched the original |
| Duplicate text/file sends after restart | Original operation receipts and message references returned; no duplicates |
| Changed payload under an existing key | `IDEMPOTENCY_CONFLICT`; no dispatch |
| Draft preservation during sends | The manually edited draft remained unchanged |
| Outgoing metadata | Both test publications had `outgoing=true` |
| `get_send_status` | Failed at MCP response validation; journal replay itself worked |
| Native draft with a document | Failed with `MediaInvalidError`; no draft was created |

The first send was intended as a refusal exercise, but the maintainer accepted its
form. It is counted only as an acceptance check. A separate operation then verified
refusal. No conclusion about automatic approval is drawn from that first call.

## Follow-up changes

`get_send_status` now returns the receipt in `result`, matching the already advertised
union schema. Tests cover stored text/file/callback receipts in compact/full schema
modes and legacy/modern MCP, plus receipt retrieval through STDIO, HTTP and `connect`.
The journal format and direct send responses remain unchanged.

The maintainer chose to defer file drafts. `file_id` remains a recognized argument but
returns `DRAFT_ATTACHMENT_UNSUPPORTED` before any backend/file call or confirmation.
Text drafts remain supported; media/rich drafts are readable but cannot be overwritten
by these tools. Registered files and separately confirmed file sends remain supported.

Targeted regression tests passed (119 tests). Full `make check` passed: Ruff format/lint,
ty and 1846 tests, with two platform-specific skips. The updated skill passed its validator.

After restarting the same test server with the follow-up code, `get_send_status` returned
the existing text/file `sent` receipts and the `CONFIRMATION_DECLINED` receipt inside
`result`. File-draft create and update calls both returned `DRAFT_ATTACHMENT_UNSUPPORTED`.
The existing manually edited draft retained its exact text and revision, and the latest
channel message remained unchanged. No new sends were needed for these checks.

## Additional selected-source checks

On `c460285d231bee2eafb2f6e3caccea740aaf9112` from PR #81, a bounded search of the two synthetic send
messages returned both with `direction=outgoing` and neither with `direction=incoming`.
Both searches completed without issues.

A clearly labelled synthetic authentication notice was sent to the same test channel
through the normal confirmation path. No real credential was used. `get_messages`
returned an empty-text placeholder with `AUTH_NOTIFICATION_FILTERED`.
Both a checkpoint continuation and a time-bounded history export were saved as JSONL.
Their temporary files and permanent archive copies matched byte-for-byte, and their
SHA-256 values matched the returned metadata. Both contained the omission marker;
neither contained the synthetic notice text or code. This checks one supported notice
pattern, not arbitrary credential detection or immunity to prompt injection.

The selected bot's message exposed inline button labels, callback types, opaque IDs
and a revision. An intentionally mismatched revision returned `MESSAGE_CONFLICT`
before confirmation or dispatch. The authorized `Show Details` call then completed
through the normal confirmation path, and its bounded observation contained one new
same-dialog message. A separate read confirmed that this message replied to the
selected source. Its `Hide Details` button was inspected but not invoked.

`get_send_status` returned the callback receipt inside `result`. Repeating the same
key and arguments returned the identical receipt and observed reference; the bot's
latest message remained unchanged. The source's unchanged text/buttons retained
their revision. This live check covers revision rejection and stability; changing only
button payloads remains covered by offline tests. Callback observations still report
`observation_complete=false` and do not guarantee causality or complete bot activity.

Final status was authorized and connected with no active cooldown. The maintainer's
manually edited native draft retained its exact text and revision. Full `make check`
passed again: Ruff format/lint, ty, 1846 tests and two platform-specific skips.

## Remaining before publication

- Finish release-version, distribution, MCPB and GitHub CI checks following
  [releasing](releasing.md). Other clients' confirmation UIs remain unverified.
