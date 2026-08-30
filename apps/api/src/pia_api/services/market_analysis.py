"""Read-only owner market-analysis gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.market_analysis import PositionLot, calculate_native_valuation
from pia_api.domain.market_data import CompletenessStatus, DailyBar, FreshnessStatus
from pia_api.domain.technical_indicators import calculate_technical_indicators


class MarketAnalysisError(RuntimeError):
    """Raised when persisted analysis cannot be returned safely."""


class ProviderAccessStatus(StrEnum):
    ENABLED = "enabled"
    PROVIDER_DISABLED = "provider_disabled"
    LICENSE_REVIEW_REQUIRED = "license_review_required"


@dataclass(frozen=True)
class AnalysisInstrument:
    instrument_id: UUID
    isin: str
    share_class_figi: str | None
    instrument_kind: str
    display_name: str
    mic: str
    quote_currency: str
    provider: str
    provider_symbol: str
    mapping_version: int
    watched: bool
    provider_identifier_id: UUID | None = None


@dataclass(frozen=True)
class AnalysisPosition:
    quantity: object
    evidence_event_ids: tuple[str, ...]
    lots: tuple[PositionLot, ...]
    snapshot_id: str
    snapshot_as_of: datetime | None
    snapshot_refreshed_at: datetime
    snapshot_input_fingerprint: str


class TrustedMarketAnalysisGateway:
    """Read persisted analysis without provider, snapshot, or ledger writes."""

    def __init__(self, settings: Settings) -> None:
        self._database_url = settings.database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )

    async def list_analysis(self, user: AuthenticatedUser) -> list[dict[str, object]]:
        return await asyncio.to_thread(self._list_analysis, user.id)

    def _list_analysis(self, user_id: str) -> list[dict[str, object]]:
        try:
            owner_id = UUID(user_id)
        except (TypeError, ValueError) as error:
            raise MarketAnalysisError("authenticated user id must be a UUID") from error
        try:
            with psycopg.connect(
                self._database_url, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    snapshot = _latest_snapshot(connection, owner_id)
                    positions = _snapshot_positions(snapshot)
                    instruments = _eligible_instruments(
                        connection, owner_id, tuple(positions)
                    )
                    result: list[dict[str, object]] = []
                    matched_position_ids: set[str] = set()
                    now = datetime.now(UTC)
                    for row in instruments:
                        instrument = _instrument(row)
                        position = positions.get(instrument.isin)
                        if position is not None:
                            matched_position_ids.add(instrument.isin)
                        access = _provider_access(row, now)
                        bars = (
                            _bars(connection, owner_id, instrument)
                            if access is ProviderAccessStatus.ENABLED
                            else ()
                        )
                        result.append(
                            build_analysis_item(
                                instrument,
                                position=position,
                                access_status=access,
                                bars=bars,
                                target_date=_target_market_date(now.date()),
                            )
                        )
                    for source_id in sorted(set(positions) - matched_position_ids):
                        result.append(
                            _unsupported_item(source_id, positions[source_id])
                        )
                    return sorted(
                        result,
                        key=lambda item: (
                            str(item["source_instrument_id"]),
                            str(item["source_kind"]),
                        ),
                    )
        except MarketAnalysisError:
            raise
        except (KeyError, TypeError, ValueError, psycopg.Error) as error:
            raise MarketAnalysisError("persisted market analysis is invalid") from error


def build_analysis_item(
    instrument: AnalysisInstrument,
    *,
    position: AnalysisPosition | None,
    access_status: ProviderAccessStatus,
    bars: tuple[DailyBar, ...],
    target_date: date,
) -> dict[str, object]:
    """Compose one API item from validated persisted facts only."""
    source_kind = (
        "portfolio_and_watchlist"
        if position is not None and instrument.watched
        else "portfolio"
        if position is not None
        else "watchlist"
    )
    base = {
        "source_kind": source_kind,
        "source_instrument_id": instrument.isin,
        "instrument": _instrument_data(instrument),
        "position": _position_data(position),
    }
    if access_status is not ProviderAccessStatus.ENABLED:
        code = (
            "MARKET_LICENSE_REVIEW_REQUIRED"
            if access_status is ProviderAccessStatus.LICENSE_REVIEW_REQUIRED
            else "MARKET_PROVIDER_DISABLED"
        )
        return {
            **base,
            "state": access_status.value,
            "bars": [],
            "indicators": [],
            "source": None,
            "freshness": {"status": "unavailable"},
            "completeness": {"status": "unavailable"},
            "valuation": None,
            "diagnostics": [{"code": code, "evidence_event_ids": []}],
        }
    if not bars:
        return {
            **base,
            "state": "unavailable",
            "bars": [],
            "indicators": [],
            "source": None,
            "freshness": {"status": "unavailable"},
            "completeness": {"status": "unavailable"},
            "valuation": None,
            "diagnostics": [
                {"code": "MARKET_ANALYSIS_NO_BARS", "evidence_event_ids": []}
            ],
        }

    analysis = calculate_technical_indicators(bars, target_date=target_date)
    if not analysis.results:
        return {
            **base,
            "state": "unavailable",
            "bars": [],
            "indicators": [],
            "source": None,
            "freshness": {"status": analysis.freshness_status.value},
            "completeness": {"status": analysis.completeness_status.value},
            "valuation": None,
            "diagnostics": [
                {
                    "code": diagnostic.code.value,
                    "market_date": diagnostic.market_date,
                    "evidence_event_ids": [],
                }
                for diagnostic in analysis.diagnostics
            ],
        }

    selected_bars = _latest_revisions(bars)
    if analysis.freshness_status is FreshnessStatus.STALE:
        state = "stale"
    elif analysis.completeness_status is CompletenessStatus.INCOMPLETE:
        state = "incomplete"
    else:
        state = "ready"
    latest = selected_bars[-1]
    valuation = (
        calculate_native_valuation(
            position_quantity=position.quantity,
            current_price=latest.close,
            quote_currency=instrument.quote_currency,
            lots=position.lots,
        )
        if position is not None
        else None
    )
    diagnostics = _analysis_diagnostics(analysis)
    if valuation is not None and valuation.status.value != "available":
        diagnostics.append(
            {
                "code": f"MARKET_VALUATION_{valuation.status.value.upper()}",
                "evidence_event_ids": list(position.evidence_event_ids),
            }
        )
    return {
        **base,
        "state": state,
        "bars": [_bar_data(bar) for bar in selected_bars],
        "indicators": [
            {
                "code": result.code.value,
                "market_date": result.market_date,
                "value": _decimal_string(result.value),
                "status": result.status.value,
                "observation_count": result.observation_count,
                "required_observations": result.required_observations,
                "window_start": result.window_start,
                "window_end": result.window_end,
                "provider_as_of": result.provider_as_of,
                "retrieved_at": result.retrieved_at,
                "source_urls": list(result.source_urls),
                "freshness_status": result.freshness_status.value,
                "completeness_status": result.completeness_status.value,
                "corrected": result.corrected,
                "diagnostics": [
                    diagnostic.model_dump(mode="json")
                    for diagnostic in result.diagnostics
                ],
            }
            for result in analysis.results
        ],
        "source": {
            "provider": instrument.provider,
            "provider_symbol": instrument.provider_symbol,
            "mic": instrument.mic,
            "quote_currency": instrument.quote_currency,
            "attribution": _attribution(instrument.provider),
            "source_urls": sorted({bar.source_url for bar in selected_bars}),
            "provider_as_of": max(bar.provider_as_of for bar in selected_bars),
            "retrieved_at": max(bar.retrieved_at for bar in selected_bars),
        },
        "freshness": {"status": analysis.freshness_status.value},
        "completeness": {"status": analysis.completeness_status.value},
        "valuation": (
            valuation.model_dump(mode="json") if valuation is not None else None
        ),
        "diagnostics": diagnostics,
    }


def _latest_snapshot(connection, owner_id: UUID) -> dict[str, object] | None:
    return connection.execute(
        """
        SELECT id::text, as_of, refreshed_at, input_fingerprint, content
        FROM public.financial_snapshots
        WHERE user_id = %s
        ORDER BY refreshed_at DESC, id DESC LIMIT 1
        """,
        (owner_id,),
    ).fetchone()


def _snapshot_positions(
    snapshot: dict[str, object] | None,
) -> dict[str, AnalysisPosition]:
    if snapshot is None:
        return {}
    content = snapshot["content"]
    if not isinstance(content, dict):
        raise MarketAnalysisError("snapshot content must be an object")
    positions_container = content.get("positions", {})
    fifo_container = content.get("fifo", {})
    if not isinstance(positions_container, dict) or not isinstance(
        fifo_container, dict
    ):
        raise MarketAnalysisError("snapshot analysis content is invalid")
    positions = positions_container.get("owner", [])
    open_lots = fifo_container.get("open_lots", [])
    if not isinstance(positions, list) or not isinstance(open_lots, list):
        raise MarketAnalysisError("snapshot positions or lots are invalid")
    result: dict[str, AnalysisPosition] = {}
    for position in positions:
        if not isinstance(position, dict):
            raise MarketAnalysisError("snapshot position is invalid")
        source_id = position["instrument_id"]
        if not isinstance(source_id, str) or source_id in result:
            raise MarketAnalysisError("snapshot position identity is invalid")
        evidence = _string_tuple(position.get("evidence_event_ids", []))
        lots = tuple(
            PositionLot(
                quantity=lot["quantity"],
                total_basis=lot["total_basis"],
                source_currency=lot["source_currency"],
                evidence_event_ids=_string_tuple(lot.get("evidence_event_ids", [])),
            )
            for lot in open_lots
            if isinstance(lot, dict) and lot.get("instrument_id") == source_id
        )
        result[source_id] = AnalysisPosition(
            quantity=position["quantity"],
            evidence_event_ids=evidence,
            lots=lots,
            snapshot_id=snapshot["id"],
            snapshot_as_of=snapshot["as_of"],
            snapshot_refreshed_at=snapshot["refreshed_at"],
            snapshot_input_fingerprint=snapshot["input_fingerprint"],
        )
    return result


def _eligible_instruments(
    connection, owner_id: UUID, position_ids: tuple[str, ...]
) -> list[dict[str, object]]:
    return connection.execute(
        """
        SELECT i.id AS instrument_id, i.isin, i.share_class_figi,
               i.instrument_kind, i.display_name, m.id AS provider_identifier_id,
               m.mic, m.quote_currency, m.provider, m.provider_symbol,
               m.mapping_version,
               EXISTS (
                   SELECT 1 FROM public.market_watchlist_entries AS w
                   WHERE w.user_id = i.user_id AND w.instrument_id = i.id
               ) AS watched,
               access.access_status, access.license_review_due_at,
               access.risk_attestation_version, access.risk_attested_at,
               access.risk_withdrawn_at
        FROM public.market_instruments AS i
        JOIN LATERAL (
            SELECT id, mic, quote_currency, provider, provider_symbol,
                   mapping_version
            FROM public.market_provider_identifiers
            WHERE user_id = i.user_id AND instrument_id = i.id
                AND valid_to IS NULL AND resolution_status = 'supported'
            ORDER BY resolved_at DESC, id DESC LIMIT 1
        ) AS m ON true
        LEFT JOIN public.market_provider_access AS access
          ON access.user_id = i.user_id AND access.provider = m.provider
        WHERE i.user_id = %s AND i.resolution_status = 'supported'
          AND (
              i.isin = ANY(%s::text[])
              OR EXISTS (
                  SELECT 1 FROM public.market_watchlist_entries AS w
                  WHERE w.user_id = i.user_id AND w.instrument_id = i.id
              )
          )
        ORDER BY i.isin, i.resolved_at DESC, i.id DESC
        """,
        (owner_id, list(position_ids)),
    ).fetchall()


def _bars(
    connection, owner_id: UUID, instrument: AnalysisInstrument
) -> tuple[DailyBar, ...]:
    if instrument.provider_identifier_id is None:
        raise MarketAnalysisError("active provider mapping identity is unavailable")
    rows = connection.execute(
        """
        SELECT instrument_id, market_date, open, high, low, close, volume,
               provider, provider_symbol, mic, quote_currency, provider_as_of,
               retrieved_at, ingestion_run_id, source_url, mapping_version,
               completeness_status, revision, response_sha256
        FROM public.market_eod_bars
        WHERE user_id = %s AND instrument_id = %s
          AND provider_identifier_id = %s AND retain_until >= current_date
        ORDER BY market_date, revision
        """,
        (owner_id, instrument.instrument_id, instrument.provider_identifier_id),
    ).fetchall()
    return tuple(
        DailyBar(
            listing_id=row["instrument_id"],
            market_date=row["market_date"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            provider=row["provider"],
            provider_symbol=row["provider_symbol"],
            mic=row["mic"],
            quote_currency=row["quote_currency"],
            provider_as_of=row["provider_as_of"],
            retrieved_at=row["retrieved_at"],
            ingestion_run_id=row["ingestion_run_id"],
            source_url=row["source_url"],
            mapping_version=row["mapping_version"],
            completeness_status=row["completeness_status"],
            revision=row["revision"],
            response_sha256=row["response_sha256"],
        )
        for row in rows
    )


def _instrument(row: dict[str, object]) -> AnalysisInstrument:
    return AnalysisInstrument(
        instrument_id=row["instrument_id"],
        isin=row["isin"],
        share_class_figi=row["share_class_figi"],
        instrument_kind=row["instrument_kind"],
        display_name=row["display_name"],
        mic=row["mic"],
        quote_currency=row["quote_currency"],
        provider=row["provider"],
        provider_symbol=row["provider_symbol"],
        mapping_version=row["mapping_version"],
        watched=row["watched"],
        provider_identifier_id=row["provider_identifier_id"],
    )


def _provider_access(row: dict[str, object], now: datetime) -> ProviderAccessStatus:
    raw_status = row.get("access_status")
    if raw_status == ProviderAccessStatus.LICENSE_REVIEW_REQUIRED.value:
        return ProviderAccessStatus.LICENSE_REVIEW_REQUIRED
    if raw_status != ProviderAccessStatus.ENABLED.value:
        return ProviderAccessStatus.PROVIDER_DISABLED
    review_due = row.get("license_review_due_at")
    if not isinstance(review_due, datetime) or review_due <= now:
        return ProviderAccessStatus.LICENSE_REVIEW_REQUIRED
    if row["provider"] == "marketstack" and (
        row.get("risk_attestation_version") != "adr-0009-founder-risk-v1"
        or not isinstance(row.get("risk_attested_at"), datetime)
        or row["risk_attested_at"] > now
        or row.get("risk_withdrawn_at") is not None
    ):
        return ProviderAccessStatus.PROVIDER_DISABLED
    return ProviderAccessStatus.ENABLED


def _latest_revisions(bars: tuple[DailyBar, ...]) -> tuple[DailyBar, ...]:
    selected: dict[date, DailyBar] = {}
    for bar in bars:
        current = selected.get(bar.market_date)
        if current is None or bar.revision > current.revision:
            selected[bar.market_date] = bar
    return tuple(selected[market_date] for market_date in sorted(selected))


def _analysis_diagnostics(analysis) -> list[dict[str, object]]:
    diagnostics = {
        (diagnostic.code.value, diagnostic.market_date)
        for result in analysis.results
        for diagnostic in result.diagnostics
    }
    return [
        {
            "code": code,
            **({"market_date": market_date} if market_date is not None else {}),
            "evidence_event_ids": [],
        }
        for code, market_date in sorted(
            diagnostics, key=lambda item: (item[0], item[1] or date.min)
        )
    ]


def _instrument_data(instrument: AnalysisInstrument) -> dict[str, object]:
    return {
        "instrument_id": str(instrument.instrument_id),
        "isin": instrument.isin,
        "share_class_figi": instrument.share_class_figi,
        "instrument_kind": instrument.instrument_kind,
        "display_name": instrument.display_name,
        "mic": instrument.mic,
        "quote_currency": instrument.quote_currency,
        "provider": instrument.provider,
        "provider_symbol": instrument.provider_symbol,
    }


def _position_data(position: AnalysisPosition | None) -> dict[str, object] | None:
    if position is None:
        return None
    return {
        "quantity": _decimal_string(position.quantity),
        "evidence_event_ids": list(position.evidence_event_ids),
        "snapshot_id": position.snapshot_id,
        "snapshot_as_of": position.snapshot_as_of,
        "snapshot_refreshed_at": position.snapshot_refreshed_at,
        "snapshot_input_fingerprint": position.snapshot_input_fingerprint,
    }


def _bar_data(bar: DailyBar) -> dict[str, object]:
    return {
        "market_date": bar.market_date,
        "open": _decimal_string(bar.open),
        "high": _decimal_string(bar.high),
        "low": _decimal_string(bar.low),
        "close": _decimal_string(bar.close),
        "volume": bar.volume,
        "revision": bar.revision,
        "provider_as_of": bar.provider_as_of,
        "retrieved_at": bar.retrieved_at,
        "source_url": bar.source_url,
        "completeness_status": bar.completeness_status.value,
        "corrected": bar.revision > 1,
    }


def _unsupported_item(source_id: str, position: AnalysisPosition) -> dict[str, object]:
    return {
        "source_kind": "portfolio",
        "source_instrument_id": source_id,
        "state": "unsupported",
        "instrument": None,
        "bars": [],
        "indicators": [],
        "source": None,
        "freshness": {"status": "unavailable"},
        "completeness": {"status": "unavailable"},
        "position": _position_data(position),
        "valuation": None,
        "diagnostics": [
            {
                "code": "MARKET_INSTRUMENT_UNSUPPORTED",
                "evidence_event_ids": list(position.evidence_event_ids),
            }
        ],
    }


def _decimal_string(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        raise MarketAnalysisError("financial values must be Decimal strings")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise MarketAnalysisError("financial values must be decimal strings") from error
    if not decimal.is_finite():
        raise MarketAnalysisError("financial values must be finite")
    return format(decimal, "f")


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MarketAnalysisError("evidence IDs must be strings")
    return tuple(value)


def _attribution(provider: str) -> str:
    display = "Marketstack" if provider == "marketstack" else provider
    return f"Market data: {display}"


def _target_market_date(current_date: date) -> date:
    target = current_date - timedelta(days=1)
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target
