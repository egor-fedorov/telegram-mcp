from datetime import UTC, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from telethon import types

from telegram_mcp.config import Settings
from telegram_mcp.message_metadata import normalize_entities, normalize_poll, normalize_reactions
from telegram_mcp.models import Chat
from telegram_mcp.telegram import TelegramBackend, normalize_message
from tests.fakes import BASE

CHAT = Chat(id="-1000000000123", title="News", type="channel", username="example")


def raw_message(**kwargs: Any) -> types.Message:
    return types.Message(
        id=10,
        peer_id=types.PeerChannel(123),
        date=BASE,
        message="Original 🦉 caption",
        **kwargs,
    )


def raw_poll(**kwargs: Any) -> types.Poll:
    return types.Poll(
        id=2**62 + 5,
        question=types.TextWithEntities(
            "🦉 Which?", [types.MessageEntityCustomEmoji(offset=0, length=2, document_id=2**62)]
        ),
        answers=[
            types.PollAnswer(types.TextWithEntities("Same", []), b"\xff\x00"),
            types.PollAnswer(types.TextWithEntities("Same", []), b""),
        ],
        hash=123,
        **kwargs,
    )


@pytest.mark.parametrize("count", [None, 0, 123])
def test_available_message_counters_preserve_absence_and_zero(count: int | None) -> None:
    raw = raw_message(
        views=count,
        forwards=count,
        replies=None if count is None else types.MessageReplies(replies=count, replies_pts=8),
    )
    result = normalize_message(raw, CHAT)
    assert (result.view_count, result.forward_count, result.reply_count) == (count, count, count)
    assert result.text == raw.message
    assert result.sent_at == BASE
    assert result.message_ref.message_id == 10
    assert result.url == "https://t.me/example/10"
    assert result.reactions is None and result.poll is None


def test_reactions_distinguish_missing_summary_from_empty_results() -> None:
    assert normalize_reactions(None) is None
    summary = normalize_reactions(types.MessageReactions(results=[]))
    assert summary is not None and summary.results == [] and not summary.is_min


@pytest.mark.parametrize("as_tags", [None, False, True])
def test_reaction_tag_flag_preserves_native_meaning(as_tags: bool | None) -> None:
    summary = normalize_reactions(
        types.MessageReactions(
            results=[types.ReactionCount(types.ReactionEmoji("⭐"), 1)],
            reactions_as_tags=as_tags,
        )
    )
    assert summary is not None
    assert summary.reactions_as_tags is bool(as_tags)
    assert summary.results[0].emoji == "⭐" and summary.results[0].count == 1


@pytest.mark.parametrize("is_min", [False, True])
def test_reaction_variants_preserve_counts_and_personal_selection(is_min: bool) -> None:
    raw = types.MessageReactions(
        min=is_min,
        results=[
            types.ReactionCount(types.ReactionEmoji("👍"), 0, chosen_order=0),
            types.ReactionCount(types.ReactionCustomEmoji(2**62 + 1), 7),
            types.ReactionCount(types.ReactionPaid(), 200, chosen_order=2),
            types.ReactionCount(types.ReactionEmpty(), 3, chosen_order=1),
            types.ReactionCount(
                cast(Any, SimpleNamespace(secret="not exported")), 4, chosen_order=5
            ),
        ],
    )
    summary = normalize_reactions(raw)
    assert summary is not None and summary.is_min is is_min
    assert [item.type for item in summary.results] == [
        "emoji",
        "custom_emoji",
        "paid",
        "empty",
        "unknown",
    ]
    assert [item.count for item in summary.results] == [0, 7, 200, 3, 4]
    assert summary.results[0].emoji == "👍"
    assert summary.results[1].document_id == str(2**62 + 1)
    assert [item.is_chosen for item in summary.results] == (
        [None] * 5 if is_min else [True, False, None, None, None]
    )
    assert [item.chosen_order for item in summary.results] == (
        [None] * 5 if is_min else [0, None, None, None, None]
    )
    assert "not exported" not in summary.model_dump_json()


def test_poll_keeps_binary_option_ids_order_and_formatted_text() -> None:
    poll = raw_poll()
    poll.answers[1].text = types.TextWithEntities(
        "Same", [types.MessageEntityTextUrl(0, 4, "https://example.com/answer")]
    )
    result = normalize_poll(types.MessageMediaPoll(poll, types.PollResults()))
    assert result is not None
    assert result.id == str(2**62 + 5)
    assert [answer.option_id for answer in result.answers] == ["ff00", ""]
    assert result.question.text == "🦉 Which?"
    assert result.question.entities[0].offset == 0
    assert result.question.entities[0].length == 2
    assert result.question.entities[0].document_id == str(2**62)
    assert result.answers[1].text.entities[0].url == "https://example.com/answer"
    assert result.results is not None and result.results.answers is None
    assert result.results.total_voters is None


