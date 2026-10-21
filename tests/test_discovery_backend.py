from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from telethon import errors, functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.domain.models import Chat, DiscoveryResult, PublicChannel, ToolError
from tests.fakes import BASE


def channel(
    channel_id: int,
    *,
    username: str | None = "public_news",
    broadcast: bool = True,
    megagroup: bool = False,
    usernames: list[types.Username] | None = None,
    participants_count: int | None = None,
    access_hash: int | None = 12345,
    minimal: bool = False,
    left: bool = False,
    banned_rights: types.ChatBannedRights | None = None,
) -> types.Channel:
    return types.Channel(
        id=channel_id,
        title=f"Channel {channel_id}",
        photo=types.ChatPhotoEmpty(),
        date=BASE,
        username=username,
        broadcast=broadcast,
        megagroup=megagroup,
        usernames=usernames,
        participants_count=participants_count,
        access_hash=access_hash,
        min=minimal,
        left=left,
        banned_rights=banned_rights,
    )


def test_discovery_models_do_not_invent_dialog_state_or_exhaustiveness() -> None:
    candidate = PublicChannel(
        id="-1000000000001", title="Channel", username="public_news", url="https://t.me/public_news"
    )
    assert candidate.participants_count is None
    assert set(candidate.model_dump()) == {"id", "title", "username", "url", "participants_count"}
    result = DiscoveryResult(channels=[candidate], returned_count=1)
    assert result.telegram_total_count is None
    assert not result.locally_truncated and not result.exhaustive
    assert "next_cursor" not in result.model_dump() and "scope" not in result.model_dump()
    with pytest.raises(ValidationError):
        DiscoveryResult.model_validate({"channels": [], "returned_count": 0, "exhaustive": True})


async def test_search_uses_peer_order_and_filters_raw_entities_without_detail_requests() -> None:
    response = types.contacts.Found(
        my_results=[types.PeerChannel(3), types.PeerUser(1), types.PeerChannel(2)],
        results=[
            types.PeerChannel(1),
            types.PeerChannel(3),
            types.PeerChannel(9),
            types.PeerChannel(5),
            types.PeerChannel(6),
            types.PeerChannel(7),
            types.PeerChat(8),
            types.PeerUser(40),
            types.PeerChannel(10),
            types.PeerChannel(11),
        ],
        chats=[
            channel(1, participants_count=12),
            channel(2, participants_count=0),
            channel(3, username=None, usernames=[types.Username("secondary", active=True)]),
            channel(5, username=None),
            channel(6, broadcast=False, megagroup=True),
            types.ChannelForbidden(id=7, access_hash=1, title="Forbidden", broadcast=True),
            types.ChatForbidden(id=8, title="Group"),
            channel(10, broadcast=False),
            channel(11, megagroup=True),
            channel(999),
        ],
        users=[types.User(id=1, username="person"), types.User(id=40, bot=True, username="bot")],
    )
    client = AsyncMock(return_value=response)
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    existing = Chat(id="17", title="Known dialog", type="private")
    backend.chat_gateway.cache[existing.id] = existing

    result = await backend.search_public_channels("news", 20)

    assert [entry.id for entry in result.channels] == [
        "-1000000000003",
        "-1000000000002",
        "-1000000000001",
        "-1000000000003",
    ]
    assert result.channels[0].username == "secondary"
    assert result.channels[0].url == "https://t.me/secondary"
    assert [entry.participants_count for entry in result.channels] == [None, 0, 12, None]
    assert result.telegram_total_count is None
    assert backend.chat_gateway.cache == {existing.id: existing}
    client.assert_awaited_once()
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert isinstance(request, functions.contacts.SearchRequest)
    assert request.q == "news" and request.limit == 20 and request.broadcasts is True
    client.get_entity.assert_not_called()
    client.get_input_entity.assert_not_called()
    client.get_dialogs.assert_not_called()
    client.get_messages.assert_not_called()


async def test_empty_search_remains_unknown_coverage() -> None:
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings,
        runtime=TelegramRuntime(
            settings, client=AsyncMock(return_value=types.contacts.Found([], [], [], []))
        ),
        chat_cache=chat_cache,
    )
    result = await backend.search_public_channels("unmatched", 1)
    assert result.channels == [] and result.telegram_total_count is None


@pytest.mark.parametrize("method", ["search", "similar"])
async def test_candidates_exclude_view_bans_but_allow_unjoined_and_send_restricted(
    method: str,
) -> None:
    entries: list[types.TypeChat] = [
        channel(1, banned_rights=types.ChatBannedRights(until_date=None, view_messages=True)),
        channel(2, left=True),
        channel(3, banned_rights=types.ChatBannedRights(until_date=None, send_messages=True)),
    ]
    response = (
        types.contacts.Found([], [types.PeerChannel(i) for i in (1, 2, 3)], entries, [])
        if method == "search"
        else types.messages.Chats(entries)
    )
    client = AsyncMock(return_value=response)
    client.get_entity = AsyncMock(return_value=channel(10))
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    result = (
        await backend.search_public_channels("news", 20)
        if method == "search"
        else await backend.get_similar_channels("@public_news")
    )
    assert [entry.id for entry in result.channels] == ["-1000000000002", "-1000000000003"]
    client.assert_awaited_once()


@pytest.mark.parametrize(
    "seed",
    [
        channel(10, left=True),
        channel(10, banned_rights=types.ChatBannedRights(until_date=None, send_messages=True)),
    ],
)
async def test_similar_allows_unjoined_and_send_restricted_seed(seed: types.Channel) -> None:
    client = AsyncMock(return_value=types.messages.Chats([]))
    client.get_entity = AsyncMock(return_value=seed)
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    result = await backend.get_similar_channels("@public_news")
    assert result.channels == []
    client.assert_awaited_once()


