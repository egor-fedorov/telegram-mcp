import hashlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.adapters.telegram.incremental import observation
from telegram_mcp.domain.models import Chat, ToolError
from tests.support.fakes import BASE


def raw(mid=10, **kwargs):
    return types.Message(id=mid, peer_id=types.PeerUser(1), date=BASE, message="😀 text", **kwargs)


def backend(rows):
    client = SimpleNamespace(
        is_connected=lambda: True,
        get_messages=AsyncMock(return_value=rows),
        get_entity=AsyncMock(return_value=types.User(id=1)),
        session=SimpleNamespace(auth_key=SimpleNamespace(key=b"private-key")),
    )
    settings = Settings()
    result = TelegramBackend(
        settings,
        runtime=TelegramRuntime(settings, client=client),
        chat_cache={"1": Chat(id="1", title="Private", type="private")},
    )
    return result, client


async def test_incremental_gateway_uses_bounded_native_history_and_canonical_ids():
    gateway, client = backend([raw(11), raw(12)])
    result = await gateway.read_incremental("1", after_id=10, through_id=12, limit=2)
    client.get_messages.assert_awaited_once_with(1, limit=2, offset_id=10, max_id=13, reverse=True)
    assert [o.message.message_ref.message_id for o in result.observations] == [11, 12]
    assert result.exhausted and result.next_offset == 12
    empty = await gateway.read_incremental("1", after_id=12, through_id=12, limit=30)
    assert empty.exhausted and client.get_messages.await_count == 1


async def test_history_date_anchor_and_recent_head_use_inclusive_boundary_padding():
    gateway, client = backend([raw()])
    await gateway.read_incremental("1", after_id=0, through_id=12, limit=30, date_from=BASE)
    assert client.get_messages.call_args.kwargs["offset_date"] == BASE - timedelta(seconds=1)
    await gateway.recent_observations("1", 1, before=BASE)
    client.get_messages.assert_awaited_with(1, limit=1, offset_date=BASE + timedelta(seconds=1))


async def test_observation_batch_deduplicates_and_reports_missing_without_deletion_claim():
    gateway, client = backend([raw(), None, types.MessageEmpty(id=11, peer_id=types.PeerUser(1))])
    result = await gateway.observe_messages("1", [10, 11, 10, 12])
    client.get_messages.assert_awaited_once_with(1, ids=[10, 11, 12])
    assert result[10] is not None and result[11] is None and result[12] is None
    assert await gateway.observe_messages("1", []) == {}
    assert client.get_messages.await_count == 1


@pytest.mark.parametrize("restriction", ["noforwards", "ttl_period", "media_ttl"])
def test_protection_is_internal_observation_metadata(restriction):
    message = raw()
    if restriction == "media_ttl":
        message.media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1), ttl_seconds=5)
    else:
        setattr(message, restriction, True if restriction == "noforwards" else 30)
    observed = observation(message, Chat(id="1", title="Source", type="private"))
    assert not observed.exportable and observed.message.text == "😀 text"


def test_media_identity_detects_same_metadata_replacement():
    message = raw(media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)))
    chat = Chat(id="1", title="Source", type="private")
    old = observation(message, chat)
    message.media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=2))
    assert observation(message, chat).revision != old.revision


async def test_binding_is_hashed_and_export_checks_fresh_entity():
    gateway, client = backend([])
    assert await gateway.checkpoint_binding() == hashlib.sha256(b"private-key").hexdigest()
    await gateway.check_export_source("1")
    client.get_entity.assert_awaited_once_with(1)
    client.get_entity.return_value = types.Chat(
        id=1,
        title="Protected",
        photo=types.ChatPhotoEmpty(),
        participants_count=1,
        date=BASE,
        version=1,
        noforwards=True,
    )
    with pytest.raises(ToolError, match="EXPORT_PROTECTED"):
        await gateway.check_export_source("-1")
    client.get_entity.return_value = types.User(id=2)
    with pytest.raises(ToolError, match="SOURCE_UNAVAILABLE"):
        await gateway.check_export_source("1")
