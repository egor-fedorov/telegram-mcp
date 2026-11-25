# 0.9.0 validation

On 2026-11-25, a selected private two-person dialog verified editing plain text and both deletion modes through MCP confirmation forms. The sender reread the edit and recovered the receipts. The other participant saw the edit and retained only the message deleted on the sender's side in a separately labeled pair. Group and channel permissions and RPCs have offline coverage. [Implementation and CI](https://github.com/egor-fedorov/telegram-mcp/pull/99).

A bounded MCP search returned the user-selected message from one bot dialog with the URL authentication button metadata intact. That message also contained a link in its text or preview. Synthetic tests cover messages whose URL is present only in a button, cursor binding, source scope and date boundaries. [Search implementation](https://github.com/egor-fedorov/telegram-mcp/pull/98).

The 30-message fixture contains 10,260 text characters and weighed 32,327 bytes for the complete result. With the feature checkout, the 23 default tools took 29,093 bytes in compact mode and 126,175 bytes in full mode. With message actions enabled, the 39 tools took 40,868 and 158,880 bytes respectively.

The release workflow builds and tests the wheel, source distribution and MCPB on the supported CI matrix, including an audit of the bundle's embedded lockfile. [Release procedure](releasing.md).
