"""Public-channel discovery results."""

from typing import Literal, NotRequired

from pydantic import with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.common import CONFIG, ChatID, Count


@with_config(CONFIG)
class PublicChannel(TypedDict):
    id: ChatID
    title: str
    username: str
    url: str
    participants_count: NotRequired[Count]


@with_config(CONFIG)
class DiscoveryResult(TypedDict):
    channels: list[PublicChannel]
    returned_count: Count
    telegram_total_count: NotRequired[Count]
    locally_truncated: bool
    exhaustive: Literal[False]
