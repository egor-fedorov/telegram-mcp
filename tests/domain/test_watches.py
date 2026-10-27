"""The archive status of a pending batch is derived, not stored."""

from datetime import UTC, datetime

from telegram_mcp.domain.models import SavedFile
from telegram_mcp.domain.watches import (
    BatchArchive,
    PendingDelivery,
    WatchChangesResult,
    WatchPosition,
)

KEY = "k" * 43
SAVED = SavedFile(
    local_path="/archive/k/file.jsonl",
    metadata_path="/archive/k/metadata.json",
    size=1,
    sha256="0" * 64,
    saved_at=datetime(2026, 10, 1, tzinfo=UTC),
)


def pending(archive: BatchArchive | None) -> PendingDelivery:
    return PendingDelivery(
        result=WatchChangesResult(name="jobs", batch_id="b" * 43),
        position=WatchPosition(),
        source_ids=[],
        archive=archive,
    )


def test_archive_status_follows_reservation_and_publication():
    assert pending(None).archive_status == "not_requested"
    assert pending(None).saved_copy is None
    assert pending(BatchArchive(key=KEY)).archive_status == "reserved"
    saved = pending(BatchArchive(key=KEY, saved_file=SAVED))
    assert saved.archive_status == "saved" and saved.saved_copy == SAVED


def test_derived_status_is_not_persisted():
    dumped = pending(BatchArchive(key=KEY)).model_dump()
    assert "archive_status" not in dumped and "saved_copy" not in dumped
