"""Statement periods: a calendar month in a named timezone, converted to UTC instants.

The core contract is half-open UTC ``[start, end)``. We never reinterpret the core's
UTC filters as local days; instead the local month boundaries (00:00 local on the 1st)
are converted to UTC with the IANA database (DST aware), stored, and printed on the PDF.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

LABEL_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


class PeriodError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MonthPeriod:
    label: str
    timezone: str
    start_utc: datetime
    end_utc: datetime


def month_period(label: str, tz_name: str) -> MonthPeriod:
    m = LABEL_RE.match(label)
    if not m:
        raise PeriodError("invalid_period", "period must be YYYY-MM.")
    year, month = int(m.group(1)), int(m.group(2))
    tz = ZoneInfo(tz_name)
    start_local = datetime(year, month, 1, tzinfo=tz)
    end_local = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=tz)
    return MonthPeriod(label, tz_name, start_local.astimezone(UTC), end_local.astimezone(UTC))


def validate_requestable(p: MonthPeriod, *, now: datetime, settle_seconds: int, max_months_back: int) -> None:
    if p.end_utc > now - timedelta(seconds=settle_seconds):
        raise PeriodError("period_not_closed", "Statements are available once the month has ended.")
    if p.start_utc < now - timedelta(days=31 * max_months_back):
        raise PeriodError("period_too_old", f"Statements are available for the last {max_months_back} months.")
