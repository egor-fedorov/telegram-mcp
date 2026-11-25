"""Actual TL types exercise peer binding, eligibility and mutation request shapes."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import functions, types, utils

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram.action_folders import action_folders
from telegram_mcp.adapters.telegram.message_actions import MessageActionGateway
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.domain.models import MessageRef, ToolError
from tests.support.fakes import BASE, FakeClock


def channel(**kwargs):
    return types.Channel(
        id=7, title="Test", photo=types.ChatPhotoEmpty(), date=BASE, access_hash=1, **kwargs
    )


def make(entity=None, **raw_fields):
    entity = entity or types.User(7, access_hash=111, first_name="Peer")
    ref = MessageRef(chat_id=str(utils.get_peer_id(entity)), message_id=15)
    fields: dict[str, Any] = dict(
        id=15, peer_id=utils.get_peer(entity), out=True, message="Plain", date=BASE
    )
    raw = types.Message(**(fields | raw_fields))
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    client.get_messages.return_value = raw
    limits = SimpleNamespace(
        edit_time_limit=100, revoke_time_limit=200, revoke_pm_time_limit=300, revoke_pm_inbox=True
    )

    def respond(request):
        if isinstance(request, functions.help.GetConfigRequest):
            return limits
        if isinstance(request, functions.messages.EditMessageRequest):
            return types.Updates([], [], [], BASE, 1)
        return types.messages.AffectedMessages(1, 1)

    client.side_effect = respond
    chats = AsyncMock()
    chats.resolve_entity.return_value = entity
    chats.chat_details.return_value = {"linked_chat_id": "-1000000000040"}
    runtime = TelegramRuntime(Settings(), client=client, clock=FakeClock(now=BASE))
    return MessageActionGateway(runtime, chats), ref, raw, client, limits


@pytest.mark.parametrize("action,revoke", [("delete_for_me", False), ("delete_for_everyone", True)])
@pytest.mark.parametrize("basic", [True, False])
async def test_private_and_basic_group_delete_one_id(action, revoke, basic):
    entity = types.Chat(7, "Group", types.ChatPhotoEmpty(), 1, BASE, 1) if basic else None
    gateway, ref, raw, client, _ = make(entity)
    target = await gateway.inspect(ref, "101")
    await target.dispatch(action, None)
    rpc = client.call_args.args[0]
    assert (
        isinstance(rpc, functions.messages.DeleteMessagesRequest)
        and rpc.id == [raw.id]
        and rpc.revoke is revoke
    )


@pytest.mark.parametrize("entity", [channel(megagroup=True), channel(broadcast=True, creator=True)])
async def test_channel_visibility_is_never_promoted(entity):
    gateway, ref, _, client, _ = make(entity)
    target = await gateway.inspect(ref, "101")
    assert "delete_for_me" not in target.supported
    with pytest.raises(ToolError, match="ACTION_NOT_ALLOWED"):
        await target.dispatch("delete_for_me", None)
    await target.dispatch("delete_for_everyone", None)
    request = client.call_args.args[0]
    assert isinstance(request, functions.channels.DeleteMessagesRequest)
    assert str(utils.get_peer_id(request.channel)) == ref.chat_id and request.id == [15]


async def test_same_numeric_message_in_wrong_peer_fails_before_any_mutation():
    gateway, ref, _, client, _ = make(peer_id=types.PeerUser(222))
    with pytest.raises(ToolError, match="MESSAGE_UNAVAILABLE"):
        await gateway.inspect(ref, "101")
    client.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"entities": [types.MessageEntityBold(0, 2)]},
        {"reply_markup": types.ReplyInlineMarkup([])},
        {"grouped_id": 4},
        {"rich_message": object()},
        {"media": types.MessageMediaWebPage(types.WebPageEmpty(99))},
    ],
)
async def test_rich_target_cannot_be_flattened(changes):
    gateway, ref, _, client, _ = make(**changes)
    target = await gateway.inspect(ref, "101")
    assert "edit" not in target.supported
    with pytest.raises(ToolError, match="ACTION_NOT_ALLOWED"):
        await target.dispatch("edit", "Plain")
    assert client.await_count == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"suggested_post": object()},
        {"paid_message_stars": 1},
        {"from_scheduled": True},
        {"media": types.MessageMediaPaidMedia(5, [])},
    ],
)
async def test_paid_and_scheduled_targets_excluded(fields):
    gateway, ref, _, client, _ = make(**fields)
    with pytest.raises(ToolError, match="ACTION_UNSUPPORTED"):
        await gateway.inspect(ref, "101")
    client.assert_not_awaited()


async def test_service_topic_roots_and_automatic_discussion_copies_excluded():
    origin = types.PeerChannel(40)
    gateway, ref, raw, client, _ = make(
        channel(megagroup=True, forum=True),
        from_id=origin,
        fwd_from=types.MessageFwdHeader(BASE, from_id=origin, channel_post=2),
    )
    with pytest.raises(ToolError, match="ACTION_UNSUPPORTED"):
        await gateway.inspect(ref, "101")
    raw.fwd_from = types.MessageFwdHeader(BASE, from_name="Hidden original author")
    with pytest.raises(ToolError, match="ACTION_UNSUPPORTED"):
        await gateway.inspect(ref, "101")
    # Ordinary forwards from the same linked channel can be deleted as copies.
    raw.fwd_from = types.MessageFwdHeader(BASE, from_id=origin, channel_post=2)
    raw.from_id = types.PeerUser(101)
    target = await gateway.inspect(ref, "101")
    assert target.supported == {"delete_for_everyone"}
    raw.id = 1
    with pytest.raises(ToolError, match="ACTION_UNSUPPORTED"):
        await gateway.inspect(MessageRef(chat_id=ref.chat_id, message_id=1), "101")
    client.get_messages.return_value = types.MessageService(
        id=15, peer_id=raw.peer_id, date=BASE, action=types.MessageActionTopicCreate("Topic", 0)
    )
    with pytest.raises(ToolError, match="MESSAGE_UNAVAILABLE"):
        await gateway.inspect(ref, "101")


async def test_private_inbox_rights_time_limit_and_saved_or_bot_visibility():
    gateway, ref, raw, _, config = make(out=False)
    target = await gateway.inspect(ref, "101")
    assert target.target == "private_incoming" and target.supported == {
        "delete_for_me",
        "delete_for_everyone",
    }
    config.revoke_pm_inbox = False
    assert (await gateway.inspect(ref, "101")).supported == {"delete_for_me"}
    config.revoke_pm_inbox = True
    raw.date = BASE - timedelta(seconds=301)
    assert (await gateway.inspect(ref, "101")).supported == {"delete_for_me"}
    for entity in (
        types.User(7, bot=True, access_hash=1),
        types.User(101, is_self=True, access_hash=2),
    ):
        gateway, ref, *_ = make(entity)
        assert "delete_for_everyone" not in (await gateway.inspect(ref, "101")).supported


async def test_group_moderation_cannot_edit_others_or_use_forward_origin_as_owner():
    group = channel(megagroup=True)
    gateway, ref, *_ = make(
        group,
        out=False,
        from_id=types.PeerUser(3),
        fwd_from=types.MessageFwdHeader(BASE, from_id=types.PeerUser(101)),
    )
    target = await gateway.inspect(ref, "101")
    assert target.target == "group_others" and target.supported == set()
    group.admin_rights = types.ChatAdminRights(delete_messages=True, edit_messages=True)
    target = await gateway.inspect(ref, "101")
    assert target.supported == {"delete_for_everyone"}


async def test_edit_plain_caption_keeps_exact_media():
    photo = types.Photo(123, 333, b"ref", BASE, [], 1)
    gateway, ref, _, client, _ = make(media=types.MessageMediaPhoto(photo=photo))
    target = await gateway.inspect(ref, "101")
    assert target.caption
    await target.dispatch("edit", "New caption")
    req = client.call_args.args[0]
    assert isinstance(req, functions.messages.EditMessageRequest) and req.id == 15
    assert req.peer.user_id == 7 and req.message == "New caption" and req.no_webpage
    assert req.media is None and req.entities is None and req.reply_markup is None


async def test_fresh_folder_metadata_rechecks_dynamic_and_explicit_membership():
    user = types.User(7, access_hash=2, first_name="User", contact=True)
    raw = types.Dialog(types.PeerUser(7), 15, 0, 0, 1, 0, 0, 0, types.PeerNotifySettings())
    folder = types.DialogFilter(
        id=7,
        title=types.TextWithEntities("Test", []),
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
        contacts=True,
        exclude_read=True,
    )
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    client.get_input_entity.return_value = types.InputPeerUser(7, 2)

    def reply(request):
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[folder])
        assert (
            isinstance(request, functions.messages.GetPeerDialogsRequest)
            and len(request.peers) == 1
        )
        assert request.peers[0].peer.user_id == 7
        return SimpleNamespace(dialogs=[raw], users=[user], chats=[])

    client.side_effect = reply
    runtime = TelegramRuntime(Settings(), client=client)
    assert await action_folders(runtime, "7", "101", {7}) == {7}
    raw.unread_count = 0
    assert await action_folders(runtime, "7", "101", {7}) == set()
    folder.include_peers = [types.InputPeerUser(7, 2)]
    assert await action_folders(runtime, "7", "101", {7}) == {7}
    folder.exclude_peers = [types.InputPeerUser(7, 2)]
    assert await action_folders(runtime, "7", "101", {7}) == set()
