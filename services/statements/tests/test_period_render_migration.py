from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from nvb_statements.period import PeriodError, month_period
from nvb_statements.render import fmt_minor, render_statement


@pytest.mark.parametrize("label,start,end", [
    ("2026-01", datetime(2025, 12, 31, 22, tzinfo=UTC), datetime(2026, 1, 31, 22, tzinfo=UTC)),  # EET UTC+2
    ("2026-07", datetime(2026, 6, 30, 21, tzinfo=UTC), datetime(2026, 7, 31, 21, tzinfo=UTC)),   # EEST UTC+3
    ("2026-12", datetime(2026, 11, 30, 22, tzinfo=UTC), datetime(2026, 12, 31, 22, tzinfo=UTC)), # year roll
])
def test_cairo_month_to_utc(label, start, end):
    p = month_period(label, "Africa/Cairo")
    assert (p.start_utc, p.end_utc) == (start, end)


def test_dst_transition_month_has_correct_length():
    p = month_period("2026-04", "Africa/Cairo")   # DST starts on the last Friday of April
    assert p.start_utc == datetime(2026, 3, 31, 22, tzinfo=UTC)
    assert p.end_utc == datetime(2026, 4, 30, 21, tzinfo=UTC)


def test_utc_timezone_option():
    p = month_period("2026-02", "UTC")
    assert (p.start_utc, p.end_utc) == (datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC))


def test_invalid_label():
    with pytest.raises(PeriodError):
        month_period("2026-00", "Africa/Cairo")


def test_money_format_is_exact_and_rejects_float():
    assert fmt_minor(123456789, "EGP") == "1,234,567.89 EGP"
    assert fmt_minor(-1) == "-0.01"
    with pytest.raises(TypeError):
        fmt_minor(10.5)


def test_markup_in_data_is_escaped_and_no_external_fetch():
    manifest = {"currency": "EGP", "account": {"masked_number": "•••• 1", "type": "current", "nickname": "<b>x</b>",
                                               "holder_name": '<img src="http://evil.invalid/x.png"/>'},
                "period_start": "2026-08-31T21:00:00+00:00", "period_end": "2026-09-30T21:00:00+00:00",
                "opening_balance_minor": 0, "credit_total_minor": 5, "debit_total_minor": 0,
                "closing_balance_minor": 5, "entry_count": 1, "dataset_id": "f" * 64,
                "account_opened_after_period": False}
    entries = [{"posted_at": "2026-09-02T10:00:00+00:00", "reference": "R1", "direction": "credit",
                "amount_minor": 5, "description": '<a href="file:///etc/passwd">click</a> & <font size=99>',
                "counterparty": "<script>"}]
    pdf = render_statement(manifest=manifest, entries=entries, period_label="2026-09", timezone="Africa/Cairo",
                           request_id="r")
    assert pdf.startswith(b"%PDF")
    assert b"/URI" not in pdf and b"/Launch" not in pdf   # no link/launch actions were created
    from pypdf import PdfReader
    import io
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert '<img src="http://evil.invalid/x.png"/>' in text   # shown as literal text, not interpreted
    assert "<script>" in text


def test_running_balance_must_reach_closing():
    manifest = {"currency": "EGP", "account": {"masked_number": "•••• 1", "type": "current", "nickname": "n"},
                "period_start": "2026-08-31T21:00:00+00:00", "period_end": "2026-09-30T21:00:00+00:00",
                "opening_balance_minor": 0, "credit_total_minor": 0, "debit_total_minor": 0,
                "closing_balance_minor": 1, "entry_count": 0, "dataset_id": "f" * 64}
    with pytest.raises(ValueError):
        render_statement(manifest=manifest, entries=[], period_label="2026-09", timezone="UTC", request_id="r")


def test_migration_upgrade_on_sqlite(tmp_path):
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    cfg = Config(str(root / "migrations" / "statements" / "alembic.ini"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"statement_requests", "statement_events", "outbox_messages", "alembic_version_statements"} <= tables
    with pytest.raises(RuntimeError):
        command.downgrade(cfg, "base")
