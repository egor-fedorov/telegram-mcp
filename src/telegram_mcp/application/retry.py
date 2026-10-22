"""Retry checkpoints with an injected monotonic time source."""

import math
from dataclasses import dataclass

from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.models import Issue, ToolError


@dataclass
class RetryState:
    issue: Issue | None = None
    retry_at: float = 0

    def ready(self, clock: Clock) -> bool:
        return clock.monotonic() >= self.retry_at

    def record(self, error: ToolError, chat_id: str | None, clock: Clock) -> None:
        self.issue = error.issue(chat_id)
        self.retry_at = clock.monotonic() + (error.retry_after or 0)

    def clear(self) -> None:
        self.issue = None
        self.retry_at = 0

    def current_issue(self, clock: Clock) -> Issue | None:
        if self.issue is None or self.issue.retry_after_seconds is None:
            return self.issue
        return self.issue.model_copy(
            update={"retry_after_seconds": max(0, math.ceil(self.retry_at - clock.monotonic()))}
        )
