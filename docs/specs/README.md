# Completed specifications

These documents preserve accepted delivery specifications. They describe
the implementation at each milestone; later changes are recorded in the
[changelog](../../CHANGELOG.md) and [roadmap](../../ROADMAP.md).

- [M1: Public-channel discovery](m1-public-channel-discovery.md)
- [M2: Message navigation and metadata](m2-message-navigation.md)
- [R1: Layered architecture](../architecture.md)
- [R2: Installation, local sharing, and release 0.2.0](r2-general-purpose.md)

## Historical completion records

M0 was implemented in commit `b167044`. At that checkpoint, `make check` passed:
Ruff format/check, ty, and 110 offline tests. This completion record does not include
validation against a live Telegram account.

M1 adds the two discovery tools, an independent opt-in permission, compact metadata,
and MCP error propagation with retry delays. `make check` passed with 225 offline
tests, including both MCP modes and existing retrieval contracts. Live Telegram
validation remains separate.

M2.1 adds forum topic listings and chat-wide pinned-message retrieval. `make check`
passed with 325 offline tests, including both MCP modes, native topic pagination,
access revocation, and temporary-failure recovery.

M2.2 adds optional topic selection to history and pinned-message retrieval, including
General, with fresh topic checks and preserved message membership. `make check`
passed with 490 offline tests. Live Telegram validation remains separate.

M2.3 preserves available message counters, aggregate reactions, and formatted poll
definitions/results without additional requests. `make check` passed with 546 offline
tests, including native Telegram payloads and both MCP modes.

M2.4 adds bounded retrieval of selected album members, preserving original messages
and explicitly reporting uncertain completeness. `make check` passed with 665 offline
tests. M2 is complete. R1 prepares the architecture before M3, selected attachment
downloads with size limits and client delivery. The detailed M3 contract will be
finalized before work starts. Live Telegram validation remains separate.

R1 separates domain contracts, application use cases and ports, concrete adapters, and
bootstrap wiring. `make check` passed with 684 offline tests, including the unchanged
schemas of all 14 MCP tools in both modes, layer boundaries, and lifecycle cleanup.
M3 is next; its detailed attachment-delivery contract remains to be agreed.
