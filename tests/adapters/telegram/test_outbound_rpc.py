"""Public TL requests, exact random-id receipts, and no implicit mutation retries."""

from unittest.mock import AsyncMock, Mock

import pytest
from telethon import TelegramClient, errors, functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram.callbacks import CallbackGateway
from telegram_mcp.adapters.telegram.drafts import DraftGateway, normalize_draft
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.adapters.telegram.safe_client import SafeTelegramClient
from telegram_mcp.adapters.telegram.sending import SendGateway, send_receipt
from telegram_mcp.domain.models import Chat, MessageRef, ToolError
from telegram_mcp.domain.outbound import OutboundOperation, RegisteredFile, SendReceipt, SendRequest
from tests.support.fakes import BASE, FakeClock


def operation():
    return OutboundOperation(
        operation_id="one",
        account_id="101",
        idempotency_key="one",
        kind="message",
        request_hash="hash",
        chat_id="1",
        random_id=42,
        receipt=SendReceipt(operation_id="one", status="pending", chat_id="1"),
    )


def test_exact_random_id_receipt_does_not_use_text_or_time_matching():
    op = operation()
    raw = types.Message(id=7, peer_id=types.PeerUser(1), date=BASE, message="body", out=True)
    updates = types.Updates(
        updates=[types.UpdateMessageID(7, 42), types.UpdateNewMessage(raw, 1, 1)],
        users=[],
        chats=[],
        date=BASE,
        seq=0,
    )
    receipt = send_receipt(updates, op)
    assert receipt.message_ref is not None
    assert receipt.message_ref.key == "1:7" and receipt.sent_at == BASE
    updates.updates[0] = types.UpdateMessageID(7, 43)
    with pytest.raises(ToolError, match="SEND_UNCERTAIN"):
        send_receipt(updates, op)


async def test_send_request_has_stable_random_id_plain_text_and_no_paid_send():
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    client.get_input_entity.return_value = types.InputPeerUser(1, 2)
    client.return_value = types.UpdateShortSentMessage(id=7, pts=1, pts_count=1, date=BASE)
    gateway = SendGateway(TelegramRuntime(Settings(), client=client))
    request = SendRequest(chat_ref="1", text="**Literal** <text>")
    receipt = await gateway.send(operation(), request, None)
    assert receipt.status == "sent"
    rpc = client.call_args.args[0]
    assert isinstance(rpc, functions.messages.SendMessageRequest)
    assert rpc.random_id == 42 and rpc.message == request.text and rpc.entities is None
    assert rpc.no_webpage and rpc.clear_draft is False and not rpc.allow_paid_floodskip
    assert rpc.allow_paid_stars == 0 and rpc.schedule_date is None


async def test_native_draft_upload_keeps_file_and_caption():
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    client.get_input_entity.return_value = types.InputPeerUser(1, 2)
    client.upload_file.return_value = types.InputFile(
        id=123, parts=1, name="cv.pdf", md5_checksum=""
    )
    document = types.Document(
        id=123,
        access_hash=456,
        file_reference=b"private",
        date=BASE,
        mime_type="application/pdf",
        size=2,
        dc_id=2,
        attributes=[],
    )
    client.side_effect = [types.MessageMediaDocument(document=document), True]
    gateway = DraftGateway(TelegramRuntime(Settings(), client=client))
    file = RegisteredFile(
        file_id="file",
        name="cv.pdf",
        mime_type="application/pdf",
        size=2,
        sha256="0" * 64,
        created_at=BASE,
    )
    assert await gateway.save("1", "My CV", file, b"CV") == "document:123"
    rpc = client.call_args.args[0]
    assert isinstance(rpc, functions.messages.SaveDraftRequest)
    assert rpc.message == "My CV" and isinstance(rpc.media, types.InputMediaDocument)
    assert rpc.media.id.id == 123 and rpc.no_webpage
    raw = types.DraftMessage(message="My CV", date=BASE, media=rpc.media)
    draft = normalize_draft("1", raw)
    assert draft.has_media and draft.media_identity == "document:123"
    raw.media = types.InputMediaDocument(types.InputDocument(124, 456, b"private"))
    assert normalize_draft("1", raw).revision != draft.revision


@pytest.mark.parametrize("mutation", [False, True])
async def test_only_explicit_read_requests_retry_server_errors(monkeypatch, mutation):
    call = AsyncMock(side_effect=[errors.ServerError(None, "INTERNAL", 500), object()])
    sleep = AsyncMock()
    monkeypatch.setattr(TelegramClient, "__call__", call)
    monkeypatch.setattr("telegram_mcp.adapters.telegram.safe_client.asyncio.sleep", sleep)
    client = object.__new__(SafeTelegramClient)
    rpc = (
        functions.messages.SendMessageRequest(types.InputPeerSelf(), "Hello", random_id=42)
        if mutation
        else functions.updates.GetStateRequest()
    )
    if mutation:
        with pytest.raises(errors.ServerError):
            await client(rpc)
        assert call.await_count == 1 and sleep.await_count == 0
    else:
        await client(rpc)
        assert call.await_count == 2 and sleep.await_count == 1


