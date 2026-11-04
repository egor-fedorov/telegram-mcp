"""Retry selected read RPCs once; mutations and reconnects never replay implicitly."""

import asyncio

from telethon import TelegramClient, errors, functions

# Explicit request types prevent a future Get* mutation from becoming retryable.
READ_REQUESTS = (
    functions.updates.GetStateRequest,
    functions.users.GetUsersRequest,
    functions.users.GetFullUserRequest,
    functions.channels.GetChannelsRequest,
    functions.channels.GetFullChannelRequest,
    functions.channels.GetMessagesRequest,
    functions.channels.GetChannelRecommendationsRequest,
    functions.contacts.ResolveUsernameRequest,
    functions.contacts.SearchRequest,
    functions.account.GetNotifySettingsRequest,
    functions.messages.GetDialogFiltersRequest,
    functions.messages.GetDialogsRequest,
    functions.messages.GetPinnedDialogsRequest,
    functions.messages.GetChatsRequest,
    functions.messages.GetFullChatRequest,
    functions.messages.GetHistoryRequest,
    functions.messages.GetMessagesRequest,
    functions.messages.GetRepliesRequest,
    functions.messages.GetDiscussionMessageRequest,
    functions.messages.GetPeerDialogsRequest,
    functions.messages.SearchRequest,
    functions.messages.SearchGlobalRequest,
    functions.messages.GetForumTopicsRequest,
    functions.messages.GetForumTopicsByIDRequest,
    functions.upload.GetFileRequest,
)


class SafeTelegramClient(TelegramClient):
    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        try:
            return await super().__call__(
                request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
            )
        except (errors.ServerError, errors.TimedOutError):
            if not isinstance(request, READ_REQUESTS):
                raise
            await asyncio.sleep(1)
            return await super().__call__(
                request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
            )