@pytest.mark.parametrize("rows", [None, []])
@pytest.mark.parametrize("total", [None, 0, 9])
def test_poll_results_preserve_absent_and_empty_vectors_without_synthesis(
    rows: list[types.PollAnswerVoters] | None, total: int | None
) -> None:
    result = normalize_poll(
        types.MessageMediaPoll(raw_poll(), types.PollResults(results=rows, total_voters=total))
    )
    assert result is not None and result.results is not None
    assert result.results.answers == rows
    assert result.results.total_voters == total


@pytest.mark.parametrize("is_min", [False, True])
def test_poll_results_match_opaque_ids_without_reordering_or_dropping_unknown_options(
    is_min: bool,
) -> None:
    rows = [
        types.PollAnswerVoters(option=b"", chosen=True, voters=0),
        types.PollAnswerVoters(option=b"unknown", voters=None),
        types.PollAnswerVoters(option=b"\xff\x00", voters=4),
    ]
    result = normalize_poll(
        types.MessageMediaPoll(raw_poll(), types.PollResults(results=rows, min=is_min))
    )
    assert result is not None and result.results is not None
    assert result.results.answers is not None
    assert [item.option_id for item in result.results.answers] == ["", "756e6b6e6f776e", "ff00"]
    assert [item.voter_count for item in result.results.answers] == [0, None, 4]
    assert [item.is_chosen for item in result.results.answers] == (
        [None] * 3 if is_min else [True, False, False]
    )
    assert all(item.is_correct is None for item in result.results.answers)


@pytest.mark.parametrize("is_min", [False, True])
@pytest.mark.parametrize("quiz", [False, True])
@pytest.mark.parametrize("revealed", [False, True])
def test_correctness_requires_quiz_and_returned_reveal_evidence(
    is_min: bool, quiz: bool, revealed: bool
) -> None:
    result = normalize_poll(
        types.MessageMediaPoll(
            raw_poll(quiz=quiz, closed=True, hide_results_until_close=True),
            types.PollResults(
                min=is_min,
                total_voters=10,
                results=[
                    types.PollAnswerVoters(option=b"\xff\x00", correct=revealed, voters=9),
                    types.PollAnswerVoters(option=b"", correct=False, voters=1),
                ],
            ),
        )
    )
    assert result is not None and result.results is not None
    assert result.results.answers is not None
    assert [item.is_correct for item in result.results.answers] == (
        [True, False] if quiz and revealed else [None, None]
    )


@pytest.mark.parametrize("solution", [None, "", "🦉 Explanation"])
def test_poll_solution_preserves_absence_empty_text_and_entities(solution: str | None) -> None:
    result = normalize_poll(
        types.MessageMediaPoll(
            raw_poll(quiz=True),
            types.PollResults(
                solution=solution,
                solution_entities=None if solution is None else [types.MessageEntityBold(0, 2)],
            ),
        )
    )
    assert result is not None and result.results is not None
    if solution is None:
        assert result.results.solution is None
    else:
        assert result.results.solution is not None
        assert result.results.solution.text == solution
        assert result.results.solution.entities[0].length == 2


def test_poll_flags_and_closing_metadata_are_preserved_without_time_inference() -> None:
    date = BASE.astimezone(timezone(timedelta(hours=3)))
    result = normalize_poll(
        types.MessageMediaPoll(
            raw_poll(
                public_voters=True,
                multiple_choice=True,
                open_answers=True,
                revoting_disabled=True,
                shuffle_answers=True,
                hide_results_until_close=True,
                close_period=0,
                close_date=date,
            ),
            types.PollResults(),
        )
    )
    assert result is not None and not result.is_closed
    assert result.public_voters and result.multiple_choice and result.allows_new_answers
    assert result.revoting_disabled and result.shuffle_answers and result.hide_results_until_close
    assert result.close_period_seconds == 0
    assert result.closes_at is not None
    assert result.closes_at == BASE and result.closes_at.tzinfo == UTC
    assert not result.is_quiz


def test_poll_does_not_change_caption_content_type_or_source_metadata() -> None:
    result = normalize_message(
        raw_message(media=types.MessageMediaPoll(raw_poll(), types.PollResults())), CHAT
    )
    assert result.poll is not None
    assert result.text == "Original 🦉 caption" and result.text_total_length == len(result.text)
    assert result.content_type == "other" and result.attachments == []
    assert result.url == "https://t.me/example/10"


