"""Caller side of the core statement dataset contract ``statement-v2``.

The manifest is computed by the core in one database snapshot and carries
``entries_sha256``: a SHA-256 over the canonical encoding of every entry of the period.
Entries are then paged in separate short requests. This module recomputes the digest over
what was actually received and accepts the dataset only when

1. the entries are strictly ordered by ``(posted_at_us, entry_id)``, unique and inside the
   half-open period,
2. the recomputed digest equals the manifest's ``entries_sha256`` (the paged set is exactly
   the manifest snapshot's set - not merely the same count and totals),
3. count, credit/debit totals and ``opening + movements == closing`` hold, and
4. a second manifest read returns the same ``dataset_id`` (still stable after paging).

Anything else raises ``DatasetIncomplete`` (retry later). A partial or drifting dataset is
never returned. No core transaction is held between pages.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DATASET_VERSION = "statement-v2"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class DatasetIncomplete(Exception):
    """Retryable: the dataset changed or did not verify while it was being read."""


class DatasetTooLarge(Exception):
    """Non-retryable: above the configured statement size limit."""


@dataclass
class Dataset:
    manifest: dict
    entries: list[dict]


def micros(dt: datetime) -> int:
    return (dt.astimezone(UTC) - EPOCH) // timedelta(microseconds=1)


def canonical_entry_line(e: dict) -> bytes:
    """Must stay byte-identical to the core's ``integration_exports.canonical_entry_line``."""
    return (json.dumps([e["entry_id"], e["posted_at_us"], e["direction"], e["amount_minor"],
                        e["balance_after_minor"], e["reference"], e["type"], e["category"], e["description"],
                        e["counterparty"]], ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def entries_digest(entries: list[dict]) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(canonical_entry_line(e))
    return h.hexdigest()


def fetch_verified(adapter, *, account_id: int, subject: str, start: datetime, end: datetime,
                   page_size: int, max_entries: int, request_id: str | None = None,
                   on_page=None) -> Dataset:
    m = adapter.manifest(account_id, subject, start, end, request_id)
    if m.get("dataset_version") != DATASET_VERSION:
        raise DatasetIncomplete("unsupported dataset version")
    if m["entry_count"] > max_entries:
        raise DatasetTooLarge(f"{m['entry_count']} entries exceeds the limit")

    entries: list[dict] = []
    cursor = None
    while True:
        page = adapter.entries(account_id, subject, start, end, cursor, page_size, request_id)
        entries.extend(page["items"])
        if on_page is not None:
            on_page()
        if len(entries) > m["entry_count"]:
            raise DatasetIncomplete("more entries than the manifest declared")
        if not page["has_more"]:
            break
        if not page["next_cursor"]:
            raise DatasetIncomplete("has_more without a cursor")
        cursor = page["next_cursor"]

    start_us, end_us = micros(start), micros(end)
    credits = debits = 0
    prev = None
    for e in entries:
        if not isinstance(e.get("posted_at_us"), int) or isinstance(e["posted_at_us"], bool):
            raise DatasetIncomplete("entry without an integer timestamp")
        if micros(datetime.fromisoformat(e["posted_at"])) != e["posted_at_us"]:
            raise DatasetIncomplete("entry timestamp fields disagree")
        key = (e["posted_at_us"], e["entry_id"])
        if prev is not None and key <= prev:
            raise DatasetIncomplete("entries out of order or duplicated")
        prev = key
        if not (start_us <= key[0] < end_us):
            raise DatasetIncomplete("entry outside the period")
        amount = e["amount_minor"]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise DatasetIncomplete("invalid amount")
        if e["direction"] == "credit":
            credits += amount
        elif e["direction"] == "debit":
            debits += amount
        else:
            raise DatasetIncomplete("invalid direction")

    if entries_digest(entries) != m["entries_sha256"]:
        raise DatasetIncomplete("entries do not match the manifest snapshot digest")
    if len(entries) != m["entry_count"] or credits != m["credit_total_minor"] or debits != m["debit_total_minor"]:
        raise DatasetIncomplete("entries do not reproduce the manifest totals")
    if m["opening_balance_minor"] + credits - debits != m["closing_balance_minor"]:
        raise DatasetIncomplete("opening + movements != closing")

    again = adapter.manifest(account_id, subject, start, end, request_id)
    if again["dataset_id"] != m["dataset_id"]:
        raise DatasetIncomplete("dataset changed while paging")
    return Dataset(m, entries)
