"""get_history argument rules as a pure function, without a backend."""

from datetime import timedelta

import pytest

from telegram_mcp.application.queries import history_spec
from telegram_mcp.application.requests import BudgetRequest, HistoryRead, TopicRead
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import BASE

TO = BASE + timedelta(days=1)
DATES = {"date_from": BASE, "date_to": TO}


def test_single_chat_keeps_global_ordering_without_budget():
    result = history_spec(chat_id="1")
    assert result.read == HistoryRead(result.read.filters, "global")
    assert result.selection.chat_ids == ["1"] and result.budget is None


def test_plural_selectors_default_to_page_ordering_and_budget():
    result = history_spec(folder_ids=[7], date_from=BASE, date_to=TO)
    assert isinstance(result.read, HistoryRead) and result.read.ordering == "page"
    assert result.budget == BudgetRequest(20, None)
    assert result.selection.folder_ids == [7] and result.selection.chat_ids is None


def test_explicit_budget_applies_to_a_single_source():
    assert history_spec(chat_ref="@jobs", max_messages=5).budget == BudgetRequest(20, 5)


def test_topic_selects_a_topic_read():
    result = history_spec(chat_id="1", topic_id=3, order="oldest_first")
    assert isinstance(result.read, TopicRead) and result.read.topic_id == 3
    assert result.read.filters.oldest_first


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "exactly one"),
        ({"chat_id": "1", "folder_ids": [1]}, "exactly one"),
        ({"chat_ids": []}, "must not be empty"),
        ({"chat_id": "1", "limit": 0}, "limit"),
        ({"chat_id": "1", "topic_id": 0}, "topic_id"),
        ({"chat_id": "1", "ordering": "random"}, "ordering"),
        ({"chat_id": "1", "max_sources": 1001}, "1..1000"),
        ({"chat_ids": ["1"], "topic_id": 1, **DATES}, "single chat_id"),
        ({"folder_ids": [1], "date_from": BASE}, "explicit date_from and date_to"),
    ],
)
def test_invalid_arguments_fail_in_documented_order(kwargs, message):
    with pytest.raises(ToolError, match=message) as caught:
        history_spec(**kwargs)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_selector_error_precedes_limit_error():
    with pytest.raises(ToolError, match="exactly one"):
        history_spec(limit=0)
