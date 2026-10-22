# Migrating to the 0.3 response contract

Version 0.3 intentionally changes output JSON. Existing tool names, source arguments,
session files, configuration permissions, and transports remain usable. There is no
legacy output mode. Restart the server and refresh the client's cached tool schemas;
old process-local cursors do not survive the restart.

## Message references and shared chat metadata

Output message references are canonical strings such as `"-1001234567890:42"`, including
reply, thread, forward, saved-forward, topic, album, and issue references. Inputs still
accept Telegram links, canonical strings, and `{chat_id, message_id}` objects.

Message-bearing responses include a `chats` map keyed by canonical chat ID. Each entry
contains source title, type, and an available username once per response. Individual
messages no longer embed a complete chat. Use `get_chat` for full chat details;
`list_chats` still returns an array of full chat records.

```json
{
  "messages": [{
    "message_ref": "-1001234567890:42",
    "sent_at": "2026-10-22T10:00:00Z",
    "text": "Original post",
    "content_type": "text",
    "has_link": false,
    "text_offset": 0,
    "text_total_length": 13,
    "next_text_offset": null
  }],
  "chats": {"-1001234567890": {"title": "Example", "type": "channel"}},
  "issues": []
}
```

`list_folders` returns `{"folders": [...]}`. `get_status` and `get_chat` now advertise
typed output schemas, as do all other tools.

## Missing values and pagination

Unknown optional metadata is omitted, including unknown counters, quiz correctness,
sender information, and unavailable forwarding fields. An absent value is not zero or
false. Known zero/false values remain explicit; an empty supplied poll-result vector
remains `[]`. Empty optional entity/attachment arrays may be omitted.

Pagination controls remain explicit: `next_cursor`, `scope.complete`, result lists,
and issue lists. Text continuation always includes `text_offset`, `text_total_length`,
and `next_text_offset`. `authorized: null` still means authorization could not be
verified. Album boundary flags remain nullable when unobserved.

Both MCP `content` and `structuredContent` contain the same complete JSON. Text JSON
is minified and preserves Unicode directly. Clients should consume one representation;
showing both to a model can still duplicate context. Each tool retains a self-contained
output schema with validation; schema fields are not replaced by unrestricted objects.

## New search behavior

`has_link` is independent of `content_type`: a photo can contain a link. Use
`search_messages(query="", has_link=true, chat_ids=["..."])` for a selected link gallery.
URL/TextURL entities and Telegram webpage media establish links; the server never
crawls websites. `web_page` includes available preview metadata. Preview images/files
are not standalone message attachments.

Global search is used only with `allow_all_chats=true` and no chat/folder/sender selector,
in newest-first order. Restricted searches keep exact order and expose preparation
progress. Cursors retain their original search engine, canonical scope, and upper date.
Revoking all-chat access invalidates permission to continue an existing global search.

`CATALOG_LOADING` asks the caller to repeat the same request after its retry delay;
it does not consume an existing message cursor. See [configuration](configuration.md)
for the 30-second completed-catalog cache and [usage](usage.md) for recovery rules.
