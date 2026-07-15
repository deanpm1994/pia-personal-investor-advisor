"""Immutable, source-faithful financial-event contracts.

These models validate reported facts only. They do not calculate balances, lots,
gains, tax, prices, or foreign-exchange conversions.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CurrencyCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z]{3}$")]


def _source_decimal(value: object) -> Decimal:
    """Accept only Decimal values or their source string representation."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        raise ValueError("must be a Decimal or decimal string; floats are not accepted")
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("must be a valid decimal value") from error
    if not decimal_value.is_finite():
        raise ValueError("must be finite")
    return decimal_value


def _positive_source_decimal(value: object) -> Decimal:
    """Reject zero and negative amounts because movement direction is explicit."""
    decimal_value = _source_decimal(value)
    if decimal_value <= 0:
        raise ValueError("must be greater than zero")
    return decimal_value


class FinancialContract(BaseModel):
    """Base model for source financial facts that cannot be mutated in place."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Money(FinancialContract):
    """A positive monetary amount in its source-reported currency."""

    amount: Decimal
    currency: CurrencyCode

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> Decimal:
        """Preserve source Decimal precision while rejecting binary floats."""
        return _positive_source_decimal(value)


class Quantity(FinancialContract):
    """A positive source-reported instrument quantity, including fractions."""

    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: object) -> Decimal:
        """Preserve source Decimal precision while rejecting binary floats."""
        return _positive_source_decimal(value)


class SourceReportedEurEvidence(FinancialContract):
    """EUR conversion details retained only when the source reported them."""

    eur_amount: Money
    source_rate: Decimal
    reported_at: datetime

    @field_validator("source_rate", mode="before")
    @classmethod
    def validate_source_rate(cls, value: object) -> Decimal:
        """Preserve the reported FX rate without calculating a replacement."""
        return _positive_source_decimal(value)

    @model_validator(mode="after")
    def require_eur_amount(self) -> "SourceReportedEurEvidence":
        """Ensure this optional evidence is explicitly denominated in EUR."""
        if self.eur_amount.currency != "EUR":
            raise ValueError("EUR evidence must use EUR as its currency")
        return self


class SourceIdentity(FinancialContract):
    """Stable source identity used for later idempotent ingestion."""

    provider: Annotated[str, StringConstraints(strict=True, min_length=1)]
    event_reference: Annotated[str, StringConstraints(strict=True, min_length=1)]

    @field_validator("provider", "event_reference")
    @classmethod
    def reject_blank_identity_parts(cls, value: str) -> str:
        """Require meaningful source identity fields without normalizing them."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class MovementDirection(StrEnum):
    """Direction of a factual cash or instrument movement."""

    IN = "in"
    OUT = "out"


class CashLeg(FinancialContract):
    """A single cash movement; signs are represented by its direction."""

    kind: Literal["cash"] = "cash"
    direction: MovementDirection
    money: Money


