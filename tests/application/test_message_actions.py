"""Single-target permissions, confirmation races and durable action recovery."""

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from telegram_mcp.adapters.config import Settings, load_settings, save_settings
from telegram_mcp.domain.changes import content_revision
from telegram_mcp.domain.message_actions import ActionPolicy, ActionRule
from telegram_mcp.domain.models import MessageRef, ToolError
from tests.support.fakes import msg
from tests.support.outbound import approve, outbound_fixture


def rule(**overrides):
    values = dict(
        chat_ids=["1"], targets=["own"], actions=["edit", "delete_for_me", "delete_for_everyone"]
    )
    return ActionRule.model_validate(values | overrides)


def enable(env, *rules, confirmation="per_rule"):
    env.settings.message_actions = ActionPolicy(confirmation=confirmation, rules=rules)


def seed(env, cid="1", mid=10, text="Original message", outgoing=True):
    message = msg(cid, mid, text)
    message.chat = env.backend.chats[0].model_copy(update={"id": cid})
    message.outgoing = outgoing
    message.revision = content_revision(message)
    env.telegram.action_messages[message.message_ref.key] = message
    return message


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"all_chats": False},
        {"all_chats": "true"},
        {"all_chats": True, "folder_ids": [1]},
        {"chat_ids": ["777000"]},
        {"chat_ids": ["٠١"]},
        {"folder_ids": [True]},
    ],
)
def test_invalid_selectors(scope):
    with pytest.raises(ValidationError):
        ActionRule.model_validate({**scope, "targets": ["own"], "actions": ["delete_for_me"]})


def test_local_policy_round_trip_and_specificity(tmp_path):
    rules = (
        rule(chat_ids=[], all_chats=True, require_confirmation=True),
        rule(chat_ids=[], folder_ids=[7], require_confirmation=False),
        rule(actions=["edit"], require_confirmation=True),
        rule(actions=["edit"], require_confirmation=False),
        rule(targets=["private_incoming"], actions=["delete_for_everyone"]),
    )
    settings = Settings(message_actions=ActionPolicy(rules=rules))
    p = tmp_path / "local.toml"
    save_settings(settings, p)
    local = load_settings(p).message_actions
    assert local == settings.message_actions
    assert local.grants("1", {7}, {"own"}, {"edit", "delete_for_me"}) == {
        "edit": True,
        "delete_for_me": False,
    }
    assert local.grants("2", set(), {"private_incoming"}, {"delete_for_everyone"}) == {}
    assert not Settings().write_policy().enabled


