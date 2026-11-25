# Privacy and local storage

Unofficial Telegram Context MCP runs on your computer. Login connects to Telegram using your own API credentials and session. Shared HTTP listens on loopback and requires a local bearer token. The project operates no hosted relay or analytics service.

Your MCP client receives the results it requests, including selected chat metadata, messages, extracted text and attachments. A client can send those results to its model provider or retain them in conversation history. Check the client's privacy and retention settings. All clients connected to the same server share its permissions.

| Local data | Retention and removal |
| --- | --- |
| Configuration, Telegram session and HTTP token | Persist across restarts. Stop the server before removing them. To revoke account access, terminate the session in Telegram's Devices settings. |
| Temporary downloads, extracted text and export resources | Normally expire after 15 minutes. Copies made by a client follow that client's retention policy. |
| Permanent archives | Created on request under the configured archive directory. Remove unwanted copies and their metadata from that directory. |
| Watches and change checkpoints | Watches persist until deleted. The local checkpoint CLI supports listing and cleanup. |
| Registered files and outbound operations | Registered file bytes persist until removed. The local CLI can remove files or prune eligible payloads. Operation receipts and idempotency keys persist for recovery. |

See the [configuration guide](configuration.md), [monitoring and archives](usage.md) and [outbound maintenance commands](outbound.md#local-maintenance). Removing local files cannot recall data already delivered to a client or sent through Telegram.

Choose sources you have permission to process. Use your own or explicitly consented material for AI demonstrations. Telegram sets terms for its API and the use of platform content. [Telegram privacy policy](https://telegram.org/privacy) · [API terms](https://core.telegram.org/api/terms) · [Content licensing](https://telegram.org/tos/content-licensing)