class InstrumentLeg(FinancialContract):
    """A single security movement; signs are represented by its direction."""

    kind: Literal["instrument"] = "instrument"
    direction: MovementDirection
    instrument_id: Annotated[str, StringConstraints(strict=True, min_length=1)]
    quantity: Quantity

    @field_validator("instrument_id")
    @classmethod
    def reject_blank_instrument_id(cls, value: str) -> str:
        """Require a supplied instrument identity without resolving it externally."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value


EventLeg = Annotated[CashLeg | InstrumentLeg, Field(discriminator="kind")]


class FinancialEventType(StrEnum):
    """The complete initial vocabulary from ADR 0005."""

    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    INTEREST = "interest"
    FEE = "fee"
    WITHHOLDING_TAX = "withholding_tax"
    SOURCE_REPORTED_FX_CONVERSION = "source_reported_fx_conversion"
    STOCK_SPLIT = "stock_split"
    CORRECTION = "correction"
    REVERSAL = "reversal"


class FinancialEvent(FinancialContract):
    """An immutable source event with normalized cash and instrument legs."""

    owner_id: UUID
    account_id: UUID
    source_identity: SourceIdentity
    event_type: FinancialEventType
    occurred_at: datetime
    legs: tuple[EventLeg, ...] = Field(min_length=1)
    source_reported_eur: SourceReportedEurEvidence | None = None
    correction_of_event_id: UUID | None = None
    reversal_of_event_id: UUID | None = None

    @model_validator(mode="after")
    def validate_event_shape(self) -> "FinancialEvent":
        """Validate movement facts without deriving accounting results."""
        self._validate_correction_link()

        if self.event_type in {
            FinancialEventType.DEPOSIT,
            FinancialEventType.DIVIDEND,
            FinancialEventType.INTEREST,
        }:
            self._require_single_cash(MovementDirection.IN)
        elif self.event_type in {
            FinancialEventType.WITHDRAWAL,
            FinancialEventType.FEE,
            FinancialEventType.WITHHOLDING_TAX,
        }:
            self._require_single_cash(MovementDirection.OUT)
        elif self.event_type is FinancialEventType.BUY:
            self._require_trade(
                cash_direction=MovementDirection.OUT,
                instrument_direction=MovementDirection.IN,
            )
        elif self.event_type is FinancialEventType.SELL:
            self._require_trade(
                cash_direction=MovementDirection.IN,
                instrument_direction=MovementDirection.OUT,
            )
        elif self.event_type is FinancialEventType.SOURCE_REPORTED_FX_CONVERSION:
            self._require_fx_conversion()
        elif self.event_type is FinancialEventType.STOCK_SPLIT:
            self._require_stock_split()
        return self

    def _validate_correction_link(self) -> None:
        """Require exactly the matching explicit link for corrections and reversals."""
        if self.event_type is FinancialEventType.CORRECTION:
            if (
                self.correction_of_event_id is None
                or self.reversal_of_event_id is not None
            ):
                raise ValueError("a correction requires only correction_of_event_id")
        elif self.event_type is FinancialEventType.REVERSAL:
            if (
                self.reversal_of_event_id is None
                or self.correction_of_event_id is not None
            ):
                raise ValueError("a reversal requires only reversal_of_event_id")
        elif (
            self.correction_of_event_id is not None
            or self.reversal_of_event_id is not None
        ):
            raise ValueError(
                "only correction and reversal events may link to another event"
            )

    def _require_single_cash(self, direction: MovementDirection) -> None:
        """Require one cash fact with the expected direction."""
        if len(self.legs) != 1 or not isinstance(self.legs[0], CashLeg):
            raise ValueError("event requires exactly one cash leg")
        if self.legs[0].direction is not direction:
            raise ValueError("cash leg has an invalid movement direction")

    def _require_trade(
        self,
        *,
        cash_direction: MovementDirection,
        instrument_direction: MovementDirection,
    ) -> None:
        """Require one cash and one instrument fact for a trade."""
        cash_legs = [leg for leg in self.legs if isinstance(leg, CashLeg)]
        instrument_legs = [leg for leg in self.legs if isinstance(leg, InstrumentLeg)]
        if len(self.legs) != 2 or len(cash_legs) != 1 or len(instrument_legs) != 1:
            raise ValueError("trade requires one cash leg and one instrument leg")
        if (
            cash_legs[0].direction is not cash_direction
            or instrument_legs[0].direction is not instrument_direction
        ):
            raise ValueError("trade legs have invalid movement directions")

    def _require_fx_conversion(self) -> None:
        """Require explicit source cash facts in two distinct currencies."""
        if len(self.legs) != 2 or not all(
            isinstance(leg, CashLeg) for leg in self.legs
        ):
            raise ValueError("FX conversion requires exactly two cash legs")
        cash_legs = tuple(leg for leg in self.legs if isinstance(leg, CashLeg))
        if {leg.direction for leg in cash_legs} != {
            MovementDirection.IN,
            MovementDirection.OUT,
        }:
            raise ValueError("FX conversion requires one inbound and one outbound leg")
        if len({leg.money.currency for leg in cash_legs}) != 2:
            raise ValueError("FX conversion legs must use distinct currencies")

    def _require_stock_split(self) -> None:
        """Require opposing instrument facts without calculating a split ratio."""
        if len(self.legs) != 2 or not all(
            isinstance(leg, InstrumentLeg) for leg in self.legs
        ):
            raise ValueError("stock split requires exactly two instrument legs")
        instrument_legs = tuple(
            leg for leg in self.legs if isinstance(leg, InstrumentLeg)
        )
        if {leg.direction for leg in instrument_legs} != {
            MovementDirection.IN,
            MovementDirection.OUT,
        }:
            raise ValueError("stock split requires one inbound and one outbound leg")
        if len({leg.instrument_id for leg in instrument_legs}) != 1:
            raise ValueError("stock split legs must identify the same instrument")


class FinancialEventBatch(FinancialContract):
    """A validation boundary that rejects duplicate source facts per account."""

    events: tuple[FinancialEvent, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_duplicate_source_identities(self) -> "FinancialEventBatch":
        """Reject same owner/account/provider/reference before persistence exists."""
        identities: set[tuple[UUID, UUID, str, str]] = set()
        for event in self.events:
            identity = (
                event.owner_id,
                event.account_id,
                event.source_identity.provider,
                event.source_identity.event_reference,
            )
            if identity in identities:
                raise ValueError("duplicate source identity for owner and account")
            identities.add(identity)
        return self
