"""Bounded raw catalog loading, immutable snapshots, and fail-closed refreshes."""

import asyncio
import inspect
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors, functions, types

from telegram_mcp.adapters.config import Settings, load_settings, save_settings
from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.domain.models import ToolError
from tests.support.catalog import dialog_page, pinned_page
from tests.support.fakes import BASE, FakeClock


def gateway(respond, *, ttl: float = 30, maximum: int = 10000):
    clock = FakeClock(now=BASE)

    async def with_pinned(request):
        if isinstance(request, functions.messages.GetPinnedDialogsRequest):
            return pinned_page([])
        result = respond(request)
        return await result if inspect.isawaitable(result) else result

    client = AsyncMock(side_effect=with_pinned)
    client.is_connected = Mock(return_value=True)
    runtime = TelegramRuntime(Settings(), client=client, clock=clock)
    chats = ChatGateway(runtime, clock=clock, max_dialogs=maximum, catalog_cache_ttl_seconds=ttl)
    return chats, client, clock


def simple_response(request):
    if isinstance(request, functions.messages.GetPinnedDialogsRequest):
        return pinned_page([])
    if isinstance(request, functions.updates.GetStateRequest):
        return None
    if isinstance(request, functions.messages.GetDialogFiltersRequest):
        return SimpleNamespace(filters=[])
    assert isinstance(request, functions.messages.GetDialogsRequest)
    return dialog_page([1])


async def test_snapshot_ttl_copies_and_fresh_authorization_without_dialog_rpcs() -> None:
    chats, client, clock = gateway(simple_response)
    result, folders = await chats.catalog()
    result[0].title = "Caller mutation"
    result.clear()
    folders.clear()
    warm, _ = await chats.catalog()
    assert warm[0].title == "User 1"
    assert [type(call.args[0]) for call in client.await_args_list] == [
        functions.messages.GetPinnedDialogsRequest,
        functions.messages.GetPinnedDialogsRequest,
        functions.messages.GetDialogsRequest,
        functions.messages.GetDialogFiltersRequest,
        functions.updates.GetStateRequest,
    ]
    clock.time += 30
    await chats.catalog()
    assert (
        sum(
            isinstance(call.args[0], functions.messages.GetDialogsRequest)
            for call in client.await_args_list
        )
        == 2
    )


async def test_cold_catalog_progress_is_retained_and_never_published_partially() -> None:
    def respond(request):
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        assert isinstance(request, functions.messages.GetDialogsRequest)
        high = 601 if request.offset_id == 0 else request.offset_id - 1
        assert request.limit == 100
        assert request.exclude_pinned is True
        return dialog_page(list(range(high, max(0, high - 100), -1)), sliced=high > 100)

    chats, client, _ = gateway(respond)
    with pytest.raises(ToolError, match="CATALOG_LOADING") as caught:
        await chats.catalog()
    assert caught.value.retry_after == 1 and "300 dialogs" in caught.value.detail
    assert chats.cache == {}
    rows, _ = await chats.catalog()
    assert len(rows) == 601 and len({row.id for row in rows}) == 601
    assert len(client.await_args_list) == 10
    assert isinstance(
        client.await_args_list[-1].args[0], functions.messages.GetDialogFiltersRequest
    )


async def test_expired_snapshot_is_not_returned_when_refresh_fails() -> None:
    chats, client, clock = gateway(simple_response)
    await chats.catalog()
    clock.time += 30
    client.side_effect = ConnectionError("private details")
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await chats.catalog()
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await chats.catalog()


async def test_cached_snapshot_does_not_hide_revoked_authorization() -> None:
    chats, client, _ = gateway(simple_response)
    await chats.catalog()
    client.side_effect = errors.SessionRevokedError(None)
    with pytest.raises(ToolError, match="AUTH_REQUIRED"):
        await chats.catalog()
    assert chats.cache == {}


async def test_flood_wait_resumes_same_page_without_extending_its_deadline() -> None:
    offsets = []
    failed = False

    def respond(request):
        nonlocal failed
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        assert isinstance(request, functions.messages.GetDialogsRequest)
        offsets.append(request.offset_id)
        if not request.offset_id:
            return dialog_page(list(range(200, 100, -1)), sliced=True)
        if not failed:
            failed = True
            raise errors.FloodWaitError(request, capture=10)
        return dialog_page([100])

    chats, _, clock = gateway(respond)
    with pytest.raises(ToolError, match="RATE_LIMITED"):
        await chats.catalog()
    for step in (0.1, 0.4, 8.6):
        clock.time += step
        with pytest.raises(ToolError, match="RATE_LIMITED"):
            await chats.catalog()
        assert offsets == [0, 101]
    clock.time = 110
    rows, _ = await chats.catalog()
    assert len(rows) == 101 and offsets == [0, 101, 101]


