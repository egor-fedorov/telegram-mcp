"""Normalize metadata already included in Telegram message objects without RPCs."""

from datetime import UTC, datetime
from typing import Any, Literal

from telethon import types

from telegram_mcp.models import (
    Entity,
    FormattedText,
    PollAnswerResult,
    PollData,
    PollOption,
    PollResults,
    ReactionCount,
    ReactionSummary,
)


def utc_date(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def normalize_entities(raw: Any) -> list[Entity]:
    entities = []
    for entity in raw or []:
        user_id = getattr(entity, "user_id", None)
        document_id = getattr(entity, "document_id", None)
        entities.append(
            Entity(
                type=type(entity).__name__,
                offset=entity.offset,
                length=entity.length,
                url=getattr(entity, "url", None),
                language=getattr(entity, "language", None),
                user_id=str(user_id) if type(user_id) is int else None,
                document_id=str(document_id) if document_id is not None else None,
                collapsed=bool(entity.collapsed) if hasattr(entity, "collapsed") else None,
                date=utc_date(getattr(entity, "date", None)),
                relative=bool(entity.relative) if hasattr(entity, "relative") else None,
                short_time=bool(entity.short_time) if hasattr(entity, "short_time") else None,
                long_time=bool(entity.long_time) if hasattr(entity, "long_time") else None,
                short_date=bool(entity.short_date) if hasattr(entity, "short_date") else None,
                long_date=bool(entity.long_date) if hasattr(entity, "long_date") else None,
                day_of_week=bool(entity.day_of_week) if hasattr(entity, "day_of_week") else None,
                old_text=getattr(entity, "old_text", None),
            )
        )
    return entities


def formatted_text(raw: types.TextWithEntities) -> FormattedText:
    return FormattedText(text=raw.text, entities=normalize_entities(raw.entities))


def normalize_reactions(raw: types.MessageReactions | None) -> ReactionSummary | None:
    if raw is None:
        return None
    is_min = bool(raw.min)
    results = []
    for item in raw.results:
        reaction = item.reaction
        kind: Literal["emoji", "custom_emoji", "paid", "empty", "unknown"] = "unknown"
        emoji = None
        document_id = None
        if isinstance(reaction, types.ReactionEmoji):
            kind, emoji = "emoji", reaction.emoticon
        elif isinstance(reaction, types.ReactionCustomEmoji):
            kind, document_id = "custom_emoji", str(reaction.document_id)
        elif isinstance(reaction, types.ReactionPaid):
            kind = "paid"
        elif isinstance(reaction, types.ReactionEmpty):
            kind = "empty"
        personal = not is_min and kind in ("emoji", "custom_emoji")
        results.append(
            ReactionCount(
                type=kind,
                emoji=emoji,
                document_id=document_id,
                count=item.count,
                is_chosen=(item.chosen_order is not None) if personal else None,
                chosen_order=item.chosen_order if personal else None,
            )
        )
    return ReactionSummary(
        is_min=is_min, reactions_as_tags=bool(raw.reactions_as_tags), results=results
    )


def normalize_poll(raw: Any) -> PollData | None:
    if not isinstance(raw, types.MessageMediaPoll):
        return None
    poll = raw.poll
    source_results = raw.results
    results = None
    if source_results is not None:
        is_min = bool(source_results.min)
        answers = None
        if source_results.results is not None:
            revealed = bool(poll.quiz) and any(item.correct for item in source_results.results)
            answers = [
                PollAnswerResult(
                    option_id=item.option.hex(),
                    voter_count=item.voters,
                    is_chosen=None if is_min else bool(item.chosen),
                    is_correct=bool(item.correct) if revealed else None,
                )
                for item in source_results.results
            ]
        solution = None
        if source_results.solution is not None:
            solution = FormattedText(
                text=source_results.solution,
                entities=normalize_entities(source_results.solution_entities),
            )
        results = PollResults(
            is_min=is_min,
            total_voters=source_results.total_voters,
            answers=answers,
            solution=solution,
        )
    return PollData(
        id=str(poll.id),
        question=formatted_text(poll.question),
        answers=[
            PollOption(option_id=answer.option.hex(), text=formatted_text(answer.text))
            for answer in poll.answers
        ],
        is_closed=bool(poll.closed),
        is_quiz=bool(poll.quiz),
        public_voters=bool(poll.public_voters),
        multiple_choice=bool(poll.multiple_choice),
        allows_new_answers=bool(poll.open_answers),
        revoting_disabled=bool(poll.revoting_disabled),
        shuffle_answers=bool(poll.shuffle_answers),
        hide_results_until_close=bool(poll.hide_results_until_close),
        close_period_seconds=poll.close_period,
        closes_at=utc_date(poll.close_date),
        results=results,
    )