async def test_receipt_only_grant_and_original_send_survives(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        sent = await env.service.send_message("1", "Original message", "send", confirm=approve)
        assert sent.message_ref
        item = seed(env, mid=sent.message_ref.message_id)
        env.settings.allowed_chat_ids = []
        enable(env, rule(targets=["mcp_sent"], actions=["delete_for_me"]))
        preview = await env.service.preview_message_action(source_operation_id=sent.operation_id)
        assert [a.action for a in preview.actions] == ["delete_for_me"]
        assert preview.actions[0].confirmation_required
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.preview_message_action(message_ref=item.message_ref.key)

        async def consent(prompt):
            assert item.message_ref.key in prompt and "delete_for_me" in prompt
            return True

        receipt = await env.service.delete_message(
            "for_me",
            preview.revision,
            "remove",
            source_operation_id=sent.operation_id,
            confirm=consent,
        )
        assert (
            receipt.status == "completed"
            and receipt.rpc_accepted
            and receipt.visibility == "for_me"
        )
        assert receipt.source_operation_id == sent.operation_id
        replay = await env.service.delete_message(
            "for_me", preview.revision, "remove", source_operation_id=sent.operation_id
        )
        assert replay == receipt and len(env.telegram.action_calls) == 1
        with pytest.raises(ToolError, match="IDEMPOTENCY_CONFLICT"):
            await env.service.delete_message(
                "for_everyone", preview.revision, "remove", source_operation_id=sent.operation_id
            )
        assert (await env.service.get_send_status(sent.operation_id)).status == "sent"
        assert (await env.service.get_send_status(idempotency_key="remove")) == receipt
        summaries = (await env.service.list_operations(kind="delete")).operations
        assert [(s.message_ref, s.source_operation_id, s.action) for s in summaries] == [
            (sent.message_ref, sent.operation_id, "delete_for_me")
        ]
        saved = (await env.store.operation("101", "remove")).model_dump_json()
        assert "Original message" not in saved
        enable(env, rule(targets=["own"]))
        with pytest.raises(ToolError, match="WRITE_ACCESS_DENIED"):
            await env.service.preview_message_action(source_operation_id=sent.operation_id)


@pytest.mark.parametrize(
    "change,code",
    [
        ("folder", "ACCESS_DENIED"),
        ("rights", "ACTION_NOT_ALLOWED"),
        ("revision", "MESSAGE_CONFLICT"),
        ("account", "ACCOUNT_CHANGED"),
        ("rules", "WRITE_ACCESS_DENIED"),
        ("decline", "CONFIRMATION_DECLINED"),
    ],
)
async def test_recheck_after_form(tmp_path, change, code):
    async with outbound_fixture(tmp_path) as env:
        item = seed(env)
        env.settings.allowed_chat_ids = []
        env.settings.allowed_folder_ids = [7]
        env.telegram.folder_ids = {7}
        enable(env, rule(chat_ids=[], folder_ids=[7]))
        preview = await env.service.preview_message_action(message_ref=item.message_ref.key)

        async def confirm(prompt):
            if change == "folder":
                env.telegram.folder_ids.clear()
            if change == "rights":
                env.telegram.supported_actions.clear()
            if change == "revision":
                item.revision = "1" * 64
            if change == "account":
                env.telegram.account = "202"
            if change == "rules":
                enable(env, rule(chat_ids=["2"]))
            return change != "decline"

        receipt = await env.service.edit_message(
            "Replacement",
            preview.revision,
            "edit",
            message_ref=item.message_ref.key,
            confirm=confirm,
        )
        assert receipt.status == "failed" and receipt.error.code == code
        assert not env.telegram.action_calls
        assert "Replacement" not in (await env.store.operation("101", "edit")).model_dump_json()


async def test_uncertain_edit_and_pending_dispatch_survive_restart(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        enable(env, rule(), confirmation="never")
        item = seed(env)
        env.telegram.action_error = ToolError("CONNECTION_ERROR", "Connection lost.")
        receipt = await env.service.edit_message(
            "New text", item.revision, "lost", message_ref="1:10"
        )
        assert receipt.status == "unknown" and not receipt.rpc_accepted
        op = await env.store.operation("101", "lost")
        assert "New text" not in op.model_dump_json()
        # Simulate process termination after the pre-dispatch write but before outcome persistence.
        op.receipt.status = "pending"
        await env.store.save_operation(op)
    async with outbound_fixture(tmp_path) as other:
        enable(other, rule(), confirmation="never")
        replay = await other.service.edit_message(
            "New text", item.revision, "lost", message_ref="1:10"
        )
        assert replay.status == "unknown" and replay.operation_id == receipt.operation_id
        assert (await other.service.list_operations(status="unknown")).operations[0].kind == "edit"
        assert not other.telegram.action_calls


async def test_flood_wait_keeps_receipt_and_delays_retry(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        enable(env, rule(), confirmation="never")
        item = seed(env)
        env.telegram.action_error = ToolError("RATE_LIMITED", "Wait.", 10)

        async def delete():
            return await env.service.delete_message(
                "for_everyone", item.revision, "wait", message_ref="1:10"
            )

        first = await delete()
        assert first.status == "pending" and first.retry_at == env.clock.utc_now() + timedelta(
            seconds=10
        )
        assert await delete() == first and len(env.telegram.action_calls) == 1
        env.telegram.action_error = None
        env.clock.now += timedelta(seconds=11)
        final = await delete()
        assert final.status == "completed" and final.operation_id == first.operation_id


async def test_different_keys_same_revision_cannot_both_edit(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        enable(env, rule())
        item = seed(env)
        revision = item.revision
        barrier = asyncio.Barrier(2)

        async def confirm(prompt):
            await barrier.wait()
            return True

        async def edit(key):
            return await env.service.edit_message(
                key, revision, key, message_ref=item.message_ref.key, confirm=confirm
            )

        results = await asyncio.gather(edit("first"), edit("second"))
        assert sorted(r.status for r in results) == ["completed", "failed"]
        assert next(r.error.code for r in results if r.error) == "MESSAGE_CONFLICT"
        assert len(env.telegram.action_calls) == 1


async def test_auth_notice_wrong_peer_and_outgoing_provenance(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        enable(env, rule())
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.preview_message_action(message_ref="777000:1")
        item = seed(env, text="Your login code: 12345")
        assert item.omission_reason
        with pytest.raises(ToolError, match="AUTH_NOTIFICATION_FILTERED"):
            await env.service.delete_message("for_me", "1" * 64, "filtered", message_ref="1:10")
        assert await env.store.operation("101", "filtered") is None
        item = seed(env)
        sent = await env.service.send_message("1", item.text, "send", confirm=approve)
        item.outgoing = False
        with pytest.raises(ToolError, match="MESSAGE_UNAVAILABLE"):
            await env.service.preview_message_action(source_operation_id=sent.operation_id)
        item.message_ref = MessageRef(chat_id="2", message_id=10)
        with pytest.raises(ToolError, match="MESSAGE_UNAVAILABLE"):
            await env.service.preview_message_action(message_ref="1:10")


async def test_public_read_or_send_grant_alone_does_not_allow_actions(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        env.settings.allow_public_channel_reads = True
        env.settings.allow_all_write_chats = True
        item = seed(env)
        with pytest.raises(ToolError, match="WRITES_DISABLED"):
            await env.service.preview_message_action(message_ref="1:10")
        enable(env, rule(chat_ids=[], all_chats=True))
        env.settings.allowed_chat_ids.clear()
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.preview_message_action(message_ref=item.message_ref.key)


async def test_preflight_floodwait_is_shared_between_preview_and_write(tmp_path, monkeypatch):
    async with outbound_fixture(tmp_path) as env:
        enable(env, rule())
        item = seed(env)
        calls = 0

        async def limited(ref, account):
            nonlocal calls
            calls += 1
            raise ToolError("RATE_LIMITED", "Wait.", 18)

        monkeypatch.setattr(env.telegram, "action_target", limited)
        with pytest.raises(ToolError, match="RATE_LIMITED"):
            await env.service.preview_message_action(message_ref="1:10")
        with pytest.raises(ToolError, match="RATE_LIMITED"):
            await env.service.edit_message("New", item.revision, "wait-first", message_ref="1:10")
        assert calls == 1
        assert await env.store.operation("101", "wait-first") is None


@pytest.mark.parametrize(
    "confirmation,expected", [("always", True), ("never", False), ("per_rule", True)]
)
def test_global_form_overrides_and_ties(confirmation, expected):
    policy = ActionPolicy(
        confirmation=confirmation,
        rules=(rule(require_confirmation=False), rule(require_confirmation=True)),
    )
    assert policy.grants("1", set(), {"own"}, {"edit"}) == {"edit": expected}