async def test_soft_budget_retains_progress_and_ttl_starts_at_completion() -> None:
    count = 0

    def respond(request):
        nonlocal count
        clock.time += 6
        if isinstance(request, functions.updates.GetStateRequest):
            return None
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        count += 1
        return dialog_page(list(range(1000 - count * 100, 900 - count * 100, -1)), sliced=count < 6)

    chats, _, clock = gateway(respond)
    for _ in range(6):
        with pytest.raises(ToolError, match="CATALOG_LOADING"):
            await chats.catalog()
    rows, _ = await chats.catalog()
    assert len(rows) == 600 and clock.time > 130
    await chats.catalog()
    assert count == 6


async def test_concurrent_catalog_calls_share_one_refresh() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        if isinstance(request, functions.messages.GetDialogsRequest):
            entered.set()
            await release.wait()
        return simple_response(request)

    chats, client, _ = gateway(respond)
    first = asyncio.create_task(chats.catalog())
    await entered.wait()
    second = asyncio.create_task(chats.catalog())
    release.set()
    one, two = await asyncio.gather(first, second)
    assert one == two
    assert (
        sum(
            isinstance(call.args[0], functions.messages.GetDialogsRequest)
            for call in client.await_args_list
        )
        == 1
    )


async def test_zero_ttl_and_forced_refresh_bypass_completed_snapshot() -> None:
    chats, client, _ = gateway(simple_response, ttl=0)
    await chats.catalog()
    await chats.catalog()
    assert client.await_count == 8
    chats.catalog_cache_ttl_seconds = 30
    await chats.catalog()
    client.side_effect = ConnectionError()
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await chats.catalog(refresh=True)
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await chats.catalog()


async def test_limit_and_nonadvancing_pages_do_not_publish_a_catalog() -> None:
    chats, _, _ = gateway(lambda _: dialog_page([1, 2]), maximum=1)
    with pytest.raises(ToolError, match="CATALOG_LIMIT"):
        await chats.catalog()
    assert chats.cache == {}
    chats, _, _ = gateway(lambda _: dialog_page(list(range(100, 0, -1)), sliced=True))
    with pytest.raises(ToolError, match="CATALOG_ERROR"):
        await chats.catalog()
    assert chats.cache == {}


async def test_notification_defaults_are_followed_by_fresh_folder_definitions() -> None:
    folder = types.DialogFilter(
        id=2,
        title=types.TextWithEntities("Unread", []),
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
        exclude_muted=True,
    )
    order = []

    def respond(request):
        order.append(type(request))
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            if order.count(functions.messages.GetDialogFiltersRequest) == 2:
                folder.title = types.TextWithEntities("Fresh definition", [])
            return SimpleNamespace(filters=[folder])
        if isinstance(request, functions.account.GetNotifySettingsRequest):
            return types.PeerNotifySettings(mute_until=BASE + timedelta(days=1))
        return dialog_page([1])

    chats, _, _ = gateway(respond)
    with pytest.raises(ToolError, match="CATALOG_LOADING"):
        await chats.catalog()
    rows, folders = await chats.catalog()
    assert rows[0].is_muted and folders[0].exclude_muted
    assert folders[0].title == "Fresh definition"
    assert order[-1] is functions.messages.GetDialogFiltersRequest
    assert order.count(functions.messages.GetDialogFiltersRequest) == 2


@pytest.mark.parametrize("ttl", [0, 5, 30])
def test_catalog_ttl_configuration_roundtrip(tmp_path: Path, ttl: int) -> None:
    path = tmp_path / "config.toml"
    save_settings(Settings(catalog_cache_ttl_seconds=ttl), path)
    assert load_settings(path).catalog_cache_ttl_seconds == ttl


@pytest.mark.parametrize("value", [-1, 31, "invalid"])
def test_invalid_catalog_ttl_configuration_is_actionable(tmp_path: Path, value) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"catalog_cache_ttl_seconds = {value!r}\n")
    with pytest.raises(ToolError, match="INVALID_CONFIGURATION"):
        load_settings(path)


