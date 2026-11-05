"""Preflight metadata and verification never grant access to a recipient's history."""

from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Attachment, MessageRef, ToolError
from telegram_mcp.domain.outbound import text_digest
from tests.support.fakes import msg
from tests.support.outbound import approve, outbound_fixture


@pytest.mark.parametrize("mode", ["allowlist", "one_off", "all_chats"])
async def test_resolve_reports_effective_policy_without_reading_messages(tmp_path, mode):
    settings = Settings(
        allow_send_messages=True,
        require_send_message_confirmation=False,
        allow_one_off_sends=mode == "one_off",
        allow_all_write_chats=mode == "all_chats",
        allowed_write_chat_ids=["1"] if mode == "allowlist" else [],
        allowed_chat_ids=["1"] if mode == "allowlist" else [],
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        env.backend.chats[0].username = "recruiter"
        await env.store.set_cooldown("101", "send:1", env.clock.now + timedelta(seconds=23))
        result = await env.service.resolve_recipient("https://t.me/recruiter")
        assert result.chat_id == "1" and result.username == "recruiter"
        assert result.send_message.allowed and result.send_message.grant == mode
        assert result.send_message.confirmation_required is (mode == "one_off")
        assert not result.send_file.allowed and result.send_file.error.code == "WRITES_DISABLED"
        assert result.retry_after_seconds == 23
        assert not env.backend.reads and not env.backend.fetches
        assert not env.telegram.send_calls and not await env.store.journal()
        if mode != "allowlist":
            assert "catalog" not in env.backend.backend_calls


async def test_resolve_reports_denial_but_does_not_hide_connection_failures(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        env.settings.allowed_write_chat_ids = []
        result = await env.service.resolve_recipient("1")
        assert not result.send_message.allowed
        assert result.send_message.error.code == "WRITE_ACCESS_DENIED"
        env.backend.authorization_fault = ToolError("CONNECTION_ERROR", "Offline")
        with pytest.raises(ToolError, match="CONNECTION_ERROR"):
            await env.service.resolve_recipient("1")


@pytest.mark.parametrize("mode", ["allowlist", "one_off", "all_chats"])
async def test_verify_exact_own_message_after_restart_and_permission_revocation(tmp_path, mode):
    settings = Settings(
        allow_send_messages=True,
        allow_one_off_sends=mode == "one_off",
        allow_all_write_chats=mode == "all_chats",
        allowed_write_chat_ids=["1"] if mode == "allowlist" else [],
        allowed_chat_ids=["1"] if mode == "allowlist" else [],
    )
    text = "Résumé 📄 — synthetic text"
    async with outbound_fixture(tmp_path, settings=settings) as env:
        receipt = await env.service.send_message("1", text, "key", confirm=approve)
        operation = await env.store.operation("101", "key")
        assert operation.payload == {} and operation.expected_text_sha256 == text_digest(text)
        telegram = env.telegram
    settings.allow_send_messages = False
    settings.allow_one_off_sends = settings.allow_all_write_chats = False
    settings.allowed_chat_ids = settings.allowed_write_chat_ids = []
    async with outbound_fixture(tmp_path, settings=settings, telegram=telegram) as env:
        own = msg("1", receipt.message_ref.message_id, text)
        own.outgoing = True
        env.backend.stored_messages = [own, msg("1", 999, "Other person's reply")]
        checked = await env.service.verify_sent_message(receipt.operation_id)
        assert checked.verification == "verified" and checked.message.text == text
        assert checked.receipt == receipt and checked.message.outgoing
        assert checked.text_matches and checked.attachment_metadata_matches
        assert not checked.attachment_bytes_verified
        assert env.backend.reads == [receipt.message_ref.key]
        assert not env.backend.fetches and "catalog" not in env.backend.backend_calls
        history = await env.service.get_messages(["1:999"])
        assert not history.messages and history.issues[0].code == "ACCESS_DENIED"
        env.telegram.account = "another"
        with pytest.raises(ToolError, match="OPERATION_UNAVAILABLE"):
            await env.service.verify_sent_message(receipt.operation_id)
        assert len(env.backend.reads) == 1


@pytest.mark.parametrize(
    "change", ["text", "missing", "network", "incoming", "filtered", "edited_auth", "legacy"]
)
async def test_verification_distinguishes_receipt_from_observation(tmp_path, change):
    async with outbound_fixture(tmp_path) as env:
        receipt = await env.service.send_message("1", "Expected", "key", confirm=approve)
        own = msg(
            "1", receipt.message_ref.message_id, "Changed" if change == "text" else "Expected"
        )
        own.outgoing = change != "incoming"
        if change == "filtered":
            own.omission_reason = "AUTH_NOTIFICATION_FILTERED"
        if change == "edited_auth":
            own.text = "Login code: 12345. Do not give this code to anyone."
        env.backend.stored_messages = [] if change == "missing" else [own]
        if change == "network":
            env.backend.message_faults["1"] = ToolError("CONNECTION_ERROR", "Offline")
        if change == "legacy":
            operation = await env.store.operation("101", "key")
            operation.expected_text_sha256 = None
            await env.store.save_operation(operation)
        checked = await env.service.verify_sent_message(receipt.operation_id)
        assert checked.receipt.status == "sent"
        if change == "text":
            assert checked.verification == "mismatch" and checked.text_matches is False
            assert checked.message.text == "Changed"
        elif change == "legacy":
            assert checked.verification == "read_back" and checked.text_matches is None
        else:
            assert checked.verification == "unavailable" and checked.message is None
            assert checked.issues
        assert await env.service.get_send_status(idempotency_key="key") == receipt
        assert len(env.telegram.send_calls) == 1


@pytest.mark.parametrize("change", ["wrong_peer", "wrong_id", "account"])
async def test_mismatched_telegram_response_never_exposes_content(tmp_path, change, monkeypatch):
    async with outbound_fixture(tmp_path) as env:
        receipt = await env.service.send_message("1", "Expected", "key", confirm=approve)

        async def read(ref):
            if change == "account":
                env.telegram.account = "other"
            own = msg(
                "2" if change == "wrong_peer" else "1",
                99 if change == "wrong_id" else ref.message_id,
                "Hidden",
            )
            own.outgoing = True
            return own

        monkeypatch.setattr(env.backend, "message", read)
        checked = await env.service.verify_sent_message(receipt.operation_id)
        assert checked.verification == "unavailable" and checked.message is None
        assert "Hidden" not in checked.model_dump_json()


async def test_unknown_send_is_not_guessed_from_history(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        env.telegram.send_error = ToolError("CONNECTION_ERROR", "Lost response")
        receipt = await env.service.send_message("1", "Expected", "key", confirm=approve)
        checked = await env.service.verify_sent_message(receipt.operation_id)
        assert receipt.status == "unknown" and checked.verification == "not_checked"
        assert checked.checked_at is None and not env.backend.reads


@pytest.mark.parametrize("metadata", ["same", "changed", "unknown"])
async def test_file_verification_compares_caption_and_metadata_without_downloading(
    tmp_path, metadata
):
    async with outbound_fixture(tmp_path) as env:
        file = await env.store.register(
            "101", "cv.pdf", "application/pdf", b"Synthetic", env.clock.now
        )
        receipt = await env.service.send_file("1", file.file_id, "CV 📄", "key", confirm=approve)
        await env.store.remove_file(file.file_id)
        own = msg("1", receipt.message_ref.message_id, "CV 📄")
        own.outgoing = True
        own.attachments = [
            Attachment(
                id="not-exposed",
                type="document",
                name="cv.pdf",
                mime_type="application/pdf",
                size=None if metadata == "unknown" else file.size + int(metadata == "changed"),
            )
        ]
        env.backend.stored_messages = [own]
        result = await env.service.verify_sent_message(receipt.operation_id)
        assert result.text_matches and not result.attachment_bytes_verified
        assert (
            result.verification
            == {"same": "verified", "changed": "mismatch", "unknown": "read_back"}[metadata]
        )
        assert "not-exposed" not in result.model_dump_json()


async def test_corrupt_receipt_cannot_select_another_chat(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        receipt = await env.service.send_message("1", "Expected", "key", confirm=approve)
        operation = await env.store.operation("101", "key")
        operation.receipt.message_ref = MessageRef(chat_id="2", message_id=10)
        await env.store.save_operation(operation)
        with pytest.raises(ToolError, match="OPERATION_UNAVAILABLE"):
            await env.service.verify_sent_message(receipt.operation_id)
        assert not env.backend.reads
