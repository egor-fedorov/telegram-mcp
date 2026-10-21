"""Wall and monotonic time for the application's injected clock port."""

import time
from datetime import UTC, datetime


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
