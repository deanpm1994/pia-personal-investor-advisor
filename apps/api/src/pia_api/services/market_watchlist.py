"""Trusted owner-scoped ISIN resolution and market watchlist workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.market_data import (
    ResolutionCandidate,
    ResolutionOutcome,
    ResolutionStatus,
    validate_isin,
)


class InstrumentResolver(Protocol):
    """Replaceable provider adapter boundary defined by ADR 0009."""

    async def resolve_isin(self, isin: str) -> ResolutionOutcome: ...


class MarketWatchlistError(RuntimeError):
    """Raised when persisted identity or watchlist state fails closed."""


@dataclass(frozen=True)
class WatchlistMutation:
    status: str
    action: str
    entry: dict[str, object] | None = None
    candidates: tuple[dict[str, str], ...] = ()


_ACTIONS = {
    "added": "Instrument added to the private watchlist.",
    "duplicate": "Instrument is already on the private watchlist.",
    "invalid": "Correct the ISIN and try again.",
    "unsupported": "This instrument is outside the approved analysis coverage.",
    "ambiguous": "Multiple listings match; no listing was chosen automatically.",
    "temporarily_unavailable": (
        "Try again later; no permanent unsupported result was stored."
    ),
    "provider_disabled": "Instrument resolution is currently disabled.",
}


class DisabledInstrumentResolver:
    """Safe runtime default that performs no network or credential access."""

    async def resolve_isin(self, isin: str) -> ResolutionOutcome:
        from datetime import UTC, datetime

        return ResolutionOutcome(
            requested_isin=isin,
            provider="openfigi",
            status=ResolutionStatus.PROVIDER_DISABLED,
            retrieved_at=datetime.now(UTC),
            source_url="https://www.openfigi.com/api/documentation",
        )


class TrustedMarketWatchlistGateway:
    """Resolve and persist watchlist identities behind the authenticated API."""

    def __init__(
        self,
        settings: Settings,
        resolver: InstrumentResolver | None = None,
    ) -> None:
        self._database_url = settings.database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )
        self._resolver = resolver or DisabledInstrumentResolver()

    async def add(self, user: AuthenticatedUser, isin: str) -> WatchlistMutation:
        try:
            validated_isin = validate_isin(isin)
        except ValueError:
            return WatchlistMutation(status="invalid", action=_ACTIONS["invalid"])

        existing = await self._find_entry_by_isin(user.id, validated_isin)
        if existing is not None:
            return WatchlistMutation(
                status="duplicate",
                action=_ACTIONS["duplicate"],
                entry=existing,
            )

        outcome = await self._resolver.resolve_isin(validated_isin)
        if outcome.requested_isin != validated_isin:
            raise MarketWatchlistError("resolver returned a different ISIN")
        if outcome.status is ResolutionStatus.SUPPORTED:
            entry, duplicate = await self._persist_supported(user.id, outcome)
            status = "duplicate" if duplicate else "added"
            return WatchlistMutation(
                status=status,
                action=_ACTIONS[status],
                entry=entry,
            )
        return WatchlistMutation(
            status=outcome.status.value,
            action=_ACTIONS[outcome.status.value],
            candidates=tuple(
                _candidate_summary(candidate) for candidate in outcome.candidates
            ),
        )

    async def list_entries(self, user: AuthenticatedUser) -> list[dict[str, object]]:
        return await asyncio.to_thread(self._list_entries, user.id)

    async def remove(self, user: AuthenticatedUser, entry_id: str) -> bool:
        try:
            parsed_id = UUID(entry_id)
            owner_id = UUID(user.id)
        except (TypeError, ValueError):
            return False
        return await asyncio.to_thread(self._remove, owner_id, parsed_id)

    async def list_portfolio_candidates(
        self, user: AuthenticatedUser
    ) -> list[dict[str, object]]:
        return await asyncio.to_thread(self._list_portfolio_candidates, user.id)

    async def _find_entry_by_isin(
        self, user_id: str, isin: str
    ) -> dict[str, object] | None:
        return await asyncio.to_thread(self._find_entry_by_isin_sync, user_id, isin)

    async def _persist_supported(
        self, user_id: str, outcome: ResolutionOutcome
    ) -> tuple[dict[str, object], bool]:
        return await asyncio.to_thread(self._persist_supported_sync, user_id, outcome)

    def _find_entry_by_isin_sync(
        self, user_id: str, isin: str
    ) -> dict[str, object] | None:
        owner_id = _owner_uuid(user_id)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            return connection.execute(
                _ENTRY_SELECT + " WHERE w.user_id = %s AND i.isin = %s",
                (owner_id, isin),
            ).fetchone()

    def _list_entries(self, user_id: str) -> list[dict[str, object]]:
        owner_id = _owner_uuid(user_id)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            return connection.execute(
                _ENTRY_SELECT + " WHERE w.user_id = %s ORDER BY w.added_at, w.id",
                (owner_id,),
            ).fetchall()

    def _persist_supported_sync(
        self, user_id: str, outcome: ResolutionOutcome
    ) -> tuple[dict[str, object], bool]:
        owner_id = _owner_uuid(user_id)
        candidate = outcome.candidates[0]
        if candidate.instrument.isin != outcome.requested_isin:
            raise MarketWatchlistError("candidate identity contradicts requested ISIN")
        try:
            with psycopg.connect(
                self._database_url, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT id FROM public.profiles WHERE id = %s FOR UPDATE",
                        (owner_id,),
                    )
                    instrument_id = self._upsert_instrument(
                        connection, owner_id, outcome, candidate
                    )
                    self._require_or_insert_mapping(
                        connection, owner_id, instrument_id, candidate
                    )
                    inserted = connection.execute(
                        """
                        INSERT INTO public.market_watchlist_entries (
                            user_id, instrument_id
                        ) VALUES (%s, %s)
                        ON CONFLICT (user_id, instrument_id) DO NOTHING
                        RETURNING id
                        """,
                        (owner_id, instrument_id),
                    ).fetchone()
                    entry = connection.execute(
                        _ENTRY_SELECT
                        + " WHERE w.user_id = %s AND w.instrument_id = %s",
                        (owner_id, instrument_id),
                    ).fetchone()
                    if entry is None:
                        raise MarketWatchlistError("watchlist insert could not be read")
                    return entry, inserted is None
        except MarketWatchlistError:
            raise
        except psycopg.Error as error:
            raise MarketWatchlistError("watchlist persistence failed") from error

    @staticmethod
    def _upsert_instrument(
        connection: psycopg.Connection[dict[str, object]],
        owner_id: UUID,
        outcome: ResolutionOutcome,
        candidate: ResolutionCandidate,
    ) -> UUID:
        existing = connection.execute(
            """
            SELECT id, instrument_kind, display_name
            FROM public.market_instruments
            WHERE user_id = %s AND isin = %s
                AND COALESCE(share_class_figi, '') = COALESCE(%s, '')
            FOR UPDATE
            """,
            (
                owner_id,
                candidate.instrument.isin,
                candidate.instrument.share_class_figi,
            ),
        ).fetchone()
        if existing is not None:
            if (
                existing["instrument_kind"]
                != candidate.instrument.instrument_kind.value
            ):
                raise MarketWatchlistError("resolved instrument kind changed")
            return existing["id"]
        inserted = connection.execute(
            """
            INSERT INTO public.market_instruments (
                user_id, isin, share_class_figi, instrument_kind, display_name,
                resolution_status, resolution_source_url, resolved_at
            ) VALUES (%s, %s, %s, %s, %s, 'supported', %s, %s)
            RETURNING id
            """,
            (
                owner_id,
                candidate.instrument.isin,
                candidate.instrument.share_class_figi,
                candidate.instrument.instrument_kind.value,
                candidate.display_name,
                outcome.source_url,
                outcome.retrieved_at,
            ),
        ).fetchone()
        assert inserted is not None
        return inserted["id"]

    @staticmethod
    def _require_or_insert_mapping(
        connection: psycopg.Connection[dict[str, object]],
        owner_id: UUID,
        instrument_id: UUID,
        candidate: ResolutionCandidate,
    ) -> None:
        mapping = candidate.mapping
        existing = connection.execute(
            """
            SELECT id, provider_symbol, provider_exchange_code, mic,
                   quote_currency, mapping_version, valid_from
            FROM public.market_provider_identifiers
            WHERE user_id = %s AND instrument_id = %s AND provider = %s
                AND valid_to IS NULL
            FOR UPDATE
            """,
            (owner_id, instrument_id, mapping.provider),
        ).fetchone()
        expected = {
            "provider_symbol": mapping.provider_symbol,
            "provider_exchange_code": mapping.provider_exchange_code,
            "mic": mapping.mic,
            "quote_currency": mapping.quote_currency,
            "mapping_version": mapping.mapping_version,
        }
        if existing is not None:
            if not any(existing[key] != value for key, value in expected.items()):
                return
            if (
                mapping.mapping_version <= existing["mapping_version"]
                or mapping.valid_from <= existing["valid_from"]
            ):
                raise MarketWatchlistError(
                    "replacement provider mapping is not a later version"
                )
            connection.execute(
                """
                UPDATE public.market_provider_identifiers
                SET valid_to = %s
                WHERE id = %s AND user_id = %s
                """,
                (mapping.valid_from, existing["id"], owner_id),
            )
        connection.execute(
            """
            INSERT INTO public.market_provider_identifiers (
                user_id, instrument_id, provider, provider_symbol,
                provider_exchange_code, mic, quote_currency, mapping_version,
                valid_from, valid_to, resolved_at, resolution_source_url,
                resolution_status
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'supported'
            )
            """,
            (
                owner_id,
                instrument_id,
                mapping.provider,
                mapping.provider_symbol,
                mapping.provider_exchange_code,
                mapping.mic,
                mapping.quote_currency,
                mapping.mapping_version,
                mapping.valid_from,
                mapping.valid_to,
                mapping.resolved_at,
                mapping.resolution_source_url,
            ),
        )

    def _remove(self, owner_id: UUID, entry_id: UUID) -> bool:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                return (
                    connection.execute(
                        """
                    DELETE FROM public.market_watchlist_entries
                    WHERE id = %s AND user_id = %s RETURNING id
                    """,
                        (entry_id, owner_id),
                    ).fetchone()
                    is not None
                )

    def _list_portfolio_candidates(self, user_id: str) -> list[dict[str, object]]:
        owner_id = _owner_uuid(user_id)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            snapshot = connection.execute(
                """
                SELECT id::text, as_of, refreshed_at, content
                FROM public.financial_snapshots
                WHERE user_id = %s
                ORDER BY refreshed_at DESC, id DESC LIMIT 1
                """,
                (owner_id,),
            ).fetchone()
            if snapshot is None:
                return []
            positions = snapshot["content"].get("positions", {}).get("owner", [])
            evidence_ids = sorted(
                {
                    event_id
                    for position in positions
                    for event_id in position.get("evidence_event_ids", [])
                }
            )
            event_types: dict[str, str] = {}
            if evidence_ids:
                rows = connection.execute(
                    """
                    SELECT id::text, event_type FROM public.financial_events
                    WHERE user_id = %s AND id = ANY(%s::uuid[])
                    """,
                    (owner_id, evidence_ids),
                ).fetchall()
                event_types = {row["id"]: row["event_type"] for row in rows}
            result = []
            for position in positions:
                source_id = position["instrument_id"]
                instrument = self._find_analyzable_instrument(
                    connection, owner_id, source_id
                )
                if instrument is not None:
                    coverage = "supported"
                    action = "Open market analysis for this exact resolved identity."
                else:
                    try:
                        validate_isin(source_id)
                        coverage = "unresolved"
                        action = "Resolve this exact ISIN before analysis."
                    except ValueError:
                        coverage = "unsupported_source_identity"
                        action = "Supply a validated ISIN; PIA will not infer one."
                evidence = list(position.get("evidence_event_ids", []))
                result.append(
                    {
                        "source_instrument_id": source_id,
                        "source_kind": _source_kind(evidence, event_types),
                        "quantity": position["quantity"],
                        "evidence_event_ids": evidence,
                        "snapshot_id": snapshot["id"],
                        "snapshot_as_of": snapshot["as_of"],
                        "snapshot_refreshed_at": snapshot["refreshed_at"],
                        "coverage_status": coverage,
                        "instrument": instrument,
                        "action": action,
                    }
                )
            return result

    @staticmethod
    def _find_analyzable_instrument(
        connection: psycopg.Connection[dict[str, object]],
        owner_id: UUID,
        source_id: str,
    ) -> dict[str, object] | None:
        return connection.execute(
            """
            SELECT i.id::text AS instrument_id, i.isin, i.share_class_figi,
                   i.instrument_kind, i.display_name, m.mic, m.quote_currency,
                   m.provider, m.provider_symbol
            FROM public.market_instruments AS i
            JOIN LATERAL (
                SELECT mic, quote_currency, provider, provider_symbol
                FROM public.market_provider_identifiers
                WHERE user_id = i.user_id AND instrument_id = i.id
                    AND valid_to IS NULL AND resolution_status = 'supported'
                ORDER BY resolved_at DESC, id DESC LIMIT 1
            ) AS m ON true
            WHERE i.user_id = %s AND i.isin = %s
                AND i.resolution_status = 'supported'
            ORDER BY i.resolved_at DESC, i.id DESC LIMIT 1
            """,
            (owner_id, source_id),
        ).fetchone()


def _owner_uuid(user_id: str) -> UUID:
    try:
        return UUID(user_id)
    except (TypeError, ValueError) as error:
        raise MarketWatchlistError("authenticated user id must be a UUID") from error


def _candidate_summary(candidate: ResolutionCandidate) -> dict[str, str]:
    return {
        "mic": candidate.listing.mic,
        "quote_currency": candidate.listing.quote_currency,
        "provider": candidate.mapping.provider,
        "provider_symbol": candidate.mapping.provider_symbol,
    }


def _source_kind(evidence_ids: list[str], event_types: dict[str, str]) -> str:
    observed = any(
        event_types.get(event_id) == "observed_position_movement"
        for event_id in evidence_ids
    )
    confirmed = any(
        event_types.get(event_id) not in {None, "observed_position_movement"}
        for event_id in evidence_ids
    )
    if observed and confirmed:
        return "mixed"
    return "observed" if observed else "confirmed"


_ENTRY_SELECT = """
SELECT w.id::text, w.instrument_id::text, i.isin, i.share_class_figi,
       i.instrument_kind, i.display_name, m.mic, m.quote_currency,
       m.provider, m.provider_symbol, w.added_at
FROM public.market_watchlist_entries AS w
JOIN public.market_instruments AS i
  ON i.id = w.instrument_id AND i.user_id = w.user_id
JOIN LATERAL (
    SELECT mic, quote_currency, provider, provider_symbol
    FROM public.market_provider_identifiers
    WHERE user_id = w.user_id AND instrument_id = w.instrument_id
        AND valid_to IS NULL AND resolution_status = 'supported'
    ORDER BY resolved_at DESC, id DESC LIMIT 1
) AS m ON true
"""
