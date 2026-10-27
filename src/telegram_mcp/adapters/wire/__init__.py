"""Compact, typed MCP response contracts and lossless message projections.

Unknown optional metadata is absent. Known false/zero values and empty result sets
remain explicit. Pagination and text-continuation controls never depend on omission.
"""

from telegram_mcp.adapters.wire.attachments import DownloadResult
from telegram_mcp.adapters.wire.catalog import (
    ChatDetails,
    ChatPage,
    Folder,
    FolderResult,
    ForumTopic,
    TopicPage,
)
from telegram_mcp.adapters.wire.changes import (
    Change,
    ChangesResult,
    ExportFile,
    ExportHistoryResult,
)
from telegram_mcp.adapters.wire.common import (
    Chat,
    ChatMetadata,
    ChatSummary,
    Issue,
    SavedFile,
    Scope,
)
from telegram_mcp.adapters.wire.discovery import DiscoveryResult, PublicChannel
from telegram_mcp.adapters.wire.encoding import encode
from telegram_mcp.adapters.wire.messages import (
    AlbumResult,
    Attachment,
    ContextResult,
    Entity,
    FormattedText,
    ForwardOrigin,
    Message,
    MessageResult,
    Page,
    PollAnswerResult,
    PollData,
    PollOption,
    PollResults,
    ReactionCount,
    ReactionSummary,
    SearchProgress,
    Sender,
    TraversalBudget,
    WebPage,
)
from telegram_mcp.adapters.wire.status import Status
from telegram_mcp.adapters.wire.watches import (
    WatchAcknowledgement,
    WatchChangesResult,
    WatchDeletion,
    WatchInfo,
    WatchList,
    WatchProgress,
)

__all__ = [
    "AlbumResult",
    "Attachment",
    "Change",
    "ChangesResult",
    "Chat",
    "ChatDetails",
    "ChatMetadata",
    "ChatPage",
    "ChatSummary",
    "ContextResult",
    "DiscoveryResult",
    "DownloadResult",
    "Entity",
    "ExportFile",
    "ExportHistoryResult",
    "Folder",
    "FolderResult",
    "FormattedText",
    "ForumTopic",
    "ForwardOrigin",
    "Issue",
    "Message",
    "MessageResult",
    "Page",
    "PollAnswerResult",
    "PollData",
    "PollOption",
    "PollResults",
    "PublicChannel",
    "ReactionCount",
    "ReactionSummary",
    "SavedFile",
    "Scope",
    "SearchProgress",
    "Sender",
    "Status",
    "TopicPage",
    "TraversalBudget",
    "WatchAcknowledgement",
    "WatchChangesResult",
    "WatchDeletion",
    "WatchInfo",
    "WatchList",
    "WatchProgress",
    "WebPage",
    "encode",
]
