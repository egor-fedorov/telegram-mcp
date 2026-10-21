from typing import Literal

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.application.ports import DiscoveryBatch
from telegram_mcp.application.service import Service
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Chat, DiscoveryResult, Folder, PublicChannel, ToolError
from tests.fakes import FakeBackend, msg

DiscoveryTool = Literal["search", "similar"]


def channel(number: int, *, participants_count: int | None = None) -> PublicChannel:
    return PublicChannel(
        id=f"-100{number:010d}",
        title=f"Channel {number}",
        username=f"channel_{number}",
        url=f"https://t.me/channel_{number}",
        participants_count=participants_count,
    )


async def discover(service: Service, tool: DiscoveryTool, limit: int = 20) -> DiscoveryResult:
    if tool == "search":
        return await service.search_public_channels("topic", limit=limit)
    return await service.get_similar_channels("@channel_1", limit=limit)


def discovery_backend(batch: DiscoveryBatch | None = None) -> FakeBackend:
    backend = FakeBackend()
    if batch is not None:
        backend.discovery_search_batch = batch
        backend.discovery_similar_batch = batch
    return backend


@pytest.mark.parametrize("tool", ["search", "similar"])
async def test_discovery_is_disabled_by_default_without_any_backend_access(
    tool: DiscoveryTool,
) -> None:
    backend = discovery_backend()
    settings = Settings()
    service = build_service(backend, settings)
    assert settings.allow_public_channel_discovery is False
    with pytest.raises(ToolError, match="FEATURE_DISABLED"):
        await discover(service, tool)
    assert backend.backend_calls == []


@pytest.mark.parametrize("tool", ["search", "similar"])
@pytest.mark.parametrize("allow_reads", [False, True])
@pytest.mark.parametrize("allow_all", [False, True])
async def test_disabled_discovery_cannot_be_enabled_by_read_permissions(
    tool: DiscoveryTool, allow_reads: bool, allow_all: bool
) -> None:
    backend = discovery_backend()
    service = build_service(
        backend,
        Settings(
            allow_public_channel_discovery=False,
            allow_public_channel_reads=allow_reads,
            allow_all_chats=allow_all,
            allowed_chat_ids=[channel(1).id],
        ),
    )
    with pytest.raises(ToolError, match="FEATURE_DISABLED"):
        await discover(service, tool)
    assert backend.backend_calls == []
    assert backend.discovery_search_calls == []
    assert backend.discovery_similar_calls == []


@pytest.mark.parametrize("tool", ["search", "similar"])
async def test_metadata_discovery_does_not_access_read_or_account_endpoints(
    tool: DiscoveryTool,
) -> None:
    candidate = channel(1)
    backend = discovery_backend(DiscoveryBatch([candidate]))
    settings = Settings(allow_public_channel_discovery=True)
    service = build_service(backend, settings)
    result = await discover(service, tool)
    assert result.channels == [candidate]
    assert backend.backend_calls == [
        "search_public_channels" if tool == "search" else "get_similar_channels"
    ]
    assert settings.allowed_chat_ids == []
    assert settings.allowed_folder_ids == []
    assert settings.allow_public_channel_reads is False
    assert settings.allow_all_chats is False


@pytest.mark.parametrize("tool", ["search", "similar"])
@pytest.mark.parametrize("permission", ["public_reads", "allowlist", "all_chats"])
async def test_discovered_channel_requires_separate_read_permission(
    tool: DiscoveryTool, permission: str
) -> None:
    candidate = channel(1)
    backend = FakeBackend(
        [msg(candidate.id, 1)],
        chats=[],
        external_chats=[
            Chat(
                id=candidate.id,
                title=candidate.title,
                type="channel",
                username=candidate.username,
            )
        ],
    )
    backend.discovery_search_batch = DiscoveryBatch([candidate])
    backend.discovery_similar_batch = DiscoveryBatch([candidate])
    settings = Settings(allow_public_channel_discovery=True)
    service = build_service(backend, settings)
    result = await discover(service, tool)
    discovered = result.channels[0]
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_chat(discovered.url)
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_history(chat_id=discovered.id)
    assert backend.fetches == []
    assert backend.reads == []
    assert backend.batch_reads == []

    if permission == "public_reads":
        settings.allow_public_channel_reads = True
    elif permission == "allowlist":
        settings.allowed_chat_ids = [candidate.id]
    else:
        settings.allow_all_chats = True
    settings.allow_public_channel_discovery = False
    history = await service.get_history(chat_ref=discovered.url)
    assert [message.message_ref.key for message in history.messages] == [f"{candidate.id}:1"]
    with pytest.raises(ToolError, match="FEATURE_DISABLED"):
        await discover(service, tool)


