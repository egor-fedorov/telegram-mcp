"""Durable peer identity and permission provenance across retries and policy changes."""

import asyncio
import json
import sqlite3
from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Chat, ToolError
from tests.support.outbound import approve, outbound_fixture


@pytest.mark.parametrize("mode", ["one_off", "all_chats"])
async def test_alias_reassignment_never_retargets_after_restart(tmp_path, mode):
    settings = Settings(
        allow_send_messages=True,
        allow_one_off_sends=mode == "one_off",
        allow_all_write_chats=mode == "all_chats",
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        env.backend.chats[0].username = "recruiter"
        env.telegram.send_error = ToolError("CONNECTION_ERROR", "Response lost")
        first = await env.service.send_message("@recruiter", "Hello", "key", confirm=approve)
        assert first.status == "unknown"
        telegram = env.telegram
        random_id = telegram.send_calls[0][0]
    telegram.send_error = None
    async with outbound_fixture(tmp_path, settings=settings, telegram=telegram) as env:
        env.backend.chats[0].username = "renamed"
        env.backend.chats.append(
            Chat(id="3", title="Another person", type="private", username="recruiter")
        )
        recovered = await env.service.send_message("https://t.me/Recruiter", "Hello", "key")
        assert recovered.status == "sent" and recovered.chat_id == "1"
        assert recovered.operation_id == first.operation_id
        assert [call[0] for call in telegram.send_calls] == [random_id, random_id]
        assert [call[1].chat_ref for call in telegram.send_calls] == ["1", "1"]
        assert await env.service.send_message("1", "Hello", "key") == recovered
        assert await env.service.send_message("@renamed", "Hello", "key") == recovered
        with pytest.raises(ToolError, match="IDEMPOTENCY_CONFLICT"):
            await env.service.send_message("3", "Hello", "key")
        with pytest.raises(ToolError, match="IDEMPOTENCY_CONFLICT"):
            await env.service.send_message("@recruiter", "Other text", "key")


@pytest.mark.parametrize("mode", ["one_off", "all_chats"])
async def test_receipt_survives_revocation_without_peer_or_file_reads(tmp_path, mode, monkeypatch):
    settings = Settings(
        allow_send_files=True,
        allow_one_off_sends=mode == "one_off",
        allow_all_write_chats=mode == "all_chats",
        allowed_upload_directories=[tmp_path],
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        path = tmp_path / "cv.txt"
        path.write_text("Synthetic CV")
        file = await env.service.register_file(path=str(path))
        result = await env.service.send_file("1", file.file_id, "CV", "key", confirm=approve)
        assert result.status == "sent"
        await env.store.remove_file(file.file_id)
        settings.allow_all_write_chats = settings.allow_one_off_sends = False
        settings.allow_send_files = False

        async def unexpected(*args, **kwargs):
            raise AssertionError("Stored receipts must not resolve recipients")

        monkeypatch.setattr(env.backend, "resolve_chat", unexpected)
        assert await env.service.get_send_status(result.operation_id) == result
        assert await env.service.send_file("1", file.file_id, "CV", "key") == result
        assert len(env.telegram.send_calls) == 1
        env.telegram.account = "other-account"
        with pytest.raises(ToolError, match="OPERATION_UNAVAILABLE"):
            await env.service.get_send_status(result.operation_id)


async def test_configuration_authorization_requires_form_when_policy_tightens(tmp_path):
    settings = Settings(
        allow_send_messages=True,
        allow_all_write_chats=True,
        require_send_message_confirmation=False,
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        env.telegram.send_error = ToolError("CONNECTION_ERROR", "Response lost")
        result = await env.service.send_message("1", "Hello", "key")
        operation = await env.store.operation("101", "key")
        assert result.status == "unknown" and not operation.confirmed
        assert operation.confirmation_source == "config"
        telegram = env.telegram
    settings.require_send_message_confirmation = True
    telegram.send_error = None
    async with outbound_fixture(tmp_path, settings=settings, telegram=telegram) as env:
        denied = await env.service.send_message("1", "Hello", "key")
        assert denied.status == "unknown" and denied.error.code == "CONFIRMATION_UNSUPPORTED"
        assert len(telegram.send_calls) == 1
        recovered = await env.service.send_message("1", "Hello", "key", confirm=approve)
        operation = await env.store.operation("101", "key")
        assert recovered.status == "sent" and operation.confirmed
        assert operation.confirmation_source == "form"
        assert telegram.send_calls[0][0] == telegram.send_calls[1][0]


@pytest.mark.parametrize("mode", ["one_off", "all_chats"])
async def test_retry_cannot_switch_to_a_different_permission_basis(tmp_path, mode):
    settings = Settings(
        allow_send_messages=True,
        allow_one_off_sends=mode == "one_off",
        allow_all_write_chats=mode == "all_chats",
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        env.telegram.send_error = ToolError("CONNECTION_ERROR", "Response lost")
        original = await env.service.send_message("1", "Hello", "key", confirm=approve)
        env.telegram.send_error = None
        settings.allow_all_write_chats = settings.allow_one_off_sends = False
        settings.allowed_chat_ids = settings.allowed_write_chat_ids = ["1"]
        blocked = await env.service.send_message("1", "Hello", "key", confirm=approve)
        assert blocked.status == "unknown" and blocked.error.code == "WRITE_ACCESS_DENIED"
        assert blocked.operation_id == original.operation_id
        assert len(env.telegram.send_calls) == 1


async def test_optional_forms_preserve_flood_wait_across_restart(tmp_path):
    settings = Settings(
        allow_send_messages=True,
        allow_all_write_chats=True,
        require_send_message_confirmation=False,
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        env.telegram.send_error = ToolError("RATE_LIMITED", "Wait", 60)
        first = await env.service.send_message("1", "Hello", "key")
        telegram, clock = env.telegram, env.clock
    telegram.send_error = None
    async with outbound_fixture(tmp_path, settings=settings, telegram=telegram, clock=clock) as env:
        assert await env.service.send_message("1", "Hello", "key") == first
        assert len(telegram.send_calls) == 1
        clock.now += timedelta(seconds=61)
        assert (await env.service.send_message("1", "Hello", "key")).status == "sent"
        assert telegram.send_calls[0][0] == telegram.send_calls[1][0]


async def test_one_off_confirmation_pins_identity_and_parallel_replays(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    settings = Settings(allow_send_messages=True, allow_one_off_sends=True)
    forms = []

    async def confirm(description):
        forms.append(description)
        entered.set()
        await release.wait()
        return True

    async with outbound_fixture(tmp_path, settings=settings) as env:
        env.backend.chats[0].username = "recruiter"
        task = asyncio.create_task(
            env.service.send_message("@recruiter", "Hello", "key", confirm=confirm)
        )
        await entered.wait()
        env.backend.chats[0].username = "renamed"
        env.backend.chats.append(
            Chat(id="3", title="New owner", type="private", username="recruiter")
        )
        pending = await env.service.send_message("@recruiter", "Hello", "key", confirm=approve)
        assert pending.status == "pending" and not env.telegram.send_calls
        release.set()
        result = await task
        assert result.status == "sent" and result.chat_id == "1"
        assert len(forms) == 1 and "One-time permission" in forms[0]
        assert "Username: 'recruiter'; type: private" in forms[0]


@pytest.mark.parametrize("change", ["grant", "capability", "account", "type"])
async def test_one_off_revalidates_after_confirmation(tmp_path, change):
    settings = Settings(allow_send_messages=True, allow_one_off_sends=True)
    async with outbound_fixture(tmp_path, settings=settings) as env:

        async def confirm(description):
            if change == "grant":
                settings.allow_one_off_sends = False
            elif change == "capability":
                settings.allow_send_messages = False
            elif change == "account":
                env.telegram.account = "102"
            else:
                env.backend.chats[0].type = "bot"
            return True

        result = await env.service.send_message("1", "Hello", "key", confirm=confirm)
        assert result.status == "failed" and not env.telegram.send_calls


async def test_legacy_journal_record_reopens_with_original_hash_and_random_id(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        env.telegram.send_error = ToolError("CONNECTION_ERROR", "Response lost")
        first = await env.service.send_message("1", "Hello", "key", confirm=approve)
        operation = await env.store.operation("101", "key")
        legacy = operation.model_dump(
            mode="json",
            exclude={
                "recipient",
                "confirmation_source",
                "reply_selector",
                "reply_ref",
            },
        )
        with sqlite3.connect(env.store.path) as db:
            db.execute(
                "UPDATE operations SET value=? WHERE id=?",
                (json.dumps(legacy), operation.operation_id),
            )
        telegram = env.telegram
    telegram.send_error = None
    async with outbound_fixture(tmp_path, telegram=telegram) as env:
        restored = await env.store.operation("101", "key")
        assert restored.recipient is None and restored.confirmed
        assert restored.confirmation_source is None
        result = await env.service.send_message("1", "Hello", "key")
        assert result.status == "sent" and result.operation_id == first.operation_id
        saved = await env.store.operation("101", "key")
        assert (
            saved.request_hash == operation.request_hash and saved.random_id == operation.random_id
        )
        assert saved.confirmation_source == "form"


async def test_declined_operation_is_not_revived_by_disabling_forms(tmp_path):
    async with outbound_fixture(tmp_path) as env:

        async def decline(description):
            return False

        failed = await env.service.send_message("1", "Hello", "key", confirm=decline)
        env.settings.require_send_message_confirmation = False
        assert await env.service.send_message("1", "Hello", "key") == failed
        assert failed.error.code == "CONFIRMATION_DECLINED" and not env.telegram.send_calls
