"""Permissions, conflicts, immutable files and durable recovery before any live sends."""

import asyncio
from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Chat, MessageButton, ReplyMarkup, ToolError
from telegram_mcp.domain.outbound import Draft
from tests.support.fakes import msg
from tests.support.outbound import approve, outbound_fixture


async def test_send_replays_after_restart_and_rejects_changed_payload(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        first = await env.service.send_message("1", "Привет 😀", "request-1", confirm=approve)
        assert first.status == "sent" and len(env.telegram.send_calls) == 1
        assert (await env.store.operation("101", "request-1")).payload == {}
        telegram = env.telegram
    async with outbound_fixture(tmp_path, telegram=telegram) as env:
        assert await env.service.send_message("1", "Привет 😀", "request-1") == first
        assert len(telegram.send_calls) == 1
        with pytest.raises(ToolError, match="IDEMPOTENCY_CONFLICT"):
            await env.service.send_message("1", "Other text", "request-1", confirm=approve)


async def test_unknown_send_uses_same_random_id_and_confirmation_after_restart(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        env.telegram.send_error = ToolError("CONNECTION_ERROR", "Lost response")
        first = await env.service.send_message("1", "Hello", "key", confirm=approve)
        assert first.status == "unknown"
        assert (await env.service.get_send_status(first.operation_id)).status == "unknown"
        telegram = env.telegram
        random_id = telegram.send_calls[0][0]
    telegram.send_error = None
    async with outbound_fixture(tmp_path, telegram=telegram) as env:
        recovered = await env.service.send_message("1", "Hello", "key")
        assert recovered.status == "sent" and recovered.operation_id == first.operation_id
        assert [call[0] for call in telegram.send_calls] == [random_id, random_id]


async def test_duplicate_while_awaiting_approval_does_not_ask_or_send_twice(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def confirm(description):
        entered.set()
        await release.wait()
        return True

    async with outbound_fixture(tmp_path) as env:
        task = asyncio.create_task(env.service.send_message("1", "One", "key", confirm=confirm))
        await entered.wait()
        duplicate = await env.service.send_message("1", "One", "key", confirm=approve)
        assert duplicate.status == "pending" and not env.telegram.send_calls
        release.set()
        result = await task
        assert result.operation_id == duplicate.operation_id and len(env.telegram.send_calls) == 1


@pytest.mark.parametrize(
    "confirmation,code", [(None, "CONFIRMATION_UNSUPPORTED"), (False, "CONFIRMATION_DECLINED")]
)
async def test_missing_or_declined_confirmation_never_dispatches(tmp_path, confirmation, code):
    async def decline(description):
        return False

    async with outbound_fixture(tmp_path) as env:
        receipt = await env.service.send_message(
            "1", "Hello", "key", confirm=decline if confirmation is False else None
        )
        assert receipt.status == "failed" and receipt.error.code == code
        assert env.telegram.send_calls == []


@pytest.mark.parametrize("changed", ["read", "write", "flag", "account"])
async def test_rechecks_permission_and_account_after_confirmation(tmp_path, changed):
    async with outbound_fixture(tmp_path) as env:

        async def change(description):
            if changed == "read":
                env.settings.allowed_chat_ids = []
            elif changed == "write":
                env.settings.allowed_write_chat_ids = []
            elif changed == "flag":
                env.settings.allow_send_messages = False
            else:
                env.telegram.account = "102"
            return True

        result = await env.service.send_message("1", "Hello", "key", confirm=change)
        assert result.status == "failed" and not env.telegram.send_calls


async def test_public_read_exception_does_not_grant_writes(tmp_path):
    settings = Settings(
        allow_send_messages=True, allow_public_channel_reads=True, allowed_write_chat_ids=["1"]
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        env.backend.chats[0] = Chat(id="1", title="Public", type="channel", username="public")
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.send_message("1", "Hello", "key", confirm=approve)
        assert env.telegram.send_calls == []


async def test_flood_wait_survives_restart_without_dispatching_early(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        env.telegram.send_error = ToolError("RATE_LIMITED", "Wait", 60)
        first = await env.service.send_message("1", "Hello", "key", confirm=approve)
        assert first.status == "pending" and first.retry_at
        telegram, clock = env.telegram, env.clock
    telegram.send_error = None
    async with outbound_fixture(tmp_path, telegram=telegram, clock=clock) as env:
        assert await env.service.send_message("1", "Hello", "key") == first
        assert len(telegram.send_calls) == 1
        clock.now += timedelta(seconds=61)
        assert (await env.service.send_message("1", "Hello", "key")).status == "sent"
        assert telegram.send_calls[0][0] == telegram.send_calls[1][0]


async def test_account_cooldown_blocks_new_keys_after_restart(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        env.telegram.send_error = ToolError("RATE_LIMITED", "Wait", 60)
        await env.service.send_message("1", "Hello", "first", confirm=approve)
        telegram, clock = env.telegram, env.clock
    telegram.send_error = None
    async with outbound_fixture(tmp_path, telegram=telegram, clock=clock) as env:
        result = await env.service.send_message("2", "Other recipient", "second", confirm=approve)
        assert result.status == "pending" and len(telegram.send_calls) == 1
        status = await env.service.get_status()
        assert status["cooldowns"][0]["scope"] == "account"
        clock.now += timedelta(seconds=61)
        assert (
            await env.service.send_message("2", "Other recipient", "second", confirm=approve)
        ).status == "sent"


async def test_cancel_after_remote_send_remains_unknown_and_reuses_random_id(tmp_path, monkeypatch):
    sent = asyncio.Event()
    async with outbound_fixture(tmp_path) as env:
        original = env.telegram.send

        async def lose_reply(*args):
            await original(*args)
            sent.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(env.telegram, "send", lose_reply)
        task = asyncio.create_task(env.service.send_message("1", "Hello", "key", confirm=approve))
        await sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        op = await env.store.operation("101", "key")
        assert op.receipt.status == "unknown" and op.dispatched
        monkeypatch.setattr(env.telegram, "send", original)
        recovered = await env.service.send_message("1", "Hello", "key")
        assert recovered.status == "sent" and recovered.operation_id == op.operation_id
        assert len(env.telegram.sent) == 1 and len(env.telegram.send_calls) == 2


async def test_cancel_during_confirmation_never_dispatches(tmp_path):
    entered = asyncio.Event()

    async def waiting(description):
        entered.set()
        await asyncio.Event().wait()
        return True

    async with outbound_fixture(tmp_path) as env:
        task = asyncio.create_task(env.service.send_message("1", "Hello", "key", confirm=waiting))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operation = await env.store.operation("101", "key")
        assert not operation.dispatched and operation.receipt.status == "failed"
        assert not env.telegram.send_calls


async def test_registered_file_keeps_original_bytes_and_receipt_after_removal(tmp_path):
    path = tmp_path / "cv.pdf"
    path.write_bytes(b"Original CV")
    async with outbound_fixture(tmp_path) as env:
        file = await env.service.register_file(path=str(path))
        path.write_bytes(b"Changed local file")
        result = await env.service.send_file(
            "1", file.file_id, "Please find my CV", "cv-1", confirm=approve
        )
        assert result.status == "sent" and result.file.sha256 == file.sha256
        assert env.telegram.send_calls[0][2] == b"Original CV"
        assert env.telegram.send_calls[0][1].text == "Please find my CV"
        await env.store.remove_file(file.file_id)
        assert await env.service.send_file("1", file.file_id, "Please find my CV", "cv-1") == result
        assert len(env.telegram.send_calls) == 1


async def test_drafts_preserve_user_state_and_use_optimistic_revision(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        created = await env.service.create_draft("1", "First")
        assert created.owned
        with pytest.raises(ToolError, match="DRAFT_EXISTS"):
            await env.service.create_draft("1", "Overwrite")
        updated = await env.service.update_draft("1", "Second", created.revision)
        assert updated.owned and updated.revision != created.revision
        with pytest.raises(ToolError, match="DRAFT_CONFLICT"):
            await env.service.update_draft("1", "Stale", created.revision)
        env.telegram.drafts["1"] = Draft(
            chat_id="1", text="User draft", revision="user", exists=True
        )
        with pytest.raises(ToolError, match="CONFIRMATION_UNSUPPORTED"):
            await env.service.update_draft("1", "Replacement", "user")
        replaced = await env.service.update_draft("1", "Replacement", "user", confirm=approve)
        assert replaced.owned and (await env.service.list_drafts()).drafts[0] == replaced


async def test_draft_changed_during_confirmation_is_not_overwritten(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        env.telegram.drafts["1"] = Draft(chat_id="1", text="Old", revision="old", exists=True)

        async def change(description):
            env.telegram.drafts["1"] = Draft(chat_id="1", text="New", revision="new", exists=True)
            return True

        with pytest.raises(ToolError, match="DRAFT_CONFLICT"):
            await env.service.update_draft("1", "Agent", "old", confirm=change)
        assert (await env.service.get_draft("1")).text == "New"


async def test_text_draft_can_be_cleared(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        draft = await env.service.create_draft("1", "Hello")
        assert not draft.has_media and draft.file is None and draft.text == "Hello"
        empty = await env.service.update_draft("1", "", draft.revision)
        assert not empty.exists and not empty.has_media


async def test_draft_post_save_media_conflict_does_not_claim_ownership(tmp_path, monkeypatch):
    async with outbound_fixture(tmp_path) as env:
        original = env.telegram.save_draft

        async def concurrent_change(*args):
            await original(*args)
            env.telegram.drafts["1"].has_media = True
            env.telegram.drafts["1"].media_identity = "different-file"
            env.telegram.drafts["1"].revision = "user-change"

        monkeypatch.setattr(env.telegram, "save_draft", concurrent_change)
        with pytest.raises(ToolError, match="DRAFT_CONFLICT"):
            await env.service.create_draft("1", "My CV")
        assert not (await env.service.get_draft("1")).owned


@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("file_id", ["registered-file", ""])
async def test_file_draft_is_refused_before_access_upload_confirmation_or_write(
    tmp_path, monkeypatch, update, file_id
):
    async with outbound_fixture(tmp_path) as env:
        existing = Draft(chat_id="1", text="User text", revision="user", exists=True)
        env.telegram.drafts["1"] = existing

        async def unexpected(*args, **kwargs):
            pytest.fail("File drafts must be rejected before any backend or file operation")

        for name in ("account_id", "draft", "save_draft"):
            monkeypatch.setattr(env.telegram, name, unexpected)
        monkeypatch.setattr(env.backend, "resolve_chat", unexpected)
        monkeypatch.setattr(env.store, "file", unexpected)
        with pytest.raises(ToolError, match="DRAFT_ATTACHMENT_UNSUPPORTED"):
            if update:
                await env.service.update_draft(
                    "1", "Changed", "user", file_id=file_id, confirm=unexpected
                )
            else:
                await env.service.create_draft("1", "Hello", file_id)
        assert env.telegram.drafts["1"] == existing


@pytest.mark.parametrize(
    "text,file,valid",
    [
        ("😀" * 2048, False, True),
        ("😀" * 2049, False, False),
        ("😀" * 512, True, True),
        ("😀" * 513, True, False),
    ],
)
def test_utf16_limits_do_not_truncate(text, file, valid):
    from telegram_mcp.domain.outbound import check_text

    if valid:
        check_text(text, file=file)
    else:
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            check_text(text, file=file)


async def test_callback_duplicate_and_unknown_are_never_reexecuted(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        message = msg("2", 7).model_copy(
            update={
                "revision": "one",
                "reply_markup": ReplyMarkup(
                    type="inline",
                    rows=[[MessageButton(button_id="opaque", type="callback", text="Details")]],
                ),
            }
        )
        env.telegram.button_message = message
        env.telegram.callback_error = ToolError("CONNECTION_ERROR", "Lost response")
        result = await env.service.invoke_button("2:7", "opaque", "one", "button-1", approve)
        assert result.status == "unknown" and env.telegram.callback_calls == 1
        env.telegram.button_message = message.model_copy(update={"revision": "two"})
        replay = await env.service.invoke_button("2:7", "opaque", "one", "button-1", approve)
        assert replay.operation_id == result.operation_id and env.telegram.callback_calls == 1
        with pytest.raises(ToolError, match="MESSAGE_CONFLICT"):
            await env.service.invoke_button("2:7", "opaque", "one", "new-key", approve)


@pytest.mark.parametrize("chat_type", ["private", "group", "channel", "saved"])
async def test_callback_requires_private_bot_dialog(tmp_path, chat_type):
    async with outbound_fixture(tmp_path) as env:
        env.backend.chats[1].type = chat_type
        with pytest.raises(ToolError, match="BUTTON_UNSUPPORTED"):
            await env.service.invoke_button("2:7", "opaque", "one", "key", approve)
        assert env.telegram.callback_calls == 0
