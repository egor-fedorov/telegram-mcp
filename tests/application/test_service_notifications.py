"""Telegram service notifications (777000) deliver login codes and are never readable."""

from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Chat, Folder, ToolError
from tests.support.fakes import BASE, FakeBackend, msg
from tests.support.incremental import incremental_fixture

SERVICE = "777000"
FROM, TO = BASE, BASE + timedelta(days=7)


def backend_with_service_chat() -> FakeBackend:
    chats = [
        Chat(id="1", title="Jobs", type="group"),
        Chat(id=SERVICE, title="Telegram", type="private"),
    ]
    messages = [msg("1", 1), msg(SERVICE, 2)]
    for message, chat in zip(messages, chats, strict=True):
        message.chat = chat
    folder = Folder(id=7, title="All", position=0, included_chat_ids=["1", SERVICE])
    return FakeBackend(messages, chats=chats, folders=[folder])


async def test_service_notifications_are_never_readable() -> None:
    backend = FakeBackend([msg("1", 1), msg(SERVICE, 2)])
    service = build_service(backend, Settings(allow_all_chats=True))
    listed = await service.list_chats()
    assert [chat.id for chat in listed.chats] == ["1"]
    page = await service.search_messages(query="budget")
    assert [m.chat.id for m in page.messages] == ["1"]
    history = await service.search_messages(query="budget", chat_ids=["1"])
    assert [m.chat.id for m in history.messages] == ["1"]
    result = await service.get_messages([f"{SERVICE}:2"])
    assert not result.messages and result.issues[0].code == "ACCESS_DENIED"
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_chat(SERVICE)
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.search_messages(query="budget", chat_ids=[SERVICE])
    assert f"{SERVICE}:2" not in backend.reads
    await service.close()


@pytest.mark.parametrize(
    "settings",
    [Settings(allow_all_chats=True), Settings(allowed_folder_ids=[7])],
    ids=["all-chats", "folder"],
)
async def test_history_selectors_exclude_or_deny_the_service_chat(settings) -> None:
    backend = backend_with_service_chat()
    service = build_service(backend, settings)
    folder = await service.get_history(folder_ids=[7], date_from=FROM, date_to=TO)
    assert folder.scope.chat_ids == ["1"]
    assert {m.chat.id for m in folder.messages} == {"1"}
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_history(chat_id=SERVICE)
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_history(chat_ids=[SERVICE], date_from=FROM, date_to=TO)
    assert SERVICE not in backend.fetches
    await service.close()


async def test_monitoring_and_exports_deny_the_service_chat(tmp_path) -> None:
    settings = Settings(allow_all_chats=True, allow_attachment_downloads=True)
    async with incremental_fixture(
        tmp_path, backend=backend_with_service_chat(), settings=settings
    ) as env:
        for call in (
            lambda: env.service.create_watch("codes", chat_refs=[SERVICE]),
            lambda: env.service.get_changes(chat_ref=SERVICE),
            lambda: env.service.export_history(chat_ref=SERVICE),
            lambda: env.service.download_attachment(f"{SERVICE}:2:document"),
        ):
            with pytest.raises(ToolError, match="ACCESS_DENIED"):
                await call()
        info = await env.service.create_watch("all", folder_ids=[7])
        assert info.source_count == 1
