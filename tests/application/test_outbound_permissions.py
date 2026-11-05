"""Local recipient and confirmation policies never expand retrieval permissions."""

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Chat, MessageButton, ReplyMarkup, ToolError
from telegram_mcp.domain.outbound import Draft
from tests.support.fakes import msg
from tests.support.outbound import approve, outbound_fixture


@pytest.mark.parametrize("mode", ["allowlist", "one_off", "all_chats"])
@pytest.mark.parametrize("kind", ["private", "bot", "group", "channel", "saved"])
async def test_recipient_modes_and_types(tmp_path, mode, kind):
    settings = Settings(
        allow_send_messages=True,
        allow_one_off_sends=mode == "one_off",
        allow_all_write_chats=mode == "all_chats",
        allowed_write_chat_ids=["1"] if mode == "allowlist" else [],
        allowed_chat_ids=["1"] if mode == "allowlist" else [],
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        env.backend.chats[0] = Chat(id="1", title="Recipient", type=kind, username="recruiter")
        if mode == "one_off" and kind not in ("private", "bot"):
            with pytest.raises(ToolError, match="WRITE_ACCESS_DENIED"):
                await env.service.send_message("@recruiter", "Hello", "key", confirm=approve)
            assert not env.telegram.send_calls
            return
        result = await env.service.send_message("@recruiter", "Hello", "key", confirm=approve)
        assert result.status == "sent" and result.chat_id == "1"
        operation = await env.store.operation("101", "key")
        assert operation.recipient.grant == mode
        assert settings.allowed_write_chat_ids == (["1"] if mode == "allowlist" else [])
        if mode != "allowlist":
            assert "catalog" not in env.backend.backend_calls
            with pytest.raises(ToolError, match="ACCESS_DENIED"):
                await env.service.get_history(chat_ref="1")


@pytest.mark.parametrize("mode", ["one_off", "all_chats"])
@pytest.mark.parametrize("blocked", ["service", "forum"])
async def test_broad_grants_do_not_override_exclusions(tmp_path, mode, blocked):
    settings = Settings(
        allow_send_messages=True,
        allow_one_off_sends=mode == "one_off",
        allow_all_write_chats=mode == "all_chats",
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        chat = Chat(
            id="777000" if blocked == "service" else "1",
            title="Unavailable",
            type="private" if blocked == "service" else "group",
            is_forum=blocked == "forum",
        )
        env.backend.chats = [chat]
        with pytest.raises(ToolError, match="ACCESS_DENIED|WRITE_UNSUPPORTED"):
            await env.service.send_message(chat.id, "Hello", "key", confirm=approve)
        assert not env.telegram.send_calls


@pytest.mark.parametrize("mode", ["one_off", "all_chats"])
async def test_broad_grants_still_require_the_action_flag(tmp_path, mode):
    settings = Settings(
        allow_one_off_sends=mode == "one_off", allow_all_write_chats=mode == "all_chats"
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        with pytest.raises(ToolError, match="WRITES_DISABLED"):
            await env.service.send_message("1", "Hello", "key", confirm=approve)
        assert not env.service.writes_enabled and not env.telegram.send_calls


async def test_one_off_send_forces_confirmation_despite_optional_forms(tmp_path):
    settings = Settings(
        allow_send_messages=True,
        allow_one_off_sends=True,
        require_send_message_confirmation=False,
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        result = await env.service.send_message("1", "Hello", "key")
        assert result.error.code == "CONFIRMATION_UNSUPPORTED"
        assert not env.telegram.send_calls
        assert (await env.service.get_status())["confirmation_required"] is True


@pytest.mark.parametrize("mode", ["one_off", "all_chats"])
async def test_reply_requires_read_access_before_fetching_message(tmp_path, mode):
    settings = Settings(
        allow_send_messages=True,
        allow_one_off_sends=mode == "one_off",
        allow_all_write_chats=mode == "all_chats",
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        result = await env.service.send_message(
            "1", "Reply", "key", reply_to="1:7", confirm=approve
        )
        assert result.error.code == "ACCESS_DENIED" and not env.telegram.send_calls
        assert "message" not in env.backend.backend_calls


@pytest.mark.parametrize("action", ["draft", "callback"])
async def test_global_writes_do_not_grant_reads_for_stateful_actions(tmp_path, action):
    settings = Settings(allow_all_write_chats=True, allow_drafts=True, allow_bot_callbacks=True)
    async with outbound_fixture(tmp_path, settings=settings) as env:
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            if action == "draft":
                await env.service.create_draft("1", "Draft")
            else:
                await env.service.invoke_button("2:7", "button", "rev", "key", approve)
        assert not env.telegram.drafts and not env.telegram.callback_calls


def button(env):
    env.telegram.button_message = msg("2", 7).model_copy(
        update={
            "revision": "one",
            "reply_markup": ReplyMarkup(
                type="inline",
                rows=[[MessageButton(button_id="opaque", type="callback", text="Details")]],
            ),
        }
    )


@pytest.mark.parametrize("disabled", ["message", "file", "callback", "draft"])
async def test_confirmation_switches_are_independent(tmp_path, disabled):
    async with outbound_fixture(tmp_path) as env:
        flag = {
            "message": "require_send_message_confirmation",
            "file": "require_send_file_confirmation",
            "callback": "require_bot_callback_confirmation",
            "draft": "require_draft_overwrite_confirmation",
        }[disabled]
        setattr(env.settings, flag, False)
        env.settings.allow_all_write_chats = True
        env.settings.allowed_write_chat_ids = []
        path = tmp_path / "cv.txt"
        path.write_text("Synthetic CV")
        file = await env.service.register_file(path=str(path))
        button(env)
        env.telegram.drafts["1"] = Draft(chat_id="1", text="User", revision="old", exists=True)
        results = {
            "message": await env.service.send_message("1", "Hello", "message"),
            "file": await env.service.send_file("1", file.file_id, "CV", "file"),
            "callback": await env.service.invoke_button("2:7", "opaque", "one", "callback"),
        }
        for kind, result in results.items():
            expected = (
                ("completed" if kind == "callback" else "sent") if kind == disabled else "failed"
            )
            assert result.status == expected
            operation = await env.store.operation("101", kind)
            if kind == disabled:
                assert not operation.confirmed and operation.confirmation_source == "config"
            else:
                assert result.error.code == "CONFIRMATION_UNSUPPORTED"
        if disabled == "draft":
            result = await env.service.update_draft("1", "New", "old")
            assert result.text == "New" and result.owned
            with pytest.raises(ToolError, match="DRAFT_CONFLICT"):
                await env.service.update_draft("1", "Stale", "old")
        else:
            with pytest.raises(ToolError, match="CONFIRMATION_UNSUPPORTED"):
                await env.service.update_draft("1", "New", "old")


async def test_global_draft_listing_is_bounded_by_read_access(tmp_path):
    settings = Settings(allow_drafts=True, allow_all_write_chats=True, allowed_chat_ids=["1", "2"])
    async with outbound_fixture(tmp_path, settings=settings) as env:
        env.backend.chats.append(Chat(id="3", title="Denied", type="private"))
        first = await env.service.list_drafts(limit=1)
        second = await env.service.list_drafts(limit=1, cursor=first.next_cursor)
        assert [d.chat_id for d in first.drafts + second.drafts] == ["1", "2"]
        assert second.next_cursor is None and not first.issues and not second.issues


async def test_callback_without_form_keeps_revision_and_uncertainty_guards(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        env.settings.require_bot_callback_confirmation = False
        button(env)
        with pytest.raises(ToolError, match="MESSAGE_CONFLICT"):
            await env.service.invoke_button("2:7", "opaque", "stale", "stale")
        env.telegram.callback_error = ToolError("CONNECTION_ERROR", "Lost response")
        result = await env.service.invoke_button("2:7", "opaque", "one", "key")
        assert result.status == "unknown"
        assert await env.service.invoke_button("2:7", "opaque", "one", "key") == result
        assert env.telegram.callback_calls == 1


async def test_allowlisted_recipient_retains_read_requirement(tmp_path):
    settings = Settings(
        allow_send_messages=True, allow_one_off_sends=True, allowed_write_chat_ids=["1"]
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.send_message("1", "Hello", "key", confirm=approve)


@pytest.mark.parametrize(
    "global_writes,one_off,expected",
    [(False, False, False), (False, True, True), (True, True, False)],
)
async def test_status_reports_effective_confirmation_summary(
    tmp_path, global_writes, one_off, expected
):
    settings = Settings(
        allow_send_messages=True,
        allow_send_files=True,
        allow_drafts=True,
        allow_bot_callbacks=True,
        allow_all_write_chats=global_writes,
        allow_one_off_sends=one_off,
        require_send_message_confirmation=False,
        require_send_file_confirmation=False,
        require_bot_callback_confirmation=False,
        require_draft_overwrite_confirmation=False,
    )
    async with outbound_fixture(tmp_path, settings=settings) as env:
        status = await env.service.get_status()
        assert status["confirmation_required"] is expected
        assert status["mode"] == "read_write"


@pytest.mark.parametrize("kind", ["file", "callback", "draft"])
async def test_no_form_policy_does_not_enable_disabled_actions(tmp_path, kind):
    async with outbound_fixture(tmp_path) as env:
        env.settings.allow_all_write_chats = True
        env.settings.allowed_write_chat_ids = []
        env.settings.require_send_file_confirmation = False
        env.settings.require_bot_callback_confirmation = False
        env.settings.require_draft_overwrite_confirmation = False
        env.settings.allow_send_files = env.settings.allow_bot_callbacks = (
            env.settings.allow_drafts
        ) = False
        with pytest.raises(ToolError, match="WRITES_DISABLED"):
            if kind == "file":
                await env.service.send_file("1", "file", "CV", "key")
            elif kind == "callback":
                await env.service.invoke_button("2:7", "opaque", "one", "key")
            else:
                await env.service.create_draft("1", "Draft")
        assert (
            not env.telegram.send_calls
            and not env.telegram.callback_calls
            and not env.telegram.drafts
        )