async def test_discovery_does_not_expand_catalog_implicit_search_or_folder_scope() -> None:
    candidate = channel(1)
    local = Chat(id="1", title="Allowed group", type="group")
    external = Chat(
        id=candidate.id, title=candidate.title, type="channel", username=candidate.username
    )
    folder = Folder(id=1, title="Channels", position=0, include_channels=True)
    backend = FakeBackend(
        [msg(local.id, 1), msg(candidate.id, 2)],
        chats=[local],
        external_chats=[external],
        folders=[folder],
    )
    backend.discovery_search_batch = DiscoveryBatch([candidate])
    backend.discovery_similar_batch = DiscoveryBatch([candidate])
    settings = Settings(
        allow_public_channel_discovery=True,
        allow_public_channel_reads=True,
        allowed_chat_ids=[local.id],
    )
    service = build_service(
        backend,
        settings,
    )
    await service.search_public_channels("topic")
    await service.get_similar_channels(candidate.url)
    listed = await service.list_chats()
    implicit = await service.search_messages(query="budget")
    from_folder = await service.search_messages(query="budget", folder_ids=[folder.id])
    assert [chat.id for chat in listed.chats] == [local.id]
    assert implicit.scope.chat_ids == [local.id]
    assert [message.message_ref.key for message in implicit.messages] == ["1:1"]
    assert from_folder.scope.chat_ids == []
    assert from_folder.messages == []
    assert candidate.id not in backend.fetches
    assert backend.chats == [local]
    assert folder.included_chat_ids == []
    assert settings.allowed_chat_ids == [local.id]


@pytest.mark.parametrize("query", ["", " \n\t ", "x" * 1001])
async def test_invalid_search_query_fails_before_backend_access(query: str) -> None:
    backend = discovery_backend()
    service = build_service(backend, Settings(allow_public_channel_discovery=True))
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await service.search_public_channels(query)
    assert backend.backend_calls == []


@pytest.mark.parametrize("query", ["x", "x" * 1000, "каналы 🌐"])
async def test_search_trims_query_and_accepts_character_length_boundaries(query: str) -> None:
    backend = discovery_backend()
    service = build_service(backend, Settings(allow_public_channel_discovery=True))
    result = await service.search_public_channels(f" \n\t{query}\t ")
    assert result.channels == []
    assert backend.discovery_search_calls == [(query, 20)]


@pytest.mark.parametrize("reference", ["", " \n\t "])
async def test_similar_channels_requires_nonblank_seed_before_backend_access(
    reference: str,
) -> None:
    backend = discovery_backend()
    service = build_service(backend, Settings(allow_public_channel_discovery=True))
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await service.get_similar_channels(reference)
    assert backend.backend_calls == []


@pytest.mark.parametrize("tool", ["search", "similar"])
@pytest.mark.parametrize("limit", [-1, 0, 101])
async def test_invalid_discovery_limit_fails_before_backend_access(
    tool: DiscoveryTool, limit: int
) -> None:
    backend = discovery_backend()
    service = build_service(backend, Settings(allow_public_channel_discovery=True))
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await discover(service, tool, limit)
    assert backend.backend_calls == []


