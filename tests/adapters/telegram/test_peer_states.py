from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from tests.support.fakes import BASE


def dialog(cid, *, pts=5, top=10):
    return types.Dialog(
        peer=types.PeerChannel(cid),
        top_message=top,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        notify_settings=types.PeerNotifySettings(),
        pts=pts,
    )


def channel(cid, **kwargs):
    return types.Channel(
        id=cid,
        title=f"Selected {cid}",
        photo=types.ChatPhotoEmpty(),
        date=BASE,
        access_hash=100 + cid,
        broadcast=True,
        **kwargs,
    )


def setup(response, inputs):
    client = AsyncMock(return_value=response)
    client.is_connected = Mock(return_value=True)
    client.session = SimpleNamespace(get_input_entity=Mock(side_effect=lambda cid: inputs[cid]))
    runtime = TelegramRuntime(Settings(), client=client)
    return TelegramBackend(Settings(), runtime=runtime), client


async def test_peer_metadata_is_one_selected_rpc_with_no_username_or_history_lookups():
    response = SimpleNamespace(
        dialogs=[dialog(1), dialog(2), dialog(99)],
        chats=[channel(1), channel(2, noforwards=True), channel(99)],
        messages=[],
        users=[],
    )
    backend, client = setup(
        response,
        {
            -1000000000001: types.InputPeerChannel(1, 101),
            -1000000000002: types.InputPeerChannel(2, 102),
        },
    )
    result = await backend.peer_states(["-1000000000001", "-1000000000002", "-1000000000001"])
    assert set(result) == {"-1000000000001", "-1000000000002"}
    assert result["-1000000000001"].revision.pts == 5
    assert result["-1000000000001"].exportable
    assert not result["-1000000000002"].exportable
    client.assert_awaited_once()
    request = client.call_args.args[0]
    assert isinstance(request, functions.messages.GetPeerDialogsRequest)
    assert [p.peer.channel_id for p in request.peers] == [1, 2]
    client.get_entity.assert_not_called()
    client.get_input_entity.assert_not_called()
    client.get_messages.assert_not_called()
    assert "-1000000000099" not in backend.chat_gateway.cache


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(dialogs=[dialog(1, pts=None)], chats=[channel(1)]),
        SimpleNamespace(dialogs=[dialog(1)], chats=[channel(1, min=True)]),
        SimpleNamespace(dialogs=[], chats=[channel(1)]),
    ],
)
async def test_missing_marker_or_incomplete_entity_requires_fallback(response):
    backend, _ = setup(response, {-1000000000001: types.InputPeerChannel(1, 101)})
    assert await backend.peer_states(["-1000000000001"]) == {}


async def test_missing_session_entity_never_performs_an_implicit_network_resolution():
    backend, client = setup(None, {})
    client.session.get_input_entity.side_effect = ValueError("No cached peer")
    assert await backend.peer_states(["-1000000000001"]) == {}
    client.assert_not_awaited()
