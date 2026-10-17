"""HTTP DTOs for Pix input and explainable decision output."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


DOCUMENT_PATTERN = r"^(?:\d{11}|\d{3}\.\d{3}\.\d{3}-\d{2}|\d{14}|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})$"


class Geolocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class AccountOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_number: str = Field(pattern=DOCUMENT_PATTERN)

    @field_validator("document_number")
    @classmethod
    def normalize_document_number(cls, value: str) -> str:
        return "".join(character for character in value if character.isdigit())


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


class DestinationAccount(Account):
    participant: str = Field(pattern=r"^\d{8}$")


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
    destination_account: DestinationAccount
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
