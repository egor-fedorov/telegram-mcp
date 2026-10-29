# M6: Selected document text extraction (0.6)

Status: implemented and validated for 0.6; not released. Selected-source live
acceptance passed on 2026-10-29.

## Goal and decisions

Extract text locally from one explicitly selected text-based PDF or DOCX attachment.
Return bounded inline text so clients do not need resource support to read it.
Start with an attachment ID, reuse an immutable temporary result for continuation,
and fail without partial output when a processing budget is exceeded.

The agreed first version excludes OCR, transcription, password input, external URLs,
arbitrary local paths, batch extraction, permanent extracted-text archives and LLM
processing. Original files can still be saved through download_attachment(save=true).
DOCX coverage is main-body paragraphs and tables, with explicit omission notices.

## Tool contract

extract_attachment_text accepts exactly one of attachment_id and extraction_id,
plus text_offset (default 0) and text_limit (default 12000, range 1–24000).
An attachment ID starts a new extraction at offset zero. An extraction ID reads a
fixed snapshot without downloading or parsing it again. Offsets count Unicode code
points. An offset equal to the total length returns an empty final slice; an offset
beyond it is invalid. Expired IDs never silently start another extraction.

The result contains extraction_id, attachment_id, canonical message_ref, original
name, detected format, source_sha256, text, text_offset, text_total_length,
next_text_offset, expires_at and issues. next_text_offset=null marks exhaustion.
PDF results also contain page_count and page_spans with one-based physical page
numbers and document-wide [start, end) Unicode offsets, clipped to the returned
text. At most 50 spans are returned; reaching that limit shortens the slice without
losing its continuation position. DOCX never claims physical page numbers.

The tool has readOnlyHint=false because it writes temporary local files;
destructiveHint=false, idempotentHint=false and openWorldHint=true. Telegram is not
modified. Extracted content is data, never instructions.

get_status adds text_extraction_available and text_extraction_formats. The tool is
always advertised. A base installation returns EXTRACTION_UNAVAILABLE with the
server-side extra installation instruction before any Telegram call.

## Access, parsing and lifetime

Require the existing attachment-download permission and current source-read policy.
Reuse download protection/TTL checks, bounded bytes and FloodWait handling. Recheck
source access and attachment identity before download, before first delivery and on
every continuation. A changed attachment is ATTACHMENT_UNAVAILABLE. Authorization
and transient network failures withhold cached output and preserve the extraction
until its normal expiry. No access expands to forwarded origins or other chats.
The post-parse check also covers changes during download; an immediate duplicate
source request between download and parsing is unnecessary.

Identify formats from bytes, using file names/MIME only as metadata. Only actual
PDF and DOCX packages qualify. Look for the complete `%PDF-` signature within the
first 1024 bytes, without stripping prefixes or changing the source digest. Parse
PDF pages with pypdf in strict mode first. On a structural `PdfReadError`, retry
from the original bytes with a fresh reader and text accumulator in tolerant mode.
Both attempts share one worker, memory limit and 30-second deadline. Encryption,
no-text results and budget failures do not trigger a retry. Successful fallback
adds DOCUMENT_RECOVERED: recovery is best effort and does not guarantee completeness.
Preserve text-layer content and physical page boundaries without promising visual
reading order, tables, or OCR. Read DOCX main-body paragraphs, visible hyperlink text and
tables in order using python-docx; traverse nested tables and avoid repeating merged
cells. Tabs separate cell positions and newlines separate paragraphs/rows.
Resolve the main part through OPC relationships using python-docx; do not require
`word/document.xml`. Non-Word main content types, including DOCM, are unsupported;
damaged XML or relationships remain invalid. Validate ZIP entry paths, duplicates,
encryption and declared/actual expansion before loading the document XML.
Headers, footers, notes, comments, text boxes and revisions are outside coverage;
detected omissions produce DOCUMENT_CONTENT_OMITTED. Embedded objects and external
relationships are never executed or fetched.

Use the optional extraction extra: pypdf>=6.21.4,<7 and python-docx>=1.2,<2.
Keep parser imports in the disposable worker. The MCPB proxy needs no extra; install
it in the environment of the session-owning HTTP/STDIO server.

| Budget | Limit |
| --- | --- |
| Input attachment | 20 MiB, existing download limit |
| PDF pages | 500 |
| Extracted text | 1,000,000 Unicode code points including separators |
| Parser execution | 30 seconds |
| Complete admitted call | 90 seconds |
| Expanded DOCX | 64 MiB total, 32 MiB per entry and 2048 ZIP entries |
| Linux worker address space | 1 GiB; stricter inherited limits remain in force |
| Nested DOCX tables | 64 levels |
| Serialized worker result | 8 MiB |
| Extraction cache | 32 results and 64 MiB, including reservations/orphans |
| Result lifetime | 900 seconds, not renewed by reads |

One extraction worker runs at a time, outside the event loop. Queue waiting counts
against the call deadline; queue exhaustion is SERVER_BUSY. Kill and reap workers on
timeout, cancellation and shutdown. Bound IPC before decoding. Do not keep the
attachment lock while parsing. Clean up only inputs owned by the current extraction.

On Linux, apply `RLIMIT_AS` before importing parsers or reading input. If setting it
fails, return EXTRACTION_FAILED without parsing. A Python MemoryError exits with a
dedicated code that the parent maps to EXTRACTION_LIMIT_EXCEEDED, including when
the input pipe closes early. Native XML allocation failures receive the same error;
unrelated signals/exits are not assumed to indicate memory exhaustion. Windows and
macOS have no OS-enforced memory cap: only the time, input, expansion and output
budgets apply there. The Linux cap measures virtual address space, not resident RAM.

