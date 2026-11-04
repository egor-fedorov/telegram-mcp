"""Normalize the public TL button variants without exposing callback payloads."""

import hashlib
from typing import Any

from telethon import types

from telegram_mcp.domain.models import MessageButton, ReplyMarkup


def button_kind(button: Any) -> str:
    value = getattr(button, "type", button)
    name = type(value).__name__
    for prefix in ("InlineButtonType", "ButtonType", "KeyboardButton"):
        name = name.removeprefix(prefix)
    return {
        "Url": "url",
        "Callback": "callback",
        "UrlAuth": "url_auth",
        "WebView": "web_app",
        "SimpleWebView": "web_app",
        "Buy": "buy",
        "RequestPhone": "request_phone",
        "RequestGeoLocation": "request_location",
        "SwitchInline": "switch_inline",
        "Game": "game",
        "Copy": "copy",
        "UserProfile": "user_profile",
        "Text": "text",
        "Disabled": "disabled",
    }.get(name, "unknown")


def button_id(button: Any, row: int, column: int) -> str:
    # Opaque bytes also distinguish callbacks that keep their old label.
    payload = bytes(button)
    digest = hashlib.sha256(payload).hexdigest()
    return f"{row}:{column}:{digest}"


def normalize_markup(raw: Any) -> ReplyMarkup | None:
    kinds = {
        types.ReplyInlineMarkup: "inline",
        types.ReplyKeyboardMarkup: "keyboard",
        types.ReplyKeyboardHide: "hide",
        types.ReplyKeyboardForceReply: "force_reply",
    }
    kind = kinds.get(type(raw))
    if kind is None:
        return None
    rows = []
    for ri, row in enumerate(getattr(raw, "rows", [])):
        buttons = []
        for ci, button in enumerate(row.buttons):
            value = getattr(button, "type", button)
            buttons.append(
                MessageButton(
                    button_id=button_id(button, ri, ci),
                    text=button.text,
                    type=button_kind(button),
                    url=getattr(value, "url", None),
                )
            )
        rows.append(buttons)
    return ReplyMarkup.model_validate({"type": kind, "rows": rows})
