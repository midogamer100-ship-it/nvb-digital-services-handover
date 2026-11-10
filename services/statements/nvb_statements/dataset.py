"""Fetch a complete, verified statement dataset from the Core Adapter.

Implements the caller side of the ``statement-v1`` contract: page every entry with the
keyset cursor, verify count/totals/ordering/boundaries against the manifest, then re-read
the manifest and require the same ``dataset_id``. A mismatch is retryable and the request is
never rendered from partial data. No core transaction is held between pages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nvb_statements.adapter_client import AdapterClient


class DatasetIncomplete(Exception):
    """Retryable: the dataset changed or did not reconcile while it was being read."""


class DatasetTooLarge(Exception):
    """Non-retryable: above the configured statement size limit."""


@dataclass
class Dataset:
    manifest: dict
    entries: list[dict]


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fetch_verified(adapter: AdapterClient, *, account_id: int, subject: str, start: datetime, end: datetime,
                   page_size: int, max_entries: int, request_id: str | None = None,
                   expected_dataset_id: str | None = None) -> Dataset:
    m = adapter.manifest(account_id, subject, start, end, request_id)
    if m["entry_count"] > max_entries:
        raise DatasetTooLarge(f"{m['entry_count']} entries exceeds the limit")
    if expected_dataset_id and m["dataset_id"] != expected_dataset_id:
        raise DatasetIncomplete("dataset changed since a previous attempt")

    entries: list[dict] = []
    cursor = None
    while True:
        page = adapter.entries(account_id, subject, start, end, cursor, page_size, request_id)
        entries.extend(page["items"])
        if len(entries) > m["entry_count"]:
            raise DatasetIncomplete("more entries than the manifest declared")
        if not page["has_more"]:
            break
        if not page["next_cursor"]:
            raise DatasetIncomplete("has_more without a cursor")
        cursor = page["next_cursor"]

    credits = debits = 0
    prev = None
    seen: set[int] = set()
    for e in entries:
        key = (_ts(e["posted_at"]), e["entry_id"])
        if prev is not None and key <= prev:
            raise DatasetIncomplete("entries out of order or duplicated")
        prev = key
        if e["entry_id"] in seen:
            raise DatasetIncomplete("duplicate entry")
        seen.add(e["entry_id"])
        if not (start <= key[0] < end):
            raise DatasetIncomplete("entry outside the period")
        if not isinstance(e["amount_minor"], int) or e["amount_minor"] <= 0:
            raise DatasetIncomplete("invalid amount")
        if e["direction"] == "credit":
            credits += e["amount_minor"]
        else:
            debits += e["amount_minor"]

    if len(entries) != m["entry_count"] or credits != m["credit_total_minor"] or debits != m["debit_total_minor"]:
        raise DatasetIncomplete("entries do not reproduce the manifest totals")
    if m["opening_balance_minor"] + credits - debits != m["closing_balance_minor"]:
        raise DatasetIncomplete("opening + movements != closing")

    again = adapter.manifest(account_id, subject, start, end, request_id)
    if again["dataset_id"] != m["dataset_id"]:
        raise DatasetIncomplete("dataset changed while paging")
    return Dataset(m, entries)
