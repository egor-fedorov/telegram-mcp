"""Conservative, local recognition of authentication notices, never their secrets."""

import re
import unicodedata

_CONTEXT = re.compile(
    r"(?:\b(?:verification|authentication|authorization|login|sign[ -]?in|one[ -]?time|security)"
    r"(?:\s+\w+){0,3}\s+(?:code|password|link)\b|"
    r"\b(?:code|password|link)\s+(?:to|for)\s+(?:log|sign|verify|auth)|"
    r"\b(?:код|пароль|ссылка)\w*\s+(?:(?:для|на|вашего)\s+)?"
    r"(?:вход|авторизац|аутентификац|подтвержден|верификац)|"
    r"\b(?:вход|авторизац|подтвержден)\w*[^\n]{0,32}\bкод|"
    r"\b(?:одноразов|проверочн)\w*\s+(?:код|пароль))",
    re.IGNORECASE,
)
_CODE = re.compile(r"(?<!\w)(?:\d[ -]?){4,10}(?!\w)")
_TOKEN_URL = re.compile(
    r"(?:https?://|tg://)[^\s<>]*(?:[?&#/](?:token|code|auth|otp|key|login_token)=)"
    r"[^\s&#<>]{4,}",
    re.IGNORECASE,
)
_LOGIN_CONTEXT = re.compile(
    r"\b(?:log[ -]?in|sign[ -]?in|authenticat|вход|войти|авторизац)", re.IGNORECASE
)


def authentication_notice(text: str) -> bool:
    """Require authentication context plus a credential-shaped value, not just digits."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Cf")
    return bool(
        (_CONTEXT.search(normalized) and _CODE.search(normalized))
        or (
            _TOKEN_URL.search(normalized)
            and (_CONTEXT.search(normalized) or _LOGIN_CONTEXT.search(normalized))
        )
    )