@pytest.mark.parametrize("tool", ["search", "similar"])
@pytest.mark.parametrize("limit", [1, 100])
async def test_discovery_limit_boundaries_preserve_backend_contracts(
    tool: DiscoveryTool, limit: int
) -> None:
    backend = discovery_backend(DiscoveryBatch([channel(1)]))
    service = build_service(backend, Settings(allow_public_channel_discovery=True))
    result = await discover(service, tool, limit)
    assert result.returned_count == 1
    if tool == "search":
        assert backend.discovery_search_calls == [("topic", limit)]
        assert backend.discovery_similar_calls == []
    else:
        assert backend.discovery_similar_calls == ["@channel_1"]
        assert backend.discovery_search_calls == []


@pytest.mark.parametrize("tool", ["search", "similar"])
async def test_discovery_preserves_first_occurrence_and_order_before_limiting(
    tool: DiscoveryTool,
) -> None:
    first = channel(2)
    duplicate = first.model_copy(update={"title": "Later duplicate", "participants_count": 999})
    second = channel(1, participants_count=0)
    third = channel(3, participants_count=100)
    original = [first, duplicate, second, third]
    batch = DiscoveryBatch(list(original), telegram_total_count=50)
    backend = discovery_backend(batch)
    service = build_service(backend, Settings(allow_public_channel_discovery=True))
    result = await discover(service, tool, limit=2)
    assert result.channels == [first, second]
    assert result.returned_count == 2
    assert result.telegram_total_count == 50
    assert result.locally_truncated is True
    assert result.exhaustive is False
    assert batch.channels == original
    assert [item.participants_count for item in result.channels] == [None, 0]
    assert len(backend.discovery_search_calls) + len(backend.discovery_similar_calls) == 1


@pytest.mark.parametrize("tool", ["search", "similar"])
@pytest.mark.parametrize(
    ("channels", "total_count", "limit", "expected_truncated"),
    [
        ([], None, 20, False),
        ([], 0, 20, False),
        ([channel(1)], None, 20, False),
        ([channel(1)], 50, 20, False),
        ([channel(1), channel(1), channel(2)], 50, 2, False),
        ([channel(1), channel(2), channel(3)], 3, 2, True),
    ],
)
async def test_discovery_distinguishes_unknown_counts_server_limits_and_local_truncation(
    tool: DiscoveryTool,
    channels: list[PublicChannel],
    total_count: int | None,
    limit: int,
    expected_truncated: bool,
) -> None:
    backend = discovery_backend(DiscoveryBatch(channels, telegram_total_count=total_count))
    service = build_service(backend, Settings(allow_public_channel_discovery=True))
    result = await discover(service, tool, limit)
    assert result.telegram_total_count == total_count
    assert result.returned_count == len(result.channels)
    assert result.locally_truncated is expected_truncated
    assert result.exhaustive is False
    assert len(result.channels) <= limit
    assert len(backend.backend_calls) == 1
    assert set(result.model_dump()) == {
        "channels",
        "returned_count",
        "telegram_total_count",
        "locally_truncated",
        "exhaustive",
    }
    for item in result.model_dump()["channels"]:
        assert set(item) == {"id", "title", "username", "url", "participants_count"}


@pytest.mark.parametrize("tool", ["search", "similar"])
@pytest.mark.parametrize(
    ("code", "retry_after"),
    [
        ("AUTH_REQUIRED", None),
        ("RATE_LIMITED", 30),
        ("CONNECTION_ERROR", None),
        ("TELEGRAM_UNAVAILABLE", None),
        ("ACCESS_DENIED", None),
        ("INVALID_ARGUMENT", None),
        ("INVALID_REFERENCE", None),
    ],
)
async def test_discovery_errors_remain_explicit_without_hidden_retry_or_empty_success(
    tool: DiscoveryTool, code: str, retry_after: int | None
) -> None:
    backend = discovery_backend()
    fault = ToolError(code, "The request could not complete.", retry_after)
    backend.discovery_faults[tool] = fault
    service = build_service(backend, Settings(allow_public_channel_discovery=True))
    with pytest.raises(ToolError) as caught:
        await discover(service, tool)
    assert caught.value.code == code
    assert caught.value.detail == fault.detail
    assert caught.value.retry_after == retry_after
    assert backend.backend_calls == [
        "search_public_channels" if tool == "search" else "get_similar_channels"
    ]