async def test_slow_mode_is_scoped_and_callback_timeouts_are_uncertain():
    clock = FakeClock()
    runtime = TelegramRuntime(Settings(), clock=clock)

    async def slow():
        raise errors.SlowModeWaitError(None, capture=30)

    with pytest.raises(ToolError, match="RATE_LIMITED") as failure:
        await runtime.write_call(slow(), scope="send:1")
    assert failure.value.scope == "send:1"
    with pytest.raises(ToolError, match="RATE_LIMITED"):
        runtime.require_write_ready("send:1")
    runtime.require_write_ready("send:2")

    async def bot_timeout():
        raise errors.BotResponseTimeoutError(None)

    with pytest.raises(ToolError, match="CALLBACK_UNCERTAIN"):
        await runtime.write_call(bot_timeout(), scope="callback:2")


@pytest.mark.parametrize(
    "kind",
    [
        types.InlineButtonTypeCallback(data=b"x", requires_password=True),
        types.InlineButtonTypeUrl(url="https://example.test"),
        types.InlineButtonTypeBuy(),
    ],
)
def test_callback_rejects_password_url_and_payment_buttons(kind):
    from telegram_mcp.adapters.telegram.buttons import button_id

    button = types.KeyboardInlineButton("Action", kind)
    raw = types.Message(
        id=1,
        peer_id=types.PeerUser(2),
        date=BASE,
        message="text",
        reply_markup=types.ReplyInlineMarkup([types.KeyboardInlineButtonRow([button])]),
    )
    with pytest.raises(ToolError, match="BUTTON_UNSUPPORTED"):
        CallbackGateway._payload(raw, button_id(button, 0, 0))


@pytest.mark.parametrize("bot_times_out", [False, True])
async def test_callback_observation_is_bounded_to_same_peer_and_handler_is_removed(
    monkeypatch, bot_times_out
):
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    client.add_event_handler = Mock()
    client.remove_event_handler = Mock()
    client.get_input_entity.return_value = types.InputPeerUser(2, 42)
    button = types.KeyboardInlineButton("Details", types.InlineButtonTypeCallback(data=b"private"))
    raw = types.Message(
        id=7,
        peer_id=types.PeerUser(2),
        date=BASE,
        message="Vacancy",
        reply_markup=types.ReplyInlineMarkup([types.KeyboardInlineButtonRow([button])]),
    )
    client.get_messages.return_value = raw
    chats = Mock()
    chats.resolve_chat = AsyncMock(return_value=Chat(id="2", title="Bot", type="bot"))
    gateway = CallbackGateway(TelegramRuntime(Settings(), client=client), chats)
    ref = MessageRef(chat_id="2", message_id=7)
    message = await gateway.message(ref)
    assert message.reply_markup is not None and message.revision

    async def invoke(rpc):
        assert isinstance(rpc, functions.messages.GetBotCallbackAnswerRequest)
        assert rpc.msg_id == 7 and rpc.data == b"private"
        if bot_times_out:
            raise errors.BotResponseTimeoutError(rpc)
        observe = client.add_event_handler.call_args.args[0]
        for mid in range(1, 30):
            candidate = types.Message(
                id=mid,
                peer_id=types.PeerUser(3 if mid == 1 else 2),
                date=BASE,
                message="Your login code: 123456" if mid == 2 else "Reply",
            )
            await observe(types.UpdateNewMessage(candidate, 1, 1))
        return types.messages.BotCallbackAnswer(cache_time=0, message="Done")

    client.side_effect = invoke
    monkeypatch.setattr("telegram_mcp.adapters.telegram.callbacks.asyncio.sleep", AsyncMock())
    if bot_times_out:
        with pytest.raises(ToolError, match="CALLBACK_UNCERTAIN"):
            await gateway.invoke(
                operation(), ref, message.reply_markup.rows[0][0].button_id, message.revision
            )
    else:
        result = await gateway.invoke(
            operation(), ref, message.reply_markup.rows[0][0].button_id, message.revision
        )
        assert result.status == "completed" and not result.observation_complete
        assert [ref.key for ref in result.observed_message_refs] == [
            f"2:{mid}" for mid in range(3, 23)
        ]
    client.assert_awaited_once()
    client.remove_event_handler.assert_called_once_with(client.add_event_handler.call_args.args[0])
