import pytest

from telegram_mcp.domain.folders import allowed_ids, contains, select_chats
from telegram_mcp.domain.models import Chat, Folder, ToolError
from telegram_mcp.domain.policy import ReadPolicy


@pytest.mark.parametrize(
    ("chat", "options", "expected"),
    [
        (Chat(id="1", title="one", type="channel"), {"include_channels": True}, True),
        (Chat(id="1", title="one", type="group"), {"include_channels": True}, False),
        (Chat(id="1", title="one", type="bot", is_contact=True), {"include_contacts": True}, False),
        (Chat(id="1", title="one", type="bot"), {"include_bots": True}, True),
        (
            Chat(id="1", title="one", type="private", is_contact=True),
            {"include_contacts": True},
            True,
        ),
        (Chat(id="1", title="one", type="private"), {"include_non_contacts": True}, True),
        (Chat(id="1", title="one", type="saved"), {"include_non_contacts": True}, True),
        (
            Chat(id="1", title="one", type="group", is_muted=True),
            {"include_groups": True, "exclude_muted": True},
            False,
        ),
        (
            Chat(id="1", title="one", type="group", is_archived=True),
            {"include_groups": True, "exclude_archived": True},
            False,
        ),
        (
            Chat(id="1", title="one", type="group", marked_unread=True),
            {"include_groups": True, "exclude_read": True},
            True,
        ),
        (
            Chat(id="1", title="one", type="group"),
            {"include_groups": True, "exclude_read": True},
            False,
        ),
    ],
)
def test_dynamic_rules(chat: Chat, options: dict[str, bool], expected: bool) -> None:
    folder = Folder.model_validate({"id": 1, "title": "Folder", "position": 0, **options})
    assert contains(folder, chat) is expected


def test_explicit_includes_pins_and_exclusions() -> None:
    chat = Chat(id="1", title="one", type="channel", is_muted=True)
    folder = Folder(
        id=1,
        title="A",
        position=0,
        include_channels=True,
        exclude_muted=True,
        pinned_chat_ids=["1"],
    )
    assert contains(folder, chat)
    folder.excluded_chat_ids = ["1"]
    assert not contains(folder, chat)
    folder.pinned_chat_ids = []
    folder.excluded_chat_ids = []
    folder.included_chat_ids = ["1"]
    assert contains(folder, chat)


def test_union_intersection_policy_and_duplicates() -> None:
    chats = [Chat(id=str(i), title=f"Channel {i}", type="channel") for i in range(1, 4)]
    folders = [
        Folder(id=1, title="Same", position=0, included_chat_ids=["1", "2"]),
        Folder(id=2, title="Same", position=1, included_chat_ids=["2", "3"]),
    ]
    settings = ReadPolicy(allowed_folder_ids=(1,))
    allowed = allowed_ids(settings, chats, folders)
    result = select_chats(chats + chats, folders, allowed, folder_ids=[1, 2])
    assert [c.id for c in result] == ["1", "2"]
    assert result[1].matched_folder_ids == [1, 2]
    assert [c.id for c in select_chats(chats, folders, allowed, [1, 2], ["2"])] == ["2"]
    assert allowed_ids(ReadPolicy(), chats, folders) == set()
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        select_chats(chats, folders, allowed, chat_ids=["3"])
    with pytest.raises(ToolError, match="FOLDER_NOT_FOUND"):
        select_chats(chats, folders, allowed, folder_ids=[99])


def test_folder_pin_order() -> None:
    chats = [Chat(id=str(i), title="x", type="channel") for i in range(1, 4)]
    folders = [
        Folder(id=1, title="A", position=0, include_channels=True, pinned_chat_ids=["3", "2"])
    ]
    assert [c.id for c in select_chats(chats, folders, {"1", "2", "3"}, [1])] == ["3", "2", "1"]


def test_unread_mentions_and_community_rules() -> None:
    chat = Chat(id="1", title="Group", type="group", is_muted=True, unread_mentions_count=1)
    folder = Folder(
        id=1, title="Unread", position=0, include_groups=True, exclude_muted=True, exclude_read=True
    )
    assert contains(folder, chat)
    chat.is_archived = True
    assert not contains(folder, chat)
    chat.is_archived = False
    chat.is_community = True
    assert not contains(folder, chat)


def test_service_notifications_are_never_allowed() -> None:
    chats = [
        Chat(id="1", title="one", type="private"),
        Chat(id="777000", title="Telegram", type="private"),
    ]
    folders = [Folder(id=1, title="All", position=0, included_chat_ids=["1", "777000"])]
    assert allowed_ids(ReadPolicy(allow_all_chats=True), chats, folders) == {"1"}
    assert allowed_ids(ReadPolicy(allowed_folder_ids=(1,)), chats, folders) == {"1"}
