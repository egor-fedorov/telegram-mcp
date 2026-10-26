"""Resolve user-facing periods into fixed, aware, half-open intervals."""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram_mcp.domain.models import ToolError

_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*")


def load_zone(name: str) -> ZoneInfo:
    """Accept only IANA zone names that the installed zone data can resolve."""
    if not _ZONE.fullmatch(name) or len(name) > 64:
        raise ToolError("INVALID_ARGUMENT", "Use an IANA time zone name such as Europe/Moscow.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ToolError(
            "INVALID_ARGUMENT", f"Unknown time zone {name!r}; use an IANA zone name."
        ) from None


def local_midnight(day: date, zone: ZoneInfo) -> datetime:
    """Midnight of a calendar day, rejecting nonexistent or ambiguous local times."""
    first = datetime.combine(day, time(), tzinfo=zone)
    # A skipped local time does not survive a round trip; a repeated one has two offsets.
    round_trip = first.astimezone(ZoneInfo("UTC")).astimezone(zone)
    if round_trip.replace(tzinfo=None) != first.replace(tzinfo=None):
        raise ToolError(
            "INVALID_ARGUMENT",
            f"Midnight on {day.isoformat()} does not exist in {zone.key}; use aware timestamps.",
        )
    if first.utcoffset() != first.replace(fold=1).utcoffset():
        raise ToolError(
            "INVALID_ARGUMENT",
            f"Midnight on {day.isoformat()} is ambiguous in {zone.key}; use aware timestamps.",
        )
    return first


def _parse_bound(value: str) -> date | datetime:
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ToolError("INVALID_ARGUMENT", f"Invalid calendar date {value!r}.") from None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(
            "INVALID_ARGUMENT", "Dates must be YYYY-MM-DD or ISO timestamps with an offset."
        ) from None
    if parsed.utcoffset() is None:
        raise ToolError("INVALID_ARGUMENT", "Timestamps must include a UTC offset.")
    return parsed


def resolve_period(
    now: datetime,
    zone: ZoneInfo,
    *,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[datetime, datetime]:
    """Return [start, end) with end capped at the captured current time."""
    if (date_from is None) != (date_to is None):
        raise ToolError("INVALID_ARGUMENT", "Provide both date_from and date_to, or neither.")
    if days is not None and date_from is not None:
        raise ToolError("INVALID_ARGUMENT", "Use either days or date_from/date_to, not both.")
    if date_from is None or date_to is None:
        span = 7 if days is None else days
        if not 1 <= span <= 365:
            raise ToolError("INVALID_ARGUMENT", "days must be 1..365.")
        return now - timedelta(days=span), now
    lower, upper = _parse_bound(date_from), _parse_bound(date_to)
    if isinstance(lower, datetime) != isinstance(upper, datetime):
        raise ToolError(
            "INVALID_ARGUMENT", "Use two calendar dates or two aware timestamps, not a mix."
        )
    if isinstance(lower, datetime) and isinstance(upper, datetime):
        start, end = lower, upper
    else:
        assert not isinstance(lower, datetime) and not isinstance(upper, datetime)
        start, end = local_midnight(lower, zone), local_midnight(upper, zone)
    end = min(end, now)
    if start >= end:
        raise ToolError(
            "INVALID_ARGUMENT", "The period is empty or reversed after capping it at now."
        )
    return start, end
