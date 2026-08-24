"""Trusted persistence for validated private daily market observations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.market_data import (
    CompletenessStatus,
    DailyBar,
    FetchAssessment,
    FetchOutcome,
    ProviderMapping,
)


class MarketDataPersistenceError(RuntimeError):
    """Raised when supplied provenance contradicts persisted market identity."""


class MarketDataAccessError(RuntimeError):
    """Raised when provider Content cannot currently be persisted or used."""


@dataclass(frozen=True)
class PersistedFetch:
    ingestion_run_id: str
    bar_ids: tuple[str, ...]
    inserted_revisions: int
    reused_bars: int


class TrustedMarketDataGateway:
    """Write assessed observations through the server-only database boundary."""

    def __init__(self, settings: Settings) -> None:
        self._database_url = settings.database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )

    async def persist_fetch(
        self,
        user: AuthenticatedUser,
        mapping_id: UUID,
        mapping: ProviderMapping,
        outcome: FetchOutcome,
        assessment: FetchAssessment,
        *,
        retention_days: int = 400,
    ) -> PersistedFetch:
        if not 1 <= retention_days <= 400:
            raise ValueError("retention_days must be between 1 and 400")
        if any(bar not in outcome.bars for bar in assessment.accepted_bars):
            raise MarketDataPersistenceError(
                "fetch assessment contains an observation outside its outcome"
            )
        if any(
            bar.market_date + timedelta(days=retention_days) < datetime.now(UTC).date()
            for bar in assessment.accepted_bars
        ):
            raise MarketDataAccessError("bar retention deadline has already passed")
        return await asyncio.to_thread(
            self._persist_fetch,
            user.id,
            mapping_id,
            mapping,
            outcome,
            assessment,
            retention_days,
        )

    def _persist_fetch(
        self,
        user_id: str,
        mapping_id: UUID,
        mapping: ProviderMapping,
        outcome: FetchOutcome,
        assessment: FetchAssessment,
        retention_days: int,
    ) -> PersistedFetch:
        fingerprint = _fetch_fingerprint(outcome, assessment)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                self._require_mapping(connection, user_id, mapping_id, mapping, outcome)
                if assessment.accepted_bars:
                    self._require_provider_access(connection, user_id, outcome.provider)
                self._record_run(connection, user_id, outcome, assessment, fingerprint)
                bar_ids: list[str] = []
                inserted = 0
                reused = 0
                for bar in assessment.accepted_bars:
                    bar_id, was_inserted = self._persist_bar(
                        connection,
                        user_id,
                        mapping_id,
                        bar,
                        retention_days,
                    )
                    self._record_observation(connection, user_id, bar_id, outcome)
                    bar_ids.append(str(bar_id))
                    inserted += int(was_inserted)
                    reused += int(not was_inserted)
        return PersistedFetch(
            ingestion_run_id=str(outcome.ingestion_run_id),
            bar_ids=tuple(bar_ids),
            inserted_revisions=inserted,
            reused_bars=reused,
        )

    @staticmethod
    def _require_mapping(
        connection: psycopg.Connection[dict[str, object]],
        user_id: str,
        mapping_id: UUID,
        mapping: ProviderMapping,
        outcome: FetchOutcome,
    ) -> None:
        row = connection.execute(
            """
            SELECT instrument_id, provider, provider_symbol, mic, quote_currency,
                   mapping_version, resolution_status, valid_from, valid_to
            FROM public.market_provider_identifiers
            WHERE id = %s AND user_id = %s
            """,
            (mapping_id, user_id),
        ).fetchone()
        if row is None:
            raise MarketDataPersistenceError("provider mapping was not found")
        expected = {
            "instrument_id": mapping.instrument_id,
            "provider": mapping.provider,
            "provider_symbol": mapping.provider_symbol,
            "mic": mapping.mic,
            "quote_currency": mapping.quote_currency,
            "mapping_version": mapping.mapping_version,
        }
        if any(row[key] != value for key, value in expected.items()):
            raise MarketDataPersistenceError(
                "provider mapping contradicts persisted identity"
            )
        if row["resolution_status"] != "supported" or row["valid_to"] is not None:
            raise MarketDataPersistenceError("provider mapping is not active")
        if (
            outcome.provider != mapping.provider
            or outcome.provider_symbol != mapping.provider_symbol
            or outcome.mic != mapping.mic
            or outcome.quote_currency != mapping.quote_currency
        ):
            raise MarketDataPersistenceError(
                "fetch outcome contradicts its provider mapping"
            )

    @staticmethod
    def _require_provider_access(
        connection: psycopg.Connection[dict[str, object]],
        user_id: str,
        provider: str,
    ) -> None:
        allowed = connection.execute(
            """
            SELECT 1
            FROM public.market_provider_access
            WHERE user_id = %s AND provider = %s
                AND access_status = 'enabled'
                AND license_review_due_at > now()
            """,
            (user_id, provider),
        ).fetchone()
        if allowed is None:
            raise MarketDataAccessError(
                "provider access is disabled or requires license review"
            )

    @staticmethod
    def _record_run(
        connection: psycopg.Connection[dict[str, object]],
        user_id: str,
        outcome: FetchOutcome,
        assessment: FetchAssessment,
        fingerprint: str,
    ) -> None:
        status = (
            "failed"
            if not assessment.accepted_bars
            else "partial"
            if assessment.completeness_status is CompletenessStatus.INCOMPLETE
            else "completed"
        )
        inserted = connection.execute(
            """
            INSERT INTO public.market_ingestion_runs (
                id, user_id, provider, status, requested_start, requested_end,
                provider_as_of, retrieved_at, source_url, input_fingerprint,
                request_parameters, response_sha256, completeness_status,
                diagnostics, quota_state, started_at, finished_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            (
                outcome.ingestion_run_id,
                user_id,
                outcome.provider,
                status,
                outcome.requested_start,
                outcome.requested_end,
                outcome.provider_as_of,
                outcome.retrieved_at,
                outcome.source_url,
                fingerprint,
                Jsonb(outcome.request_parameters),
                outcome.response_sha256,
                assessment.completeness_status.value,
                Jsonb(
                    [item.model_dump(mode="json") for item in assessment.diagnostics]
                ),
                Jsonb(outcome.quota_state.model_dump(mode="json")),
                outcome.started_at,
                outcome.retrieved_at,
            ),
        ).fetchone()
        if inserted is not None:
            return
        existing = connection.execute(
            """
            SELECT user_id::text, input_fingerprint
            FROM public.market_ingestion_runs WHERE id = %s
            """,
            (outcome.ingestion_run_id,),
        ).fetchone()
        if existing is None or existing["user_id"] != user_id:
            raise MarketDataPersistenceError("ingestion run identity is unavailable")
        if existing["input_fingerprint"] != fingerprint:
            raise MarketDataPersistenceError(
                "ingestion run identity was reused for different content"
            )

    @staticmethod
    def _persist_bar(
        connection: psycopg.Connection[dict[str, object]],
        user_id: str,
        mapping_id: UUID,
        bar: DailyBar,
        retention_days: int,
    ) -> tuple[UUID, bool]:
        latest = connection.execute(
            """
            SELECT id, provider_identifier_id, instrument_id, mic, quote_currency,
                   mapping_version, open, high, low, close, volume, revision
            FROM public.market_eod_bars
            WHERE user_id = %s AND provider = %s AND provider_symbol = %s
                AND market_date = %s
            ORDER BY revision DESC
            LIMIT 1
            FOR UPDATE
            """,
            (user_id, bar.provider, bar.provider_symbol, bar.market_date),
        ).fetchone()
        if latest is not None:
            identity = (
                latest["provider_identifier_id"],
                latest["instrument_id"],
                latest["mic"],
                latest["quote_currency"],
                latest["mapping_version"],
            )
            expected_identity = (
                mapping_id,
                bar.listing_id,
                bar.mic,
                bar.quote_currency,
                bar.mapping_version,
            )
            if identity != expected_identity:
                raise MarketDataPersistenceError(
                    "bar correction changes its canonical listing or mapping"
                )
            stored_values = (
                latest["open"],
                latest["high"],
                latest["low"],
                latest["close"],
                latest["volume"],
            )
            observed_values = (
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
            )
            if stored_values == observed_values:
                return latest["id"], False
            revision = latest["revision"] + 1
        else:
            revision = 1
        row = connection.execute(
            """
            INSERT INTO public.market_eod_bars (
                user_id, instrument_id, provider_identifier_id, ingestion_run_id,
                provider, provider_symbol, mic, quote_currency, mapping_version,
                market_date, open, high, low, close, volume, provider_as_of,
                retrieved_at, source_url, completeness_status, revision,
                response_sha256, retain_until
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) RETURNING id
            """,
            (
                user_id,
                bar.listing_id,
                mapping_id,
                bar.ingestion_run_id,
                bar.provider,
                bar.provider_symbol,
                bar.mic,
                bar.quote_currency,
                bar.mapping_version,
                bar.market_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.provider_as_of,
                bar.retrieved_at,
                bar.source_url,
                bar.completeness_status.value,
                revision,
                bar.response_sha256,
                bar.market_date + timedelta(days=retention_days),
            ),
        ).fetchone()
        assert row is not None
        return row["id"], True

    @staticmethod
    def _record_observation(
        connection: psycopg.Connection[dict[str, object]],
        user_id: str,
        bar_id: UUID,
        outcome: FetchOutcome,
    ) -> None:
        connection.execute(
            """
            INSERT INTO public.market_eod_bar_ingestions (
                user_id, bar_id, ingestion_run_id, provider, provider_as_of,
                retrieved_at, source_url, response_sha256
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, bar_id, ingestion_run_id) DO NOTHING
            """,
            (
                user_id,
                bar_id,
                outcome.ingestion_run_id,
                outcome.provider,
                outcome.provider_as_of,
                outcome.retrieved_at,
                outcome.source_url,
                outcome.response_sha256,
            ),
        )


def _fetch_fingerprint(outcome: FetchOutcome, assessment: FetchAssessment) -> str:
    payload = {
        "assessment": assessment.model_dump(mode="json"),
        "outcome": outcome.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