@pytest.mark.parametrize("with_count", [False, True])
async def test_similar_preserves_server_count_order_and_duplicates(with_count: bool) -> None:
    entries: list[types.TypeChat] = [channel(2), channel(3, username=None), channel(1), channel(2)]
    response = (
        types.messages.ChatsSlice(100, entries) if with_count else types.messages.Chats(entries)
    )
    client = AsyncMock(return_value=response)
    client.get_entity = AsyncMock(return_value=channel(10))
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )

    result = await backend.get_similar_channels("https://t.me/s/public_news")

    assert [entry.id for entry in result.channels] == [
        "-1000000000002",
        "-1000000000001",
        "-1000000000002",
    ]
    assert result.telegram_total_count == (100 if with_count else None)
    client.get_entity.assert_awaited_once_with("public_news")
    client.assert_awaited_once()
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert isinstance(request, functions.channels.GetChannelRecommendationsRequest)
    assert isinstance(request.channel, types.InputChannel)
    assert request.channel.channel_id == 10 and request.channel.access_hash == 12345
    client.get_input_entity.assert_not_called()
    client.get_dialogs.assert_not_called()
    client.get_messages.assert_not_called()
    assert backend.chat_gateway.cache == {}


@pytest.mark.parametrize("reference", ["-1000000000010", "@public_news", "t.me/public_news"])
async def test_similar_accepts_shared_references_and_alternate_public_username(
    reference: str,
) -> None:
    client = AsyncMock(return_value=types.messages.Chats([]))
    client.get_entity = AsyncMock(
        return_value=channel(
            10, username=None, usernames=[types.Username("alternate", active=True)]
        )
    )
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    result = await backend.get_similar_channels(reference)
    assert result.channels == [] and result.telegram_total_count is None
    client.get_entity.assert_awaited_once()
    client.assert_awaited_once()


@pytest.mark.parametrize(
    "seed",
    [
        types.User(id=10, username="public_news"),
        types.User(id=10, username="public_news", bot=True),
        types.ChatForbidden(id=10, title="Group"),
        types.ChannelForbidden(id=10, access_hash=1, title="Forbidden", broadcast=True),
        channel(10, username=None),
        channel(10, broadcast=False, megagroup=True),
        channel(10, megagroup=True),
        channel(10, username=None, usernames=[types.Username("inactive", active=False)]),
        channel(10, banned_rights=types.ChatBannedRights(until_date=None, view_messages=True)),
    ],
)
async def test_similar_requires_fresh_eligible_seed_before_recommendations(seed: object) -> None:
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=seed)
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    backend.chat_gateway.cache["-1000000000010"] = Chat(
        id="-1000000000010", title="Stale public metadata", type="channel", username="public_news"
    )
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await backend.get_similar_channels("-1000000000010")
    client.get_entity.assert_awaited_once_with(-1000000000010)
    client.assert_not_called()


@pytest.mark.parametrize("seed", [channel(10, access_hash=None), channel(10, minimal=True)])
async def test_similar_rejects_unavailable_input_entity(seed: types.Channel) -> None:
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=seed)
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    with pytest.raises(ToolError, match="INVALID_REFERENCE.*unavailable"):
        await backend.get_similar_channels("@public_news")
    client.assert_not_called()


async def test_similar_invalid_reference_does_not_request_personalized_recommendations() -> None:
    client = AsyncMock()
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    with pytest.raises(ToolError, match="INVALID_REFERENCE"):
        await backend.get_similar_channels("")
    client.assert_not_called()
    client.get_entity.assert_not_called()
    client.get_entity.side_effect = ValueError("private detail")
    with pytest.raises(ToolError, match="username or t.me link") as caught:
        await backend.get_similar_channels("-1000000000010")
    assert "private detail" not in str(caught.value)
    client.assert_not_called()


@pytest.mark.parametrize("error_type", [errors.QueryTooShortError, errors.SearchQueryEmptyError])
async def test_telegram_rejected_query_is_an_argument_error(error_type: type[Exception]) -> None:
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings,
        runtime=TelegramRuntime(settings, client=AsyncMock(side_effect=error_type(None))),
        chat_cache=chat_cache,
    )
    with pytest.raises(ToolError, match="INVALID_ARGUMENT.*too-short"):
        await backend.search_public_channels("a", 20)


@pytest.mark.parametrize("method", ["search", "similar"])
@pytest.mark.parametrize(
    ("failure", "code", "retry_after"),
    [
        (errors.FloodWaitError(None, capture=17), "RATE_LIMITED", 17),
        (errors.AuthKeyUnregisteredError(None), "AUTH_REQUIRED", None),
        (ConnectionError("private detail"), "CONNECTION_ERROR", None),
        (errors.RpcCallFailError(None), "TELEGRAM_UNAVAILABLE", None),
        (errors.ChannelPrivateError(None), "TELEGRAM_ERROR", None),
    ],
)
async def test_discovery_failures_are_not_disguised_as_empty_results(
    method: str, failure: Exception, code: str, retry_after: int | None
) -> None:
    client = AsyncMock(side_effect=failure)
    client.get_entity = AsyncMock(return_value=channel(10))
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    with pytest.raises(ToolError) as caught:
        if method == "search":
            await backend.search_public_channels("news", 20)
        else:
            await backend.get_similar_channels("@public_news")
    assert caught.value.code == code and caught.value.retry_after == retry_after
    assert "private detail" not in str(caught.value)
    client.assert_awaited_once()
