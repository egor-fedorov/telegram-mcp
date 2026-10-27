"""Wire DTOs mirror same-named domain models except for documented projections."""

import inspect

from pydantic import BaseModel
from typing_extensions import get_type_hints, is_typeddict

from telegram_mcp.adapters import wire
from telegram_mcp.domain import changes, models, watches

# Compact responses move message chats into one response-level map and bound scope IDs.
CHATS = {"chats"}
PROJECTIONS: dict[str, tuple[set[str], set[str]]] = {
    "Scope": (
        {"chat_count", "chat_ids_truncated", "pending_chat_count", "pending_chat_ids_truncated"},
        set(),
    ),
    "Message": (set(), {"chat"}),
    "MessageResult": (CHATS, set()),
    "Page": (CHATS, set()),
    "AlbumResult": (CHATS, set()),
    "ContextResult": (CHATS, set()),
    "ChangesResult": (CHATS, set()),
    "WatchChangesResult": (CHATS, set()),
}


def domain_models() -> dict[str, type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for module in (models, changes, watches):
        for name, value in vars(module).items():
            if inspect.isclass(value) and issubclass(value, BaseModel):
                found.setdefault(name, value)
    return found


def test_wire_fields_match_domain_models():
    domain = domain_models()
    compared = set()
    for name, value in vars(wire).items():
        if not (inspect.isclass(value) and is_typeddict(value)) or name not in domain:
            continue
        compared.add(name)
        added, removed = PROJECTIONS.get(name, (set(), set()))
        expected = set(domain[name].model_fields) - removed | added
        assert set(get_type_hints(value)) == expected, name
    assert set(PROJECTIONS) <= compared
    assert len(compared) >= 40