Reuse private temporary-file infrastructure with capacity reservations, atomic
publication, checksums and cleanup. Do not evict unexpired results. Restart invalidates
IDs. No permanent database or general file-serving endpoint is added.

## Errors and incomplete content

| Code | Meaning |
| --- | --- |
| EXTRACTION_UNAVAILABLE | Install the optional extra in the server environment |
| DOCUMENT_UNSUPPORTED | Unsupported content; includes DOCM, OLE Office containers and legacy .doc |
| DOCUMENT_ENCRYPTED | Encrypted PDF/package; no password parameter |
| DOCUMENT_INVALID | Damaged or unparseable document |
| DOCUMENT_NO_TEXT | No text in supported content; OCR may be needed, but a scan is not inferred |
| EXTRACTION_LIMIT_EXCEEDED | Page, text, expansion, worker-output or allocation limit |
| EXTRACTION_TIMEOUT | Parser/call deadline exceeded |
| EXTRACTION_EXPIRED | Unknown, expired or previous-process extraction ID |
| EXTRACTION_STORAGE_FULL | Capacity unavailable; existing results remain intact |
| EXTRACTION_FAILED | Worker failure or invalid worker protocol, with sanitized diagnostics |

A mixed PDF returns its text with DOCUMENT_PAGES_WITHOUT_TEXT, an exact count and
at most 20 example page numbers. Empty pages may be blank or image-only. Textual
coverage does not imply complete visual understanding. DOCUMENT_RECOVERED is an
informational issue on successful tolerant PDF parsing, preserved on continuation.
Processing failures publish no partial text or continuation token. Existing
access/download errors are preserved.

## Acceptance and delivery

Cover real synthetic PDFs/DOCX files: Unicode and physical pages; blank, image-only,
mixed, encrypted and damaged documents; nested/merged tables, links and omissions.
Include recoverable xref/stream-length defects and prefixed PDFs, relocated DOCX main
parts, DOCM classification and per-entry expansion. Exercise Linux memory enforcement,
inherited limits, setup failure and allocation errors without exhausting the test host.
Verify exact reconstruction across small slices, immutable digests, expiry without
renewal, quota accounting, access revocation, replacement and transient recheck errors.
Exercise cancellation/timeouts/shutdown, bounded IPC, no orphan workers/files, and
responsiveness of other tools during parsing. Test STDIO, HTTP and connect in both
MCP modes and both schema modes; retain compact output and existing tool contracts.

Run the existing OS/Python CI matrix with the extra and a separate clean base-package
probe without it. Add opt-in live extraction checks using explicitly selected PDF
and DOCX attachments and redacted results. Before publication, record both successful
live formats and continuation. Do not silently enable downloads or perform a live
check on unselected sources.

One focused feature PR includes the specification and implementation, followed by
make check, diff review and the required checks job. Release preparation/publication
is separate from merging implementation. No permanent-state migration is needed.

## Validation record

Code revision `4355dd7` passed `make check` (Ruff, ty, 1549 tests and two platform-specific
skips), clean wheel/source installations without and with the extra, and the full
[GitHub OS/Python matrix](https://github.com/egor-fedorov/telegram-mcp/actions/runs/40849705762).

On 2026-10-29, bounded live validation used exactly two maintainer-selected Telegram
messages on Linux, Python 3.12.15, Telethon 1.45.0, pypdf 6.21.4, python-docx 1.2.0
and lxml 6.1.3. The earlier attempt stopped at `AUTH_REQUIRED`; the successful run
followed interactive reauthorization by the maintainer.

| Format | Input bytes | Extracted Unicode characters | Physical PDF pages | Issues |
| --- | --- | --- | --- | --- |
| DOCX | 10,257 | 3,538 | Not applicable | None |
| PDF | 74,381 | 6,341 | 1 | None |

Each document used one initial extraction, continuation from offset one, a replay
of the first 129 characters and an empty final slice at the total length. Checks
confirmed the selected message reference, stable source digest and expiry, and PDF
page provenance without inventing DOCX page numbers. Access/download permissions were
limited in memory to the selected sources for the probe; saved configuration was not
changed. No catalog, global search, archive write or unselected-source read was used.
Temporary inputs and snapshots were removed. The shared HTTP service was restarted
and its status confirmed `authorized=true`, `connected=true`.

The public record contains counts and outcomes only. No document content, file names,
source selectors or credentials are retained. This check does not claim manual visual
comparison, live multi-page coverage, or live encrypted/damaged-document coverage;
those parsing and pagination paths remain covered by synthetic offline tests.

The pre-release recovery and resource-limit fixes pass `make check`: Ruff, ty,
1682 tests and two platform-specific skips. The added cases use synthetic documents
and disposable workers, including a real Linux address-space limit. Repeat the two
selected live document checks on the final release candidate before publication.

The 0.6.0 release candidate repeated those checks on 2026-10-29 after the fixes;
both formats and all continuation checks passed with the same counts. See the
[release record](../validation-0.6.md) for packaging and publication status.

References: [pypdf extraction](https://pypdf.readthedocs.io/en/stable/user/extract-text.html),
[python-docx document iteration](https://python-docx.readthedocs.io/en/latest/api/document.html).
