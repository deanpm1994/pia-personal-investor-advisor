"""Validated contracts for private manual-account metadata and ledger facts."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class AccountRole(StrEnum):
    """The account roles defined by ADR 0007."""

    BROKERAGE = "brokerage"
    CASH = "cash"
    SAVINGS = "savings"
    EMERGENCY_RESERVE = "emergency_reserve"


def _positive_decimal_string(value: object) -> Decimal:
    """Accept source decimal strings only; binary floats are never ledger inputs."""
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise ValueError("must be a decimal string; floats are not accepted")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("must be a valid decimal value") from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError("must be finite and greater than zero")
    return amount


def _account_name(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class ManualAccountCreate(BaseModel):
    """Create immutable-role account metadata without an economic balance override."""

    model_config = ConfigDict(extra="forbid")

    name: str
    role: AccountRole
    emergency_reserve_target_eur: Decimal | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _account_name(value)

    @field_validator("emergency_reserve_target_eur", mode="before")
    @classmethod
    def validate_target(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return _positive_decimal_string(value)

    @model_validator(mode="after")
    def limit_target_to_emergency_reserves(self) -> "ManualAccountCreate":
        if (
            self.emergency_reserve_target_eur is not None
            and self.role is not AccountRole.EMERGENCY_RESERVE
        ):
            raise ValueError(
                "emergency_reserve_target_eur requires an emergency_reserve account"
            )
        return self


class ManualAccountUpdate(BaseModel):
    """Editable metadata only; roles and economic history remain immutable."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    emergency_reserve_target_eur: Decimal | None = None

    @field_validator("name")
    @classmethod
    def validate_optional_name(cls, value: str | None) -> str | None:
        return _account_name(value) if value is not None else None

    @field_validator("emergency_reserve_target_eur", mode="before")
    @classmethod
    def validate_optional_target(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return _positive_decimal_string(value)

    @model_validator(mode="after")
    def require_an_edit(self) -> "ManualAccountUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one metadata field is required")
        return self


class CashMovementCommand(BaseModel):
    """One exact cash fact for an opening balance, deposit, or withdrawal."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal
    currency: str
    occurred_at: datetime
    kind: str = "deposit"

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> Decimal:
        return _positive_decimal_string(value)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        if len(value) != 3 or not value.isascii() or not value.isupper():
            raise ValueError("must be an uppercase ISO-4217 currency code")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_an_instant(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone offset")
        return value


class TransferCommand(CashMovementCommand):
    """A same-owner movement represented by two linked immutable cash events."""

    from_account_id: str
    to_account_id: str

    @model_validator(mode="after")
    def require_distinct_accounts(self) -> "TransferCommand":
        if self.from_account_id == self.to_account_id:
            raise ValueError("transfer accounts must differ")
        return self


class CorrectionCommand(CashMovementCommand):
    """An explicit append-only correction or reversal of one manual cash fact."""

    target_event_id: str
    mode: str
    direction: str | None = None

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in {"correction", "reversal"}:
            raise ValueError("must be correction or reversal")
        return value

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, value: str | None) -> str | None:
        if value is not None and value not in {"in", "out"}:
            raise ValueError("must be in or out")
        return value

    @model_validator(mode="after")
    def require_correction_direction(self) -> "CorrectionCommand":
        if self.mode == "correction" and self.direction is None:
            raise ValueError("a correction requires direction")
        if self.mode == "reversal" and self.direction is not None:
            raise ValueError("a reversal derives direction from its target")
        return self
