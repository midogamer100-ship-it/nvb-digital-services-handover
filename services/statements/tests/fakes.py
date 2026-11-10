"""Test doubles: an in-memory Core Adapter with a tiny ledger (synthetic data)."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from nvb_statements.adapter_client import AdapterCallError, Identity


def account(account_id: int, last4: str = "1234") -> dict:
    return {"account_id": account_id, "masked_number": f"•••• {last4}", "type": "current", "nickname": "Everyday",
            "currency": "EGP", "status": "active", "opened_at": "2026-01-01T09:00:00+00:00"}


class FakeAdapter:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.owners: dict[int, str] = {}
        self.accounts: dict[int, dict] = {}
        self.ledger: dict[int, list[dict]] = {}
        self.unavailable = False
        self.fail_manifest = 0
        self.mutate_after_first_page = False
        self.calls: list[str] = []
        self._next_id = 1

    # --- setup ---------------------------------------------------------------------------
    def add_customer(self, subject: str, accounts: list[dict], role: str = "customer") -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.sessions[token] = {"subject": subject, "role": role, "csrf": csrf, "accounts": accounts}
        for a in accounts:
            self.owners[a["account_id"]] = subject
            self.accounts[a["account_id"]] = a
            self.ledger.setdefault(a["account_id"], [])
        return token, csrf

    def post(self, account_id: int, posted_at: datetime, direction: str, amount: int, desc: str = "Synthetic") -> None:
        self.ledger[account_id].append({"entry_id": self._next_id, "posted_at": posted_at.astimezone(UTC),
                                        "direction": direction, "amount_minor": amount, "description": desc,
                                        "counterparty": "Demo", "reference": f"REF{self._next_id:06d}",
                                        "type": "deposit", "category": "Test"})
        self._next_id += 1

    # --- adapter API -----------------------------------------------------------------------
    def close(self) -> None:
        pass

    def _check(self) -> None:
        if self.unavailable:
            raise AdapterCallError(503, "core_unavailable", "down", True)

    def resolve(self, session_token, csrf_token, request_id):
        self.calls.append("resolve")
        self._check()
        s = self.sessions.get(session_token)
        if s is None:
            return None
        csrf_valid = None if csrf_token is None else (csrf_token == s["csrf"])
        return Identity(subject=s["subject"], role=s["role"], username=s["subject"], display_name="Test",
                        csrf_valid=csrf_valid, accounts=list(s["accounts"]) if s["role"] == "customer" else [])

    def _rows(self, account_id, subject, start, end):
        if self.owners.get(account_id) != subject:
            raise AdapterCallError(404, "not_found", "Not found.", False)
        rows = sorted(self.ledger[account_id], key=lambda e: (e["posted_at"], e["entry_id"]))
        return rows, [e for e in rows if start <= e["posted_at"] < end]

    def manifest(self, account_id, subject, start, end, request_id=None):
        self.calls.append("manifest")
        self._check()
        if self.fail_manifest:
            self.fail_manifest -= 1
            raise AdapterCallError(503, "core_busy", "busy", True)
        rows, period = self._rows(account_id, subject, start, end)
        signed = lambda e: e["amount_minor"] if e["direction"] == "credit" else -e["amount_minor"]  # noqa: E731
        opening = sum(signed(e) for e in rows if e["posted_at"] < start)
        credits = sum(e["amount_minor"] for e in period if e["direction"] == "credit")
        debits = sum(e["amount_minor"] for e in period if e["direction"] == "debit")
        core = {"account_id": account_id, "start": start.isoformat(), "end": end.isoformat(), "o": opening,
                "c": credits, "d": debits, "n": len(period), "ids": [e["entry_id"] for e in period]}
        return {"dataset_version": "statement-v1",
                "dataset_id": hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest(),
                "account_id": account_id, "period_start": start.astimezone(UTC).isoformat(),
                "period_end": end.astimezone(UTC).isoformat(), "boundary": "[start,end) UTC",
                "ordering": "posted_at ASC, id ASC", "currency": "EGP", "opening_balance_minor": opening,
                "closing_balance_minor": opening + credits - debits, "credit_total_minor": credits,
                "debit_total_minor": debits, "entry_count": len(period),
                "first_entry_id": period[0]["entry_id"] if period else None,
                "last_entry_id": period[-1]["entry_id"] if period else None,
                "account": {**self.accounts[account_id], "holder_name": "Synthetic <Holder> & Co"},
                "account_opened_in_period": False, "account_opened_after_period": False,
                "generated_at": datetime.now(UTC).isoformat()}

    def entries(self, account_id, subject, start, end, cursor, limit, request_id=None):
        self.calls.append("entries")
        self._check()
        _rows, period = self._rows(account_id, subject, start, end)
        offset = int(cursor or 0)
        chunk = period[offset:offset + limit]
        if self.mutate_after_first_page and offset == 0:
            self.mutate_after_first_page = False
            self.post(account_id, start + timedelta(seconds=1), "credit", 5, "late posting")
        has_more = offset + limit < len(period)
        items = [{**e, "posted_at": e["posted_at"].isoformat(),
                  "signed_amount_minor": e["amount_minor"] if e["direction"] == "credit" else -e["amount_minor"],
                  "balance_after_minor": 0} for e in chunk]
        return {"items": items, "has_more": has_more, "next_cursor": str(offset + limit) if has_more else None}
