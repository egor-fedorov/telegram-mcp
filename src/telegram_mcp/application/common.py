"""Validation and cursor fingerprints shared by application scenarios."""

import json

from telegram_mcp.domain.models import ToolError


def fingerprint(values: object) -> str:
    return json.dumps(values, sort_keys=True, default=str)


def check_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise ToolError("INVALID_ARGUMENT", "limit must be between 1 and 100.")


def check_topic_id(topic_id: int | None) -> None:
    if topic_id is not None and (type(topic_id) is not int or not 1 <= topic_id <= 2147483647):
        raise ToolError("INVALID_ARGUMENT", "topic_id must be an integer from 1 to 2147483647.")
