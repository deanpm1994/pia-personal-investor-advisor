"""Pure native-currency valuation for read-only market analysis."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from enum import StrEnum

from pydantic import field_validator, model_validator

from pia_api.domain.market_data import CurrencyCode, MarketDataContract

_OUTPUT_QUANTUM = Decimal("0.000000000001")


class ValuationStatus(StrEnum):
    AVAILABLE = "available"
    NO_POSITION = "no_position"
    MARKET_DATA_UNAVAILABLE = "market_data_unavailable"
    BASIS_UNAVAILABLE = "basis_unavailable"
    CURRENCY_MISMATCH = "currency_mismatch"
    QUANTITY_MISMATCH = "quantity_mismatch"


class PositionLot(MarketDataContract):
    quantity: Decimal
    total_basis: Decimal
    source_currency: CurrencyCode
    evidence_event_ids: tuple[str, ...] = ()

    @field_validator("quantity", "total_basis", mode="before")
    @classmethod
    def validate_decimal(cls, value: object) -> Decimal:
        return _decimal(value)

    @model_validator(mode="after")
    def require_positive_values(self) -> PositionLot:
        if self.quantity <= 0:
            raise ValueError("lot quantity must be greater than zero")
        if self.total_basis <= 0:
            raise ValueError("lot total_basis must be greater than zero")
        return self


class NativeValuation(MarketDataContract):
    status: ValuationStatus
    quote_currency: CurrencyCode
    current_price: Decimal | None = None
    current_value: Decimal | None = None
    total_basis: Decimal | None = None
    unrealized_gain: Decimal | None = None
    unrealized_return_percent: Decimal | None = None
    evidence_event_ids: tuple[str, ...] = ()


def calculate_native_valuation(
    *,
    position_quantity: object | None,
    current_price: object | None,
    quote_currency: str,
    lots: tuple[PositionLot, ...],
) -> NativeValuation:
    """Value one position only when exact native-currency basis reconciles."""
    quantity = _decimal(position_quantity) if position_quantity is not None else None
    price = _decimal(current_price) if current_price is not None else None
    if quantity is None or quantity == 0:
        return _unavailable(ValuationStatus.NO_POSITION, quote_currency)
    if quantity < 0:
        raise ValueError("position_quantity must not be negative")
    if price is None:
        return _unavailable(ValuationStatus.MARKET_DATA_UNAVAILABLE, quote_currency)
    if price <= 0:
        raise ValueError("current_price must be greater than zero")
    if not lots:
        return _unavailable(ValuationStatus.BASIS_UNAVAILABLE, quote_currency)
    if {lot.source_currency for lot in lots} != {quote_currency}:
        return _unavailable(ValuationStatus.CURRENCY_MISMATCH, quote_currency)

    lot_quantity = sum((lot.quantity for lot in lots), Decimal("0"))
    if lot_quantity != quantity:
        return _unavailable(ValuationStatus.QUANTITY_MISMATCH, quote_currency)
    basis = sum((lot.total_basis for lot in lots), Decimal("0"))
    if basis <= 0:
        return _unavailable(ValuationStatus.BASIS_UNAVAILABLE, quote_currency)

    with localcontext() as context:
        context.prec = 50
        raw_current_value = quantity * price
        raw_unrealized_gain = raw_current_value - basis
        raw_unrealized_return = raw_unrealized_gain / basis * Decimal("100")
        current_value = _quantize(raw_current_value)
        total_basis = _quantize(basis)
        unrealized_gain = _quantize(raw_unrealized_gain)
        unrealized_return = _quantize(raw_unrealized_return)
    evidence = tuple(
        sorted({evidence_id for lot in lots for evidence_id in lot.evidence_event_ids})
    )
    return NativeValuation(
        status=ValuationStatus.AVAILABLE,
        quote_currency=quote_currency,
        current_price=_quantize(price),
        current_value=current_value,
        total_basis=total_basis,
        unrealized_gain=unrealized_gain,
        unrealized_return_percent=unrealized_return,
        evidence_event_ids=evidence,
    )


def _unavailable(status: ValuationStatus, currency: str) -> NativeValuation:
    return NativeValuation(status=status, quote_currency=currency)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        raise ValueError("must be a Decimal or decimal string; floats are not accepted")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("must be a valid decimal value") from error
    if not result.is_finite():
        raise ValueError("must be finite")
    return result


def _quantize(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return value.quantize(_OUTPUT_QUANTUM, rounding=ROUND_HALF_EVEN)
