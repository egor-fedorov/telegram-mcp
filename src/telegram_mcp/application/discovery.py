"""Opt-in public discovery without granting message access."""

from telegram_mcp.application.common import check_limit
from telegram_mcp.application.ports import DiscoveryBatch, DiscoveryPort, PolicyProvider
from telegram_mcp.domain.models import DiscoveryResult, ToolError


def discovery_result(batch: DiscoveryBatch, limit: int) -> DiscoveryResult:
    # Keep native result order and the first occurrence's metadata.
    unique = {}
    for channel in batch.channels:
        unique.setdefault(channel.id, channel)
    channels = list(unique.values())[:limit]
    return DiscoveryResult(
        channels=channels,
        returned_count=len(channels),
        telegram_total_count=batch.telegram_total_count,
        locally_truncated=len(unique) > limit,
    )


class DiscoveryService:
    def __init__(self, discovery: DiscoveryPort, policies: PolicyProvider):
        self.discovery = discovery
        self.policies = policies

    def require_discovery(self) -> None:
        if not self.policies.current().allow_public_channel_discovery:
            raise ToolError(
                "FEATURE_DISABLED",
                "Public-channel discovery requires allow_public_channel_discovery.",
            )

    async def search_public_channels(self, query: str, limit: int = 20) -> DiscoveryResult:
        self.require_discovery()
        check_limit(limit)
        query = query.strip()
        if not 1 <= len(query) <= 1000:
            raise ToolError(
                "INVALID_ARGUMENT", "query must contain 1 to 1000 characters after trimming."
            )
        return discovery_result(await self.discovery.search_public_channels(query, limit), limit)

    async def get_similar_channels(self, chat_ref: str, limit: int = 20) -> DiscoveryResult:
        self.require_discovery()
        check_limit(limit)
        chat_ref = chat_ref.strip()
        if not chat_ref:
            raise ToolError("INVALID_ARGUMENT", "chat_ref must not be empty.")
        return discovery_result(await self.discovery.get_similar_channels(chat_ref), limit)
