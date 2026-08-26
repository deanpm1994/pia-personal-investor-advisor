"""Controlled daily EOD scheduling, eligibility, quota, and audit orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.market_data import (
    DiagnosticCode,
    FetchStatus,
    ProviderMapping,
    QuotaState,
    assess_fetch_outcome,
)
from pia_api.providers.marketstack import (
    MarketstackDailyBarProvider,
    RequestBudget,
)
from pia_api.services.market_data import TrustedMarketDataGateway

ATTESTATION_VERSION = "adr-0009-founder-risk-v1"
PROVIDER = "marketstack"
MONTHLY_LIMIT = 100
ROUTINE_LIMIT = 72
ACTIVE_INSTRUMENT_LIMIT = 3


class DailyBarProvider(Protocol):
    async def fetch(
        self, mapping: ProviderMapping, start_date: date, end_date: date
    ): ...


@dataclass(frozen=True)
class EligibleMapping:
    mapping_id: UUID
    mapping: ProviderMapping
    latest_market_date: date | None


@dataclass(frozen=True)
class ProviderGate:
    allowed: bool
    status: str


@dataclass(frozen=True)
class MarketEodJobResult:
    job_id: str
    status: str
    target_date: date | None
    eligible_instruments: int
    fetched_instruments: int
    successful_instruments: int
    diagnostics: tuple[str, ...]


class TrustedMarketIngestionStore:
    """Server-only scheduler state with atomic quota reservations."""

    def __init__(self, settings: Settings) -> None:
        self._database_url = settings.database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )

    async def provider_gate(
        self, user: AuthenticatedUser, run_at: datetime
    ) -> ProviderGate:
        return await asyncio.to_thread(self._provider_gate, user.id, run_at)

    async def eligible_mappings(
        self, user: AuthenticatedUser
    ) -> tuple[EligibleMapping, ...]:
        return await asyncio.to_thread(self._eligible_mappings, user.id)

    async def reserve_quota(
        self, user: AuthenticatedUser, attempt: int, run_at: datetime
    ) -> QuotaState | None:
        return await asyncio.to_thread(self._reserve_quota, user.id, attempt, run_at)

    async def record_job(
        self,
        user: AuthenticatedUser,
        job_id: UUID,
        scheduled_for: datetime,
        result: MarketEodJobResult,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._record_job,
            user.id,
            job_id,
            scheduled_for,
            result,
            started_at,
            finished_at,
        )

    async def purge_content(
        self,
        user: AuthenticatedUser,
        run_at: datetime,
        *,
        all_provider_content: bool,
    ) -> int:
        return await asyncio.to_thread(
            self._purge_content, user.id, run_at, all_provider_content
        )

    def _provider_gate(self, user_id: str, run_at: datetime) -> ProviderGate:
        owner_id = _owner_uuid(user_id)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """
                SELECT access_status, license_review_due_at,
                       risk_attestation_version, risk_attested_at,
                       risk_withdrawn_at
                FROM public.market_provider_access
                WHERE user_id = %s AND provider = 'marketstack'
                """,
                (owner_id,),
            ).fetchone()
        if row is None or row["access_status"] != "enabled":
            return ProviderGate(False, "provider_disabled")
        if (
            row["license_review_due_at"] is None
            or row["license_review_due_at"] <= run_at
        ):
            return ProviderGate(False, "license_review_required")
        if (
            row["risk_attestation_version"] != ATTESTATION_VERSION
            or row["risk_attested_at"] is None
            or row["risk_attested_at"] > run_at
            or row["risk_withdrawn_at"] is not None
        ):
            return ProviderGate(False, "provider_disabled")
        return ProviderGate(True, "enabled")

    def _eligible_mappings(self, user_id: str) -> tuple[EligibleMapping, ...]:
        owner_id = _owner_uuid(user_id)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """
                WITH latest_snapshot AS (
                    SELECT content FROM public.financial_snapshots
                    WHERE user_id = %s
                    ORDER BY refreshed_at DESC, id DESC LIMIT 1
                ), portfolio_isins AS (
                    SELECT position ->> 'instrument_id' AS isin
                    FROM latest_snapshot,
                    LATERAL jsonb_array_elements(
                        COALESCE(content -> 'positions' -> 'owner', '[]'::jsonb)
                    ) AS position
                )
                SELECT m.id, m.instrument_id, m.provider, m.provider_symbol,
                       m.provider_exchange_code, m.mic, m.quote_currency,
                       m.mapping_version, m.valid_from, m.valid_to,
                       m.resolved_at, m.resolution_source_url,
                       m.resolution_status,
                       max(b.market_date) AS latest_market_date
                FROM public.market_provider_identifiers AS m
                JOIN public.market_instruments AS i
                  ON i.id = m.instrument_id AND i.user_id = m.user_id
                LEFT JOIN public.market_eod_bars AS b
                  ON b.user_id = m.user_id
                 AND b.provider_identifier_id = m.id
                WHERE m.user_id = %s AND m.provider = 'marketstack'
                  AND m.valid_to IS NULL AND m.resolution_status = 'supported'
                  AND (
                    EXISTS (
                        SELECT 1 FROM public.market_watchlist_entries AS w
                        WHERE w.user_id = m.user_id
                          AND w.instrument_id = m.instrument_id
                    )
                    OR i.isin IN (SELECT isin FROM portfolio_isins)
                  )
                GROUP BY m.id
                ORDER BY m.resolved_at, m.id
                LIMIT 4
                """,
                (owner_id, owner_id),
            ).fetchall()
        return tuple(
            EligibleMapping(
                mapping_id=row["id"],
                mapping=ProviderMapping(
                    instrument_id=row["instrument_id"],
                    provider=row["provider"],
                    provider_symbol=row["provider_symbol"],
                    provider_exchange_code=row["provider_exchange_code"],
                    mic=row["mic"],
                    quote_currency=row["quote_currency"],
                    mapping_version=row["mapping_version"],
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                    resolved_at=row["resolved_at"],
                    resolution_source_url=row["resolution_source_url"],
                    resolution_status=row["resolution_status"],
                ),
                latest_market_date=row["latest_market_date"],
            )
            for row in rows
        )

    def _reserve_quota(
        self, user_id: str, attempt: int, run_at: datetime
    ) -> QuotaState | None:
        owner_id = _owner_uuid(user_id)
        month_start = run_at.date().replace(day=1)
        column = "routine_requests" if attempt == 1 else "reserve_requests"
        category_limit = ROUTINE_LIMIT if attempt == 1 else MONTHLY_LIMIT
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO public.market_quota_usage (
                        user_id, provider, month_start
                    ) VALUES (%s, 'marketstack', %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (owner_id, month_start),
                )
                row = connection.execute(
                    f"""
                    UPDATE public.market_quota_usage
                    SET {column} = {column} + 1, updated_at = now()
                    WHERE user_id = %s AND provider = 'marketstack'
                      AND month_start = %s
                      AND routine_requests + reserve_requests < %s
                      AND {column} < %s
                    RETURNING routine_requests, reserve_requests
                    """,
                    (owner_id, month_start, MONTHLY_LIMIT, category_limit),
                ).fetchone()
                if row is None:
                    return None
                used = row["routine_requests"] + row["reserve_requests"]
                return QuotaState(
                    limit=MONTHLY_LIMIT,
                    used=used,
                    remaining=MONTHLY_LIMIT - used,
                    reset_at=_next_month(run_at),
                )

    def _record_job(
        self,
        user_id: str,
        job_id: UUID,
        scheduled_for: datetime,
        result: MarketEodJobResult,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        owner_id = _owner_uuid(user_id)
        with psycopg.connect(self._database_url) as connection:
            connection.execute(
                """
                INSERT INTO public.market_schedule_runs (
                    id, user_id, provider, scheduled_for, target_date, status,
                    eligible_instruments, fetched_instruments,
                    successful_instruments, diagnostics, started_at, finished_at
                ) VALUES (
                    %s, %s, 'marketstack', %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    job_id,
                    owner_id,
                    scheduled_for,
                    result.target_date,
                    result.status,
                    result.eligible_instruments,
                    result.fetched_instruments,
                    result.successful_instruments,
                    Jsonb(list(result.diagnostics)),
                    started_at,
                    finished_at,
                ),
            )

    def _purge_content(
        self, user_id: str, run_at: datetime, all_provider_content: bool
    ) -> int:
        owner_id = _owner_uuid(user_id)
        with psycopg.connect(self._database_url) as connection:
            deleted = connection.execute(
                """
                DELETE FROM public.market_eod_bars
                WHERE user_id = %s AND provider = 'marketstack'
                  AND (%s OR retain_until < %s)
                """,
                (owner_id, all_provider_content, run_at.date()),
            ).rowcount
        return deleted


