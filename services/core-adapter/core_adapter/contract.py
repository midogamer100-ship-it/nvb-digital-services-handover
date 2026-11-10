"""Extension-facing contract (core-adapter v1).

Core responses are validated against these models before they are returned, so a
core change that breaks the contract fails closed (502 core_contract_violation)
instead of leaking unexpected shapes to the extension services. Money is always
an integer count of minor units (StrictInt), never a float.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

CONTRACT_VERSION = "core-adapter-v1"
DATASET_VERSION = "statement-v2"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Account(_Model):
    account_id: StrictInt
    masked_number: str
    type: str
    nickname: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    status: str
    opened_at: datetime


class StatementAccount(Account):
    holder_name: str | None = None


class IdentityResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_token: str = Field(min_length=1, max_length=128)
    csrf_token: str | None = Field(default=None, max_length=128)
    include_accounts: bool = False


class Identity(_Model):
    active: bool
    subject: str | None = None
    username: str | None = None
    role: Literal["customer", "admin", "demo"] | None = None
    display_name: str | None = None
    full_name: str | None = None
    customer_number: str | None = None
    session_expires_at: datetime | None = None
    csrf_valid: bool | None = None
    accounts: list[Account] | None = None


class SubjectAccounts(_Model):
    subject: str
    accounts: list[Account]


class StatementManifest(_Model):
    dataset_version: Literal["statement-v2"]
    dataset_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    # SHA-256 over the canonical encoding of every entry of the period (core snapshot).
    entries_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: StrictInt
    period_start: datetime
    period_end: datetime
    boundary: Literal["[start,end) UTC"]
    ordering: Literal["posted_at ASC, id ASC"]
    currency: str
    opening_balance_minor: StrictInt
    closing_balance_minor: StrictInt
    credit_total_minor: StrictInt
    debit_total_minor: StrictInt
    entry_count: StrictInt
    first_entry_id: StrictInt | None
    last_entry_id: StrictInt | None
    account: StatementAccount
    account_opened_in_period: bool
    account_opened_after_period: bool
    generated_at: datetime


class StatementEntry(_Model):
    entry_id: StrictInt
    posted_at: datetime
    # Integer microseconds since the Unix epoch: the value hashed into entries_sha256.
    posted_at_us: StrictInt
    reference: str
    direction: Literal["debit", "credit"]
    type: str
    category: str
    amount_minor: StrictInt = Field(gt=0)
    signed_amount_minor: StrictInt
    balance_after_minor: StrictInt
    description: str
    counterparty: str


class StatementEntriesPage(_Model):
    items: list[StatementEntry]
    has_more: bool
    next_cursor: str | None


class DiscoveredAccount(Account):
    subject: str


class DiscoveryPage(_Model):
    items: list[DiscoveredAccount]
    has_more: bool
    next_after_account_id: StrictInt | None


class ErrorBody(_Model):
    code: str
    message: str
    retryable: bool = False
    request_id: str | None = None


class ErrorResponse(_Model):
    error: ErrorBody
