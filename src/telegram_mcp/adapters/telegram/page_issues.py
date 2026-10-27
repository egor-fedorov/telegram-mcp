"""Distinct incomplete-page issues collected while mapping one Telegram response."""

from telegram_mcp.domain.models import Issue, ToolError


class PageIssues:
    def __init__(self, chat_id: str, code: str):
        self.chat_id, self.code = chat_id, code
        self.items: list[Issue] = []

    def incomplete(self, detail: str) -> None:
        issue = ToolError(self.code, detail).issue(self.chat_id)
        if issue not in self.items:
            self.items.append(issue)