async def test_network_failure_resumes_the_uncommitted_page() -> None:
    offsets = []
    fail = True

    def respond(request):
        nonlocal fail
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        assert isinstance(request, functions.messages.GetDialogsRequest)
        offsets.append(request.offset_id)
        if not request.offset_id:
            return dialog_page(list(range(200, 100, -1)), sliced=True)
        if fail:
            fail = False
            raise ConnectionError("private details")
        return dialog_page([100])

    chats, _, _ = gateway(respond)
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await chats.catalog()
    assert chats.cache == {}
    result, _ = await chats.catalog()
    assert len(result) == 101 and offsets == [0, 101, 101]


async def test_cancelled_page_keeps_only_previously_validated_progress() -> None:
    entered = asyncio.Event()
    blocked = True
    offsets = []

    async def respond(request):
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        assert isinstance(request, functions.messages.GetDialogsRequest)
        offsets.append(request.offset_id)
        if not request.offset_id:
            return dialog_page(list(range(200, 100, -1)), sliced=True)
        if blocked:
            entered.set()
            await asyncio.Event().wait()
        return dialog_page([100])

    chats, _, _ = gateway(respond)
    pending = asyncio.create_task(chats.catalog())
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert chats.cache == {}
    blocked = False
    result, _ = await chats.catalog()
    assert len(result) == 101 and offsets == [0, 101, 101]


async def test_inactive_builder_expires_after_fifteen_minutes() -> None:
    offsets = []

    def respond(request):
        assert isinstance(request, functions.messages.GetDialogsRequest)
        offsets.append(request.offset_id)
        high = 1000 if not request.offset_id else request.offset_id - 1
        return dialog_page(list(range(high, high - 100, -1)), sliced=True)

    chats, _, clock = gateway(respond)
    with pytest.raises(ToolError, match="CATALOG_LOADING"):
        await chats.catalog()
    clock.time += 900
    with pytest.raises(ToolError, match="CATALOG_LOADING"):
        await chats.catalog()
    assert offsets[0] == offsets[3] == 0 and chats.cache == {}


async def test_reconnect_invalidates_snapshot_but_keeps_partial_builder() -> None:
    offsets = []
    connected = True

    def respond(request):
        if isinstance(request, functions.updates.GetStateRequest):
            return None
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        offsets.append(request.offset_id)
        high = 501 if not request.offset_id else request.offset_id - 1
        return dialog_page(list(range(high, max(0, high - 100), -1)), sliced=high > 100)

    chats, client, _ = gateway(respond)
    client.is_connected = Mock(side_effect=lambda: connected)

    async def reconnect():
        nonlocal connected
        connected = True

    client.connect.side_effect = reconnect
    with pytest.raises(ToolError, match="CATALOG_LOADING"):
        await chats.catalog()
    connected = False
    result, _ = await chats.catalog()
    assert len(result) == 501 and offsets == [0, 402, 302, 202, 102, 2]
    connected = False
    with pytest.raises(ToolError, match="CATALOG_LOADING"):
        await chats.catalog()
    assert offsets[6] == 0 and client.connect.await_count == 2


async def test_revoked_authorization_discards_partial_catalog() -> None:
    failed = False
    offsets = []

    def respond(request):
        nonlocal failed
        if isinstance(request, functions.updates.GetStateRequest):
            return None
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        offsets.append(request.offset_id)
        if not request.offset_id:
            return dialog_page(list(range(200, 100, -1)), sliced=True)
        if not failed:
            failed = True
            raise errors.SessionRevokedError(request)
        return dialog_page([100])

    chats, _, _ = gateway(respond)
    with pytest.raises(ToolError, match="AUTH_REQUIRED"):
        await chats.catalog()
    result, _ = await chats.catalog()
    assert len(result) == 101 and offsets == [0, 101, 0, 101]


async def test_missing_next_offset_cannot_publish_or_poison_a_later_refresh() -> None:
    broken = dialog_page(list(range(100, 0, -1)), sliced=True)
    broken.messages = []
    chats, client, _ = gateway(lambda _: broken)
    with pytest.raises(ToolError, match="CATALOG_ERROR"):
        await chats.catalog()
    assert chats.cache == {}
    client.side_effect = simple_response
    assert len((await chats.catalog())[0]) == 1