def test_embedded_reactors_voters_and_repliers_are_excluded() -> None:
    actor = types.PeerUser(999888777)
    poll = raw_poll(public_voters=True, open_answers=True)
    assert isinstance(poll.answers[0], types.PollAnswer)
    poll.answers[0].added_by = actor
    poll.answers[0].date = BASE
    result = normalize_message(
        raw_message(
            reactions=types.MessageReactions(
                results=[types.ReactionCount(types.ReactionPaid(), 12)],
                recent_reactions=[
                    types.MessagePeerReaction(actor, BASE, types.ReactionEmoji("👍"))
                ],
                top_reactors=[types.MessageReactor(count=12, peer_id=actor, my=True)],
            ),
            replies=types.MessageReplies(replies=8, replies_pts=1, recent_repliers=[actor]),
            media=types.MessageMediaPoll(
                poll,
                types.PollResults(
                    total_voters=5,
                    recent_voters=[actor],
                    results=[types.PollAnswerVoters(option=b"", recent_voters=[actor])],
                ),
            ),
        ),
        CHAT,
    )
    serialized = result.model_dump_json()
    for forbidden in ("999888777", "recent_reactions", "top_reactors", "recent_voters", "added_by"):
        assert forbidden not in serialized
    assert result.reply_count == 8
    assert result.poll is not None and result.poll.results is not None
    assert result.poll.results.answers is not None
    assert result.poll.results.answers[0].voter_count is None


def test_entity_mapping_preserves_constructor_specific_fields_and_native_offsets() -> None:
    entities = normalize_entities(
        [
            types.MessageEntityBold(offset=2, length=4),
            types.MessageEntityPre(offset=2, length=4, language="python"),
            types.MessageEntityMentionName(offset=2, length=4, user_id=2**62),
            types.MessageEntityCustomEmoji(offset=0, length=2, document_id=2**62 + 1),
            types.MessageEntityBlockquote(offset=2, length=4),
            types.MessageEntityBlockquote(offset=2, length=4, collapsed=True),
            types.MessageEntityFormattedDate(
                offset=2, length=4, date=BASE.replace(tzinfo=None), relative=True
            ),
            types.MessageEntityDiffReplace(offset=2, length=4, old_text="previous"),
            types.InputMessageEntityMentionName(
                offset=2, length=4, user_id=types.InputUser(12, 999)
            ),
        ]
    )
    assert entities[0].collapsed is None and entities[0].relative is None
    assert entities[1].language == "python"
    assert entities[2].user_id == str(2**62)
    assert entities[3].document_id == str(2**62 + 1) and entities[3].length == 2
    assert entities[4].collapsed is False and entities[5].collapsed is True
    assert entities[6].date == BASE and entities[6].relative is True
    assert entities[6].short_time is False and entities[6].long_time is False
    assert entities[6].short_date is False and entities[6].long_date is False
    assert entities[6].day_of_week is False
    assert entities[7].old_text == "previous"
    assert entities[8].user_id is None
    assert all(
        entity.offset == 2 for entity in entities if entity.type != "MessageEntityCustomEmoji"
    )


@pytest.mark.parametrize("method", ["messages", "history", "search", "pinned", "thread"])
async def test_existing_backend_paths_preserve_metadata_without_extra_rpc(method: str) -> None:
    backend = TelegramBackend(Settings())
    backend.chats[CHAT.id] = CHAT
    source = raw_message(
        views=10,
        forwards=0,
        reactions=types.MessageReactions(
            results=[types.ReactionCount(types.ReactionEmoji("👍"), 2)]
        ),
        media=types.MessageMediaPoll(raw_poll(), types.PollResults(total_voters=0)),
        entities=[types.MessageEntityCustomEmoji(9, 2, 1234)],
    )
    client = AsyncMock()
    client.get_messages.return_value = [source]
    backend.client = client
    if method == "messages":
        message = (await backend.messages(CHAT.id, [10]))[10]
    else:
        kwargs: dict[str, Any] = {}
        if method == "search":
            kwargs["query"] = "caption"
        elif method == "pinned":
            kwargs["pinned_only"] = True
        elif method == "thread":
            kwargs["thread_id"] = 8
        message = (await backend.fetch(CHAT.id, **kwargs)).messages[0]
    assert message is not None and message.view_count == 10 and message.forward_count == 0
    assert message.reactions is not None and message.reactions.results[0].count == 2
    assert message.poll is not None and message.poll.results is not None
    assert message.poll.results.total_voters == 0
    assert message.entities[0].document_id == "1234"
    client.get_messages.assert_awaited_once()
    assert len(client.mock_calls) == 1
