"""HTTP DTOs for Pix input and explainable decision output."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Geolocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class AccountOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_number: str = Field(min_length=11, max_length=18)


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch: str = Field(min_length=1, max_length=20)
    account_number: str = Field(min_length=1, max_length=30)
    opening_date: datetime
    owner: AccountOwner

    @field_validator("opening_date")
    @classmethod
    def require_opening_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "opening_date")


class TransactionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=255)
    channel: str = Field(min_length=1, max_length=30)
    ip: str = Field(min_length=1, max_length=45)
    geolocation: Geolocation | None = None


class PixTransactionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=255)
    amount: int = Field(gt=0)
    transaction_date: datetime
    source_account: Account
    destination_account: Account
    source: TransactionSource

    @field_validator("transaction_date")
    @classmethod
    def require_transaction_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "transaction_date")

def _require_timezone(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone")
    return value


class RuleResult(BaseModel):
    name: str
    status: Literal["approved", "rejected", "two_factor_required"]
    reason: str
    metadata: dict[str, int | str]


class PixDecision(BaseModel):
    id: str
    status: Literal["approved", "rejected", "two_factor_required"]
    reason: str
    metadata: dict[str, list[RuleResult]]
