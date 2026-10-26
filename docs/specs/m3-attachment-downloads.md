# M3: Selected attachment downloads

Status: implemented for 0.4.0. Selected-source live checks are recorded in
[0.4 validation](../validation-0.4.md).
User-facing behavior and errors are documented in [usage](../usage.md#download-a-selected-attachment).
This is the original milestone contract. Later permanent-copy behavior is documented
in the [current guide](../usage.md#keep-selected-files).

## Capability and contract

`download_attachment(attachment_id)` uses an existing message attachment ID. The
independent `allow_attachment_downloads` permission defaults to false and is exposed
through configuration, `configure`, and `get_status`. Existing source permissions
continue to apply. A call downloads exactly one current Document or largest static
Photo representation. It never fetches whole albums, previews, forwarded origins,
external URLs, stories, or paid media. Protected and positive-TTL media are refused.

The result contains the attachment/message references, name, MIME, actual size,
SHA-256, temporary local path, opaque resource URI, and expiry. Tool text and
structured content mirror compact metadata; a separate ResourceLink enables explicit
binary reads. Resources are not enumerated. All three connection paths support reads,
and resources recheck source access/protection/identity before releasing saved bytes.
Delivered copies cannot be revoked; attachment IDs do not pin historical versions.

## Bounds and ownership

- 20 MiB per file, with actual-byte enforcement independent of metadata.
- 60-second total operation deadline; normal metadata RPC timeouts are unchanged.
- 100 MiB and 32 stored files, including in-progress reservations; no eviction.
- 15-minute lifetime, immediate read expiry, cleanup every 60 seconds and at shutdown.
- Private platformdirs cache storage isolated by canonical session path; generated
  filenames, POSIX modes or Windows ACLs, and an independent process lease.
- Crash leftovers are cleaned at the next authorized initialization. Failed deletion
  stays accounted and is retried; active files are never cleaned by another owner.

The application owns access, serialization, cooldown and lifecycle through capability
ports. Telegram downloads use public Telethon media APIs with a bounded writer.
The store owns files and quota; MCP projection/resource delivery remains an adapter.
No new dependencies, public HTTP routes, or persistent download database are introduced.

## Recovery and compatibility

A stale Telegram file reference can trigger one fresh lookup of the same message and
media identity, followed by a reset and retry within the original deadline. Known
sizes must match the completed transfer. Network failure, timeout or cancellation
cannot publish a partial file. FloodWait checkpoints prevent premature attachment
requests; no application-level sleeps or partial-download resumption are introduced.

The original 14 tools retain their behavior; the 15th tool and download-status fields
are additive. The STDIO bridge forwards resources/list, resources/templates/list and
resources/read. Binary response limits imposed by other clients remain outside the
server contract; local file access is available on the same computer.

## Validation

Offline tests cover access/protection changes, source replacement, byte limits,
reference recovery, cooldowns, deadlines, cancellation, quota, expiry, process leases,
file integrity and platform-specific permissions. Tests transfer a complete 20 MiB
synthetic file through real STDIO, native HTTP and `connect`, without an account.
Run the full `make check` and required GitHub matrix before merging. Before publication,
separately validate a user-authorized photo and document without logging their content.
