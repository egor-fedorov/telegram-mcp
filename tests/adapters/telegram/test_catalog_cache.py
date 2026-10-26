"""Long-lived dialog snapshots never substitute for fresh access decisions."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import ToolError
from tests.support.catalog import dialog_page, gateway, simple_response
from tests.support.fakes import FakeBackend, msg


def calls(client, kind):
    return sum(isinstance(call.args[0], kind) for call in client.await_args_list)


async def test_large_catalog_reuses_after_31_seconds_and_refreshes_once_with_hard_age_limit():
    entered, release = asyncio.Event(), asyncio.Event()
    refreshing = False

    async def respond(request):
        high = request.offset_id - 1 if request.offset_id else 2000
        if refreshing and not request.offset_id:
            entered.set()
            await release.wait()
        page = dialog_page(list(range(high, max(0, high - 100), -1)), sliced=high > 100)
        if refreshing:
            for user in page.users:
                user.first_name = "Refreshed"
        return page

    chats, client, clock = gateway(respond)
    original, _ = await chats.catalog()
    assert len(original) == 2000 and calls(client, functions.messages.GetDialogsRequest) == 20
    clock.time += 31
    warm, _ = await chats.catalog()
    assert warm == original and calls(client, functions.messages.GetDialogsRequest) == 20
    clock.time += 269
    refreshing = True
    stale, _ = await chats.catalog()
    assert stale == original
    await asyncio.wait_for(entered.wait(), 1)
    task = chats.dialog_catalog._task
    for _ in range(3):
        assert (await chats.catalog())[0] == original
    assert chats.dialog_catalog._task is task
    assert calls(client, functions.messages.GetDialogsRequest) == 21
    assert calls(client, functions.messages.GetDialogFiltersRequest) == 6
    clock.time += 300
    chats.dialog_catalog.wait_seconds = 0.01
    with pytest.raises(ToolError, match="CATALOG_LOADING"):
        await chats.catalog()
    assert chats.dialog_catalog._task is task and task is not None
    release.set()
    await task
    updated, _ = await chats.catalog()
    assert len(updated) == 2000 and all(c.title == "Refreshed" for c in updated)
    assert calls(client, functions.messages.GetDialogsRequest) == 40
    await chats.close()


@pytest.mark.parametrize("change", ["excluded", "removed", "category", "filters_error"])
async def test_fresh_folder_decision_hides_saved_buffers_and_preserves_cursor(change):
    folder = types.DialogFilter(
        id=2,
        title=types.TextWithEntities("Allowed", []),
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
        non_contacts=True,
    )
    current = [folder]
    fail = False

    def definitions():
        if fail:
            raise ConnectionError()
        return current

    chats, client, _ = gateway(simple_response, folders=definitions)
    backend = FakeBackend([msg("1", 1), msg("1", 2)])
    backend.catalog = chats.catalog
    backend.check_authorization = chats.check_authorization
    service = build_service(backend, Settings(allowed_folder_ids=[2]))
    first = await service.get_history(chat_id="1", limit=1)
    assert first.next_cursor
    if change == "excluded":
        folder.exclude_peers = [types.InputPeerUser(1, 1)]
    elif change == "removed":
        current.clear()
    elif change == "category":
        folder.non_contacts = False
    else:
        fail = True
    with pytest.raises(ToolError, match="CONNECTION_ERROR" if fail else "ACCESS_DENIED"):
        await service.get_history(chat_id="1", limit=1, cursor=first.next_cursor)
    assert calls(client, functions.messages.GetDialogsRequest) == 1
    folder.exclude_peers = []
    folder.non_contacts = True
    current[:] = [folder]
    fail = False
    last = await service.get_history(chat_id="1", limit=1, cursor=first.next_cursor)
    assert [m.message_ref.message_id for m in last.messages] == [1]
    assert last.scope.complete
    await service.close()
    await chats.close()


@pytest.mark.parametrize("error", [ConnectionError(), errors.FloodWaitError(None, capture=10)])
async def test_background_failure_keeps_snapshot_and_never_retries_itself(error):
    failing = False

    def respond(request):
        if failing:
            raise error
        return dialog_page([1])

    chats, client, clock = gateway(respond)
    original = (await chats.catalog())[0]
    failing = True
    clock.time += 300
    assert (await chats.catalog())[0] == original
    task = chats.dialog_catalog._task
    assert task is not None
    await task
    assert chats.dialog_catalog._snapshot is not None
    before = calls(client, functions.messages.GetDialogsRequest)
    await asyncio.sleep(0)
    assert calls(client, functions.messages.GetDialogsRequest) == before
    if isinstance(error, errors.FloodWaitError):
        for step in (0.1, 0.4, 8.6):
            clock.time += step
            assert (await chats.catalog())[0] == original
            assert calls(client, functions.messages.GetDialogsRequest) == before
    clock.time += 300
    with pytest.raises(ToolError, match="RATE_LIMITED|CONNECTION_ERROR"):
        await chats.catalog()
    await chats.close()


async def test_rpc_timeout_does_not_invalidate_usable_snapshot():
    chats, client, _ = gateway(simple_response)
    await chats.catalog()
    generation = chats.runtime.generation
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await chats.runtime.call(AsyncMock(side_effect=TimeoutError())())
    assert chats.runtime.generation == generation
    await chats.catalog()
    assert calls(client, functions.messages.GetDialogsRequest) == 1
    await chats.close()


async def test_invalid_completed_refresh_does_not_replace_valid_snapshot():
    malformed = False

    def respond(request):
        page = dialog_page([1])
        if malformed:
            page.dialogs[0].unread_count = None
        return page

    chats, _, clock = gateway(respond)
    original = (await chats.catalog())[0]
    malformed = True
    clock.time += 300
    assert (await chats.catalog())[0] == original
    task = chats.dialog_catalog._task
    assert task is not None
    await task
    assert (await chats.catalog())[0] == original
    clock.time += 300
    with pytest.raises(ToolError, match="CATALOG_ERROR"):
        await chats.catalog()
    await chats.close()


async def test_zero_grace_blocks_old_snapshot_while_refresh_continues():
    entered, release = asyncio.Event(), asyncio.Event()
    block = False

    async def respond(request):
        if block:
            entered.set()
            await release.wait()
        return dialog_page([1])

    chats, _, clock = gateway(respond, stale=0)
    await chats.catalog()
    block = True
    clock.time += 300
    chats.dialog_catalog.wait_seconds = 0.01
    with pytest.raises(ToolError, match="CATALOG_LOADING"):
        await chats.catalog()
    assert entered.is_set()
    task = chats.dialog_catalog._task
    assert task is not None and not task.done()
    release.set()
    await task
    assert len((await chats.catalog())[0]) == 1
    await chats.close()


async def test_missing_auxiliary_metadata_resumes_after_floodwait_without_rescan():
    folder = types.DialogFilter(
        id=2,
        title=types.TextWithEntities("Saved and muted", []),
        pinned_peers=[],
        include_peers=[types.InputPeerSelf()],
        exclude_peers=[],
        exclude_muted=True,
    )
    failed = False

    def respond(request):
        nonlocal failed
        if isinstance(request, functions.account.GetNotifySettingsRequest):
            if not failed:
                failed = True
                raise errors.FloodWaitError(request, capture=10)
            return types.PeerNotifySettings()
        return dialog_page([1])

    chats, client, clock = gateway(respond, folders=[folder])
    client.get_me.return_value = types.InputPeerUser(99, 99)
    with pytest.raises(ToolError, match="RATE_LIMITED"):
        await chats.catalog()
    for step in (0.1, 0.4, 8.6):
        clock.time += step
        with pytest.raises(ToolError, match="RATE_LIMITED"):
            await chats.catalog()
        assert calls(client, functions.account.GetNotifySettingsRequest) == 1
    clock.time = 110
    _, folders = await chats.catalog()
    assert folders[0].included_chat_ids == ["99"]
    assert calls(client, functions.account.GetNotifySettingsRequest) == 4
    assert calls(client, functions.messages.GetDialogsRequest) == 1
    client.get_me.assert_awaited_once()
    await chats.close()


@pytest.mark.parametrize("revoke", [False, True])
async def test_shutdown_or_revocation_cancels_refresh_without_publishing(revoke):
    entered = asyncio.Event()
    block = False

    async def respond(request):
        if block:
            entered.set()
            await asyncio.Event().wait()
        return dialog_page([1])

    chats, client, clock = gateway(respond)
    await chats.catalog()
    clock.time += 300
    block = True
    await chats.catalog()
    await asyncio.wait_for(entered.wait(), 1)
    task = chats.dialog_catalog._task
    assert task is not None
    if revoke:
        client.side_effect = errors.SessionRevokedError(None)
        with pytest.raises(ToolError, match="AUTH_REQUIRED"):
            await chats.catalog()
    await chats.close()
    assert task.done() and chats.cache == {} and chats.dialog_catalog._snapshot is None