class _StoreBudget(RequestBudget):
    def __init__(
        self,
        store: TrustedMarketIngestionStore,
        user: AuthenticatedUser,
        run_at: datetime,
    ) -> None:
        self._store = store
        self._user = user
        self._run_at = run_at

    async def reserve(self, attempt: int) -> QuotaState | None:
        return await self._store.reserve_quota(self._user, attempt, self._run_at)


class MarketEodCoordinator:
    """Run only scheduled eligible listings; never called by an HTTP route."""

    def __init__(
        self,
        settings: Settings,
        store: TrustedMarketIngestionStore,
        persistence: TrustedMarketDataGateway,
        provider_factory,
        *,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._store = store
        self._persistence = persistence
        self._provider_factory = provider_factory
        self._clock = clock

    async def run(
        self, user: AuthenticatedUser, scheduled_for: datetime
    ) -> MarketEodJobResult:
        if scheduled_for.tzinfo is None or scheduled_for.utcoffset() is None:
            raise ValueError("scheduled_for must include a timezone offset")
        scheduled_for = scheduled_for.astimezone(UTC)
        started_at = self._clock()
        job_id = uuid4()
        target = scheduled_target(scheduled_for)
        diagnostics: list[str] = []
        if not self._settings.marketstack_enabled:
            diagnostics.append(DiagnosticCode.PROVIDER_DISABLED.value)
            await self._purge_disabled_content(user, scheduled_for, diagnostics)
            result = MarketEodJobResult(
                str(job_id), "provider_disabled", target, 0, 0, 0, tuple(diagnostics)
            )
            await self._record(user, job_id, scheduled_for, result, started_at)
            return result
        gate = await self._store.provider_gate(user, scheduled_for)
        if not gate.allowed:
            code = (
                DiagnosticCode.LICENSE_REVIEW_REQUIRED
                if gate.status == "license_review_required"
                else DiagnosticCode.PROVIDER_DISABLED
            )
            diagnostics.append(code.value)
            await self._purge_disabled_content(user, scheduled_for, diagnostics)
            result = MarketEodJobResult(
                str(job_id), gate.status, target, 0, 0, 0, tuple(diagnostics)
            )
            await self._record(user, job_id, scheduled_for, result, started_at)
            return result
        access_key = self._settings.marketstack_access_key
        if access_key is None or not access_key.get_secret_value():
            diagnostics.append(DiagnosticCode.PROVIDER_DISABLED.value)
            await self._purge_disabled_content(user, scheduled_for, diagnostics)
            result = MarketEodJobResult(
                str(job_id), "provider_disabled", target, 0, 0, 0, tuple(diagnostics)
            )
            await self._record(user, job_id, scheduled_for, result, started_at)
            return result

        expired = await self._store.purge_content(
            user, scheduled_for, all_provider_content=False
        )
        if expired:
            diagnostics.append(DiagnosticCode.PROVIDER_CONTENT_PURGED.value)
        if target is None:
            result = MarketEodJobResult(
                str(job_id), "skipped", None, 0, 0, 0, tuple(diagnostics)
            )
            await self._record(user, job_id, scheduled_for, result, started_at)
            return result
        eligible = await self._store.eligible_mappings(user)
        if len(eligible) > ACTIVE_INSTRUMENT_LIMIT:
            diagnostics.append(DiagnosticCode.ACTIVE_INSTRUMENT_CAP.value)
        selected = eligible[:ACTIVE_INSTRUMENT_LIMIT]
        successful = 0
        fetched = 0
        budget = _StoreBudget(self._store, user, scheduled_for)
        provider = self._provider_factory(access_key.get_secret_value(), budget)
        for item in selected:
            start_date = (
                target - timedelta(days=365)
                if item.latest_market_date is None
                else target - timedelta(days=7)
            )
            outcome = await provider.fetch(item.mapping, start_date, target)
            fetched += int(outcome.status is not FetchStatus.QUOTA_EXHAUSTED)
            assessment = assess_fetch_outcome(
                outcome, mapping=item.mapping, target_date=target
            )
            await self._persistence.persist_fetch(
                user, item.mapping_id, item.mapping, outcome, assessment
            )
            diagnostics.extend(
                diagnostic.code.value for diagnostic in assessment.diagnostics
            )
            if assessment.accepted_bars:
                successful += 1
            if outcome.status is FetchStatus.QUOTA_EXHAUSTED:
                break
        if any(code == DiagnosticCode.QUOTA_EXHAUSTED.value for code in diagnostics):
            status = "quota_exhausted" if successful == 0 else "partial"
        elif successful == len(selected) and len(eligible) <= ACTIVE_INSTRUMENT_LIMIT:
            status = "completed"
        elif successful:
            status = "partial"
        else:
            status = "failed"
        result = MarketEodJobResult(
            str(job_id),
            status,
            target,
            len(eligible),
            fetched,
            successful,
            tuple(sorted(set(diagnostics))),
        )
        await self._record(user, job_id, scheduled_for, result, started_at)
        return result

    async def _purge_disabled_content(
        self,
        user: AuthenticatedUser,
        run_at: datetime,
        diagnostics: list[str],
    ) -> None:
        deleted = await self._store.purge_content(
            user, run_at, all_provider_content=True
        )
        if deleted:
            diagnostics.append(DiagnosticCode.PROVIDER_CONTENT_PURGED.value)

    async def _record(
        self,
        user: AuthenticatedUser,
        job_id: UUID,
        scheduled_for: datetime,
        result: MarketEodJobResult,
        started_at: datetime,
    ) -> None:
        await self._store.record_job(
            user,
            job_id,
            scheduled_for,
            result,
            started_at,
            self._clock(),
        )


def scheduled_target(run_at: datetime) -> date | None:
    """Return the previous UTC weekday only for the approved run window."""
    run_at = run_at.astimezone(UTC)
    if run_at.weekday() in {6, 0} or run_at.time() < time(6, 0):
        return None
    target = run_at.date() - timedelta(days=1)
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target


async def run_market_eod(settings: Settings, run_at: datetime) -> MarketEodJobResult:
    user = AuthenticatedUser(id=settings.market_eod_owner_id, email=None)
    store = TrustedMarketIngestionStore(settings)
    coordinator = MarketEodCoordinator(
        settings,
        store,
        TrustedMarketDataGateway(settings),
        lambda key, budget: MarketstackDailyBarProvider(key, budget),
    )
    return await coordinator.run(user, run_at)


def _owner_uuid(user_id: str) -> UUID:
    try:
        return UUID(user_id)
    except (TypeError, ValueError) as error:
        raise ValueError("market EOD owner id must be a UUID") from error


def _next_month(run_at: datetime) -> datetime:
    year = run_at.year + int(run_at.month == 12)
    month = 1 if run_at.month == 12 else run_at.month + 1
    return datetime(year, month, 1, tzinfo=UTC)
