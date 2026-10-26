from datetime import timedelta
from types import SimpleNamespace

from telethon import types

from telegram_mcp.adapters.telegram.mapping import (
    chat_from_entity,
    folder_from_tl,
    normalize_message,
)
from telegram_mcp.domain.models import Chat, MessageRef
from tests.support.fakes import BASE


def test_folder_mapping_with_formatted_title_and_shared_folder() -> None:
    raw = types.DialogFilter(
        id=4,
        title=types.TextWithEntities("AI", []),
        pinned_peers=[types.InputPeerChannel(123, 45)],
        include_peers=[types.InputPeerUser(12, 34)],
        exclude_peers=[types.InputPeerChat(67)],
        broadcasts=True,
        exclude_read=True,
    )
    folder = folder_from_tl(raw, 2)
    assert folder.title == "AI" and folder.position == 2
    assert folder.pinned_chat_ids == ["-1000000000123"]
    assert folder.excluded_chat_ids == ["-67"]
    assert folder.include_channels and folder.exclude_read
    shared = types.DialogFilterChatlist(
        id=5,
        title=types.TextWithEntities("Shared", []),
        pinned_peers=[],
        include_peers=[types.InputPeerUser(12, 34)],
    )
    assert folder_from_tl(shared, 3).included_chat_ids == ["12"]


def test_normalization_preserves_source_links_and_reply_peer() -> None:
    chat = Chat(id="-1000000000123", title="News", type="channel", username="example")
    raw = types.Message(
        id=10,
        peer_id=types.PeerChannel(123),
        date=BASE,
        message="Original post",
        from_id=types.PeerChannel(123),
        reply_to=types.MessageReplyHeader(
            reply_to_msg_id=8, reply_to_peer_id=types.PeerChannel(456)
        ),
    )
    result = normalize_message(raw, chat)
    assert result.text == "Original post"
    assert result.url == "https://t.me/example/10"
    assert result.reply_to == MessageRef(chat_id="-1000000000456", message_id=8)
    private_channel = chat.model_copy(update={"username": None})
    assert normalize_message(raw, private_channel).url == "https://t.me/c/123/10"
    basic_group = Chat(id="-100123", title="Basic", type="group")
    assert normalize_message(raw, basic_group).url is None


def test_inherited_and_explicit_mute_settings() -> None:
    entity = types.User(id=1, first_name="First", contact=True)
    dialog = SimpleNamespace(
        archived=False,
        unread_count=0,
        unread_mentions_count=1,
        dialog=SimpleNamespace(notify_settings=types.PeerNotifySettings(), unread_mark=True),
    )
    assert chat_from_entity(
        entity, dialog, default_muted=True, now=BASE + timedelta(days=1)
    ).is_muted
    dialog.dialog.notify_settings = types.PeerNotifySettings(mute_until=BASE)
    mapped = chat_from_entity(entity, dialog, default_muted=True, now=BASE + timedelta(days=1))
    assert not mapped.is_muted and mapped.unread_mentions_count == 1


def test_forward_sources_remain_separate_for_saved_and_hidden_senders() -> None:
    chat = Chat(id="1", title="Saved Messages", type="saved")
    raw = types.Message(
        id=10,
        peer_id=types.PeerUser(1),
        date=BASE + timedelta(days=2),
        message="forward",
        fwd_from=types.MessageFwdHeader(
            date=BASE,
            from_id=types.PeerChannel(123),
            channel_post=7,
            post_author="Original author",
            saved_from_peer=types.PeerChannel(456),
            saved_from_msg_id=8,
            saved_from_id=types.PeerUser(42),
            saved_from_name="Saved author",
            saved_date=BASE + timedelta(days=1),
        ),
    )
    origin = normalize_message(raw, chat).forward_origin
    assert origin is not None
    assert origin.sender_id == "-1000000000123" and origin.post_author == "Original author"
    assert origin.message_ref == MessageRef(chat_id="-1000000000123", message_id=7)
    assert origin.saved_from_ref == MessageRef(chat_id="-1000000000456", message_id=8)
    assert origin.saved_from_sender_id == "42" and origin.saved_from_name == "Saved author"
    assert origin.sent_at == BASE and origin.saved_at == BASE + timedelta(days=1)
    raw.fwd_from = types.MessageFwdHeader(date=BASE, from_name="Hidden author")
    hidden = normalize_message(raw, chat).forward_origin
    assert hidden is not None and hidden.name == "Hidden author"
    assert hidden.sender_id is None and hidden.message_ref is None and hidden.saved_from_ref is None
