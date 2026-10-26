from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.domain.models import Chat, ToolError
from tests.support.catalog import dialog_page, pinned_page
from tests.support.fakes import BASE


async def test_catalog_is_not_silently_truncated() -> None:
    settings = Settings(max_dialogs=1)
    chat_cache = {}

    def respond(request):
        if isinstance(request, functions.updates.GetStateRequest):
            return None
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        return pinned_page([1, 2])

    client = AsyncMock(side_effect=respond)
    client.is_connected = lambda: True
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    with pytest.raises(ToolError, match="CATALOG_LIMIT"):
        await backend.catalog()
    await backend.close()


async def test_saved_messages_peer_in_folder_uses_account_id() -> None:
    raw = types.DialogFilter(
        id=4,
        title=types.TextWithEntities("Personal", []),
        pinned_peers=[types.InputPeerSelf()],
        include_peers=[types.InputPeerSelf()],
        exclude_peers=[],
    )
    settings = Settings()
    chat_cache = {}

    def respond(request):
        if isinstance(request, functions.messages.GetPinnedDialogsRequest):
            return pinned_page([])
        if isinstance(request, functions.messages.GetDialogsRequest):
            return dialog_page([])
        return SimpleNamespace(filters=[raw])

    client = AsyncMock(side_effect=respond)
    client.is_connected = lambda: True
    client.get_me = AsyncMock(return_value=types.InputPeerUser(123, 456))
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    _, folders = await backend.catalog()
    assert folders[0].included_chat_ids == ["123"]
    assert folders[0].pinned_chat_ids == ["123"]
    client.get_me.assert_awaited_once_with(input_peer=True)
    await backend.close()


def channel(
    *,
    broadcast: bool = False,
    megagroup: bool = False,
    username: str | None = None,
    usernames: list[types.Username] | None = None,
) -> types.Channel:
    return types.Channel(
        id=123,
        title="News",
        photo=types.ChatPhotoEmpty(),
        date=BASE,
        broadcast=broadcast,
        megagroup=megagroup,
        username=username,
        usernames=usernames,
    )


@pytest.mark.parametrize(
    "reference",
    [
        "-1000000000123",
        "@example",
        "https://t.me/example",
        "https://t.me/s/example",
        "t.me/s/example",
    ],
)
async def test_chat_references_resolve_to_canonical_id(reference: str) -> None:
    settings = Settings()
    chat_cache = {}
    client = SimpleNamespace(
        is_connected=lambda: True,
        get_entity=AsyncMock(return_value=channel(broadcast=True, username="example")),
    )
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    resolved = await backend.resolve_chat(reference)
    assert resolved.id == "-1000000000123" and resolved.username == "example"
    await backend.resolve_chat(resolved.id)
    assert client.get_entity.await_count == 1
    await backend.resolve_chat(resolved.id, refresh=True)
    assert client.get_entity.await_count == 2


async def test_unknown_numeric_chat_suggests_username_without_leaking_error() -> None:
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings,
        runtime=TelegramRuntime(
            settings,
            client=SimpleNamespace(
                is_connected=lambda: True, get_entity=AsyncMock(side_effect=ValueError("secret"))
            ),
        ),
        chat_cache=chat_cache,
    )
    with pytest.raises(ToolError, match="username or t.me link") as caught:
        await backend.resolve_chat("-1000000000123")
    assert caught.value.code == "INVALID_REFERENCE"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "entity",
    [
        channel(broadcast=True, username="example"),
        channel(broadcast=True, usernames=[types.Username("example", active=True)]),
    ],
)
async def test_public_channel_checks_fresh_entity_and_active_usernames(
    entity: types.Channel,
) -> None:
    settings = Settings()
    chat_cache = {}
    chat_cache["-1000000000123"] = Chat(id="-1000000000123", title="Stale", type="group")
    client = SimpleNamespace(is_connected=lambda: True, get_entity=AsyncMock(return_value=entity))
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    resolved = await backend.public_channel("-1000000000123")
    assert resolved.type == "channel" and resolved.username == "example"
    assert resolved.title == "News"
    client.get_entity.assert_awaited_once_with(-1000000000123)
    client.get_entity.return_value = channel(broadcast=True)
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await backend.public_channel(resolved.id)
    assert client.get_entity.await_count == 2


@pytest.mark.parametrize(
    "entity",
    [
        types.User(id=123, username="example"),
        types.ChannelForbidden(id=123, access_hash=1, title="Forbidden", broadcast=True),
        channel(broadcast=False, megagroup=True, username="example"),
        channel(broadcast=True, megagroup=True, username="example"),
        channel(broadcast=True, usernames=[types.Username("example", active=False)]),
        channel(broadcast=True),
    ],
)
async def test_public_exception_rejects_private_and_non_broadcast_entities(entity: object) -> None:
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings,
        runtime=TelegramRuntime(
            settings,
            client=SimpleNamespace(
                is_connected=lambda: True, get_entity=AsyncMock(return_value=entity)
            ),
        ),
        chat_cache=chat_cache,
    )
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await backend.public_channel("-1000000000123")
