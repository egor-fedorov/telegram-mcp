from telegram_mcp.domain.models import (
    Entity,
    FormattedText,
    Message,
    PollAnswerResult,
    PollData,
    PollOption,
    PollResults,
    ReactionCount,
    ReactionSummary,
)
from tests.support.fakes import BASE, msg

METADATA_FIELDS = ("view_count", "forward_count", "reply_count", "reactions", "poll", "entities")


def rich_message(mid: int = 20, text: str = "A😀B metadata") -> Message:
    message = msg("-1001", mid, text, content="other", topic_id=7)
    message.view_count = 123
    message.forward_count = 0
    message.reply_count = None
    message.entities = [Entity(type="MessageEntityBold", offset=1, length=2)]
    message.reactions = ReactionSummary(
        results=[
            ReactionCount(type="emoji", emoji="👍", count=3, is_chosen=True, chosen_order=0),
            ReactionCount(type="custom_emoji", document_id="9007199254740993", count=2),
            ReactionCount(type="paid", count=50),
        ]
    )
    message.poll = PollData(
        id="9007199254740995",
        question=FormattedText(
            text="A😀B?",
            entities=[
                Entity(type="MessageEntityCustomEmoji", offset=1, length=2, document_id="91")
            ],
        ),
        answers=[
            PollOption(option_id="00ff", text=FormattedText(text="Yes")),
            PollOption(option_id="80", text=FormattedText(text="No")),
        ],
        is_quiz=True,
        closes_at=BASE,
        results=PollResults(
            is_min=True,
            total_voters=0,
            answers=[
                PollAnswerResult(option_id="80", voter_count=0),
                PollAnswerResult(option_id="00ff"),
            ],
            solution=FormattedText(
                text="A😀B explanation",
                entities=[Entity(type="MessageEntityItalic", offset=1, length=2)],
            ),
        ),
    )
    return message


def assert_metadata(actual: dict[str, object], expected: Message) -> None:
    original = expected.model_dump(mode="json", exclude_none=True)

    def without_empty_entities(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: without_empty_entities(item)
                for key, item in value.items()
                if key != "entities" or item != []
            }
        if isinstance(value, list):
            return [without_empty_entities(item) for item in value]
        return value

    original = without_empty_entities(original)
    assert isinstance(original, dict)
    for field in METADATA_FIELDS:
        if field in original:
            assert actual[field] == original[field]
        else:
            assert field not in actual