async def test_pinned_duplicates_and_migrated_groups_do_not_distort_offsets() -> None:
    first = dialog_page(list(range(200, 100, -1)), sliced=True)
    first.dialogs[0].peer = types.PeerChat(200)
    first.users = first.users[1:]
    first.chats = [
        types.Chat(
            id=200,
            title="Migrated",
            photo=types.ChatPhotoEmpty(),
            participants_count=1,
            date=BASE,
            version=1,
            migrated_to=types.InputChannel(300, 400),
        )
    ]
    first.messages[0].peer_id = types.PeerChat(200)
    offsets = []

    def respond(request):
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        offsets.append(request.offset_id)
        return first if not request.offset_id else dialog_page([101, 100])

    chats, _, _ = gateway(respond)
    result, _ = await chats.catalog()
    assert len(result) == len({chat.id for chat in result}) == 100
    assert all(chat.id != "-200" for chat in result)
    assert offsets == [0, 101]


async def test_pinned_dialogs_are_separate_from_unpinned_chronological_pagination() -> None:
    summary = types.DialogFolder(
        folder=types.Folder(id=1, title="Archive"),
        peer=types.PeerUser(999),
        top_message=999,
        unread_muted_peers_count=0,
        unread_unmuted_peers_count=0,
        unread_muted_messages_count=0,
        unread_unmuted_messages_count=0,
        pinned=True,
    )
    calls = []

    def respond(request):
        calls.append(request)
        if isinstance(request, functions.messages.GetPinnedDialogsRequest):
            if request.folder_id == 0:
                page = pinned_page(list(range(1, 111)))
                page.dialogs.insert(0, summary)
                return page
            assert request.folder_id == 1
            return pinned_page([111, 112])
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        assert isinstance(request, functions.messages.GetDialogsRequest)
        assert request.folder_id is None and request.exclude_pinned is True
        assert request.offset_id == 0 and request.offset_date is None
        assert isinstance(request.offset_peer, types.InputPeerEmpty)
        page = dialog_page([1000, 113, 110])
        page.dialogs[1].folder_id = 1
        page.dialogs.insert(0, summary)
        return page

    chats, client, _ = gateway(simple_response)
    client.side_effect = respond
    rows, _ = await chats.catalog()
    found = {row.id: row for row in rows}
    assert len(rows) == len(found) == 114 and "999" not in found
    assert {row.id for row in rows if row.is_archived} == {"111", "112", "113"}
    assert "1000" in found
    assert len(calls) == 4
    assert [request.folder_id for request in calls[:2]] == [0, 1]


async def test_pinned_dialogs_obey_catalog_limit_before_loading_ordinary_dialogs() -> None:
    chats, client, _ = gateway(simple_response, maximum=1)
    client.side_effect = lambda request: pinned_page([1, 2])
    with pytest.raises(ToolError, match="CATALOG_LIMIT"):
        await chats.catalog()
    assert chats.cache == {}
    client.assert_awaited_once()
    assert isinstance(client.await_args.args[0], functions.messages.GetPinnedDialogsRequest)


@pytest.mark.parametrize("change", ["reconnect", "expiry"])
async def test_snapshot_is_rechecked_after_authorization_probe(change: str) -> None:
    chats, client, clock = gateway(simple_response)
    await chats.catalog()
    if change == "reconnect":
        client.is_connected = Mock(side_effect=[True, False, False])

        async def reconnect():
            client.is_connected = Mock(return_value=True)

        client.connect.side_effect = reconnect
    else:

        def respond(request):
            if isinstance(request, functions.updates.GetStateRequest):
                clock.time += 30
            return simple_response(request)

        client.side_effect = respond
    await chats.catalog()
    assert (
        sum(
            isinstance(call.args[0], functions.messages.GetDialogsRequest)
            for call in client.await_args_list
        )
        == 2
    )


async def test_catalog_cannot_publish_after_concurrent_authorization_loss() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            entered.set()
            await release.wait()
        return simple_response(request)

    chats, _, _ = gateway(respond)
    pending = asyncio.create_task(chats.catalog())
    await entered.wait()
    try:
        with pytest.raises(ToolError, match="AUTH_REQUIRED"):
            await chats.runtime.call(AsyncMock(side_effect=errors.SessionRevokedError(None))())
    finally:
        release.set()
    with pytest.raises(ToolError, match="AUTH_REQUIRED"):
        await pending
    assert chats.cache == {}
    assert chats._snapshot is None and chats._builder is None
