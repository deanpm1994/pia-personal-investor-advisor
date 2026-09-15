"""Synthetic Phase 6 reconciliation across resolution, storage, API, and RLS."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, time, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from jwt import InvalidTokenError
from psycopg.types.json import Jsonb

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.market_data import (
    CompletenessStatus,
    DailyBar,
    FetchOutcome,
    FetchStatus,
    InstrumentIdentity,
    InstrumentKind,
    ListingIdentity,
    ProviderMapping,
    ResolutionCandidate,
    ResolutionOutcome,
    ResolutionStatus,
)
from pia_api.main import create_app
from pia_api.services.market_analysis import TrustedMarketAnalysisGateway
from pia_api.services.market_data import TrustedMarketDataGateway
from pia_api.services.market_ingestion import (
    ATTESTATION_VERSION,
    MarketEodCoordinator,
    TrustedMarketIngestionStore,
    scheduled_target,
)
from pia_api.services.market_watchlist import TrustedMarketWatchlistGateway

pytestmark = pytest.mark.local_supabase

ISIN = "US0000000002"
SOURCE_URL = "https://api.marketstack.com/v2/eod?symbols=SYNX"


@pytest.fixture(scope="module")
def database_url() -> str:
    if os.environ.get("PIA_RUN_LOCAL_SUPABASE_TESTS") != "1":
        pytest.skip("set PIA_RUN_LOCAL_SUPABASE_TESTS=1 to run local Supabase tests")
    return Settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _scheduled_run() -> datetime:
    candidate = datetime.now(UTC).date()
    while candidate.weekday() not in {1, 2, 3, 4, 5}:
        candidate -= timedelta(days=1)
    return datetime.combine(candidate, time(6), UTC)


def _weekdays(count: int, end: date) -> tuple[date, ...]:
    days: list[date] = []
    candidate = end
    while len(days) < count:
        if candidate.weekday() < 5:
            days.append(candidate)
        candidate -= timedelta(days=1)
    return tuple(reversed(days))


def _insert_auth_user(connection, user_id: uuid.UUID) -> None:
    connection.execute(
        """
        INSERT INTO auth.users (
            id, instance_id, aud, role, email, encrypted_password,
            email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
            created_at, updated_at
        ) VALUES (
            %s, '00000000-0000-0000-0000-000000000000', 'authenticated',
            'authenticated', %s, '', now(),
            '{"provider":"email","providers":["email"]}', '{}', now(), now()
        )
        """,
        (user_id, f"phase6-{user_id}@example.test"),
    )


def _as_authenticated_user(connection, user_id: uuid.UUID) -> None:
    connection.execute("SET LOCAL ROLE authenticated")
    connection.execute(
        "SELECT set_config('request.jwt.claim.sub', %s, true)", (str(user_id),)
    )
    connection.execute(
        "SELECT set_config('request.jwt.claim.role', 'authenticated', true)"
    )


def _seed_phase5_snapshot(connection, owner_id: uuid.UUID) -> dict[str, object]:
    content = {
        "positions": {
            "owner": [
                {
                    "instrument_id": ISIN,
                    "quantity": "2.000000000000",
                    "evidence_event_ids": ["synthetic-buy"],
                }
            ]
        },
        "fifo": {
            "open_lots": [
                {
                    "instrument_id": ISIN,
                    "quantity": "2.000000000000",
                    "total_basis": "200.000000000000",
                    "source_currency": "EUR",
                    "evidence_event_ids": ["synthetic-buy"],
                }
            ]
        },
    }
    connection.execute(
        """
        INSERT INTO public.financial_snapshots (
            user_id, input_fingerprint, input_watermark, input_counts, content
        ) VALUES (%s, %s, '{}', '{}', %s)
        """,
        (owner_id, "f" * 64, Jsonb(content)),
    )
    return content


class _Resolver:
    def __init__(self, resolved_at: datetime) -> None:
        self.resolved_at = resolved_at
        self.calls: list[str] = []

    async def resolve_isin(self, isin: str) -> ResolutionOutcome:
        self.calls.append(isin)
        instrument_id = uuid.uuid4()
        mapping = ProviderMapping(
            instrument_id=instrument_id,
            provider="marketstack",
            provider_symbol="SYNX",
            provider_exchange_code="XMAD",
            mic="XMAD",
            quote_currency="EUR",
            mapping_version=1,
            valid_from=self.resolved_at,
            resolved_at=self.resolved_at,
            resolution_source_url="https://resolver.example.test/v3/mapping",
            resolution_status=ResolutionStatus.SUPPORTED,
        )
        return ResolutionOutcome(
            requested_isin=isin,
            provider="synthetic-resolver",
            status=ResolutionStatus.SUPPORTED,
            retrieved_at=self.resolved_at,
            source_url="https://resolver.example.test/v3/mapping",
            candidates=(
                ResolutionCandidate(
                    instrument=InstrumentIdentity(
                        isin=isin,
                        share_class_figi="BBG000000001",
                        instrument_kind=InstrumentKind.COMMON_STOCK,
                    ),
                    display_name="Synthetic Equity",
                    listing=ListingIdentity(
                        instrument_id=instrument_id,
                        mic="XMAD",
                        quote_currency="EUR",
                    ),
                    mapping=mapping,
                ),
            ),
        )


class _Provider:
    def __init__(self, budget, run_at: datetime) -> None:
        self.budget = budget
        self.run_at = run_at
        self.calls: list[tuple[date, date]] = []

    async def fetch(
        self, mapping: ProviderMapping, start_date: date, end_date: date
    ) -> FetchOutcome:
        self.calls.append((start_date, end_date))
        quota = await self.budget.reserve(1)
        assert quota is not None
        run_id = uuid.uuid4()
        bars = tuple(
            DailyBar(
                listing_id=mapping.instrument_id,
                market_date=market_date,
                open=str(index),
                high=str(index),
                low=str(index),
                close=str(index),
                volume=1000 + index,
                provider=mapping.provider,
                provider_symbol=mapping.provider_symbol,
                mic=mapping.mic,
                quote_currency=mapping.quote_currency,
                provider_as_of=self.run_at - timedelta(hours=7),
                retrieved_at=self.run_at,
                ingestion_run_id=run_id,
                source_url=SOURCE_URL,
                mapping_version=mapping.mapping_version,
                completeness_status=CompletenessStatus.COMPLETE,
                revision=1,
                response_sha256="a" * 64,
            )
            for index, market_date in enumerate(_weekdays(200, end_date), start=1)
        )
        return FetchOutcome(
            ingestion_run_id=run_id,
            provider=mapping.provider,
            provider_symbol=mapping.provider_symbol,
            mic=mapping.mic,
            quote_currency=mapping.quote_currency,
            requested_start=start_date,
            requested_end=end_date,
            provider_as_of=self.run_at - timedelta(hours=7),
            started_at=self.run_at - timedelta(minutes=1),
            retrieved_at=self.run_at,
            source_url=SOURCE_URL,
            request_parameters={"symbols": mapping.provider_symbol},
            response_sha256="a" * 64,
            completeness_status=CompletenessStatus.COMPLETE,
            status=FetchStatus.COMPLETED,
            quota_state=quota,
            bars=bars,
        )


class _Verifier:
    def __init__(self, owner_id: uuid.UUID, other_id: uuid.UUID) -> None:
        self.users = {
            "owner-token": AuthenticatedUser(id=str(owner_id), email=None),
            "other-token": AuthenticatedUser(id=str(other_id), email=None),
        }

    async def verify(self, token: str) -> AuthenticatedUser:
        try:
            return self.users[token]
        except KeyError as error:
            raise InvalidTokenError("invalid synthetic token") from error


def _snapshot_facts(connection, owner_id: uuid.UUID) -> tuple[object, ...]:
    return connection.execute(
        """
        SELECT input_fingerprint, input_watermark, input_counts, content
        FROM public.financial_snapshots WHERE user_id = %s
        ORDER BY refreshed_at, id
        """,
        (owner_id,),
    ).fetchall()


def test_phase6_synthetic_path_reconciles_and_preserves_phase5_facts(
    database_url: str,
) -> None:
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    owner = AuthenticatedUser(id=str(owner_id), email=None)
    run_at = _scheduled_run()
    target_date = scheduled_target(run_at)
    assert target_date is not None
    settings = Settings(
        database_url=database_url,
        marketstack_enabled=True,
        marketstack_access_key="synthetic-private-key",
        market_eod_owner_id=str(owner_id),
    )
    resolver = _Resolver(run_at - timedelta(days=365))
    watchlist = TrustedMarketWatchlistGateway(settings, resolver)
    ingestion_store = TrustedMarketIngestionStore(settings)
    persistence = TrustedMarketDataGateway(settings)
    providers: list[_Provider] = []

    def provider_factory(_key: str, budget) -> _Provider:
        provider = _Provider(budget, run_at)
        providers.append(provider)
        return provider

    coordinator = MarketEodCoordinator(
        settings,
        ingestion_store,
        persistence,
        provider_factory,
        clock=lambda: run_at,
    )
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(connection, owner_id)
            _insert_auth_user(connection, other_id)
            expected_snapshot = _seed_phase5_snapshot(connection, owner_id)
            baseline_facts = _snapshot_facts(connection, owner_id)
            connection.execute(
                """
                INSERT INTO public.market_provider_access (
                    user_id, provider, access_status, license_checked_at,
                    license_review_due_at, risk_attestation_version,
                    risk_attested_at
                ) VALUES (
                    %s, 'marketstack', 'enabled', %s, %s, %s, %s
                )
                """,
                (
                    owner_id,
                    run_at - timedelta(days=1),
                    run_at + timedelta(days=30),
                    ATTESTATION_VERSION,
                    run_at - timedelta(days=1),
                ),
            )

        mutation = asyncio.run(watchlist.add(owner, ISIN))
        assert mutation.status == "added"
        assert mutation.entry is not None
        assert mutation.entry["isin"] == ISIN
        assert resolver.calls == [ISIN]

        first_run = asyncio.run(coordinator.run(owner, run_at))
        second_run = asyncio.run(coordinator.run(owner, run_at))
        assert first_run.status == second_run.status == "completed"
        assert first_run.successful_instruments == 1
        assert providers[0].calls == [(target_date - timedelta(days=365), target_date)]

        analysis_gateway = TrustedMarketAnalysisGateway(settings)
        items = asyncio.run(analysis_gateway.list_analysis(owner))
        assert len(items) == 1
        item = items[0]
        assert item["source_kind"] == "portfolio_and_watchlist"
        assert item["state"] == "ready"
        assert len(item["bars"]) == 200
        assert item["bars"][-1]["market_date"] == target_date
        assert item["bars"][-1]["close"] == "200.000000000000"
        latest_indicators = {
            result["code"]: result["value"]
            for result in item["indicators"]
            if result["market_date"] == target_date
        }
        assert latest_indicators == {
            "sma_20": "190.500000000000",
            "sma_50": "175.500000000000",
            "sma_200": "100.500000000000",
            "rsi_14": "100.000000000000",
        }
        assert item["valuation"] == {
            "status": "available",
            "quote_currency": "EUR",
            "current_price": "200.000000000000",
            "current_value": "400.000000000000",
            "total_basis": "200.000000000000",
            "unrealized_gain": "200.000000000000",
            "unrealized_return_percent": "100.000000000000",
            "evidence_event_ids": ["synthetic-buy"],
        }
        assert item["source"] == {
            "provider": "marketstack",
            "provider_symbol": "SYNX",
            "mic": "XMAD",
            "quote_currency": "EUR",
            "attribution": "Market data: Marketstack",
            "source_urls": [SOURCE_URL],
            "provider_as_of": run_at - timedelta(hours=7),
            "retrieved_at": run_at,
        }

        app = create_app(settings)
        app.state.jwt_verifier = _Verifier(owner_id, other_id)
        app.state.market_analysis_gateway = analysis_gateway
        client = TestClient(app)
        assert client.get("/v1/market/analysis").status_code == 401
        owner_response = client.get(
            "/v1/market/analysis",
            headers={"Authorization": "Bearer owner-token"},
        )
        other_response = client.get(
            "/v1/market/analysis",
            headers={"Authorization": "Bearer other-token"},
        )
        assert owner_response.status_code == 200
        assert owner_response.json()["items"][0]["valuation"]["current_value"] == (
            "400.000000000000"
        )
        assert other_response.json() == {"state": "empty", "items": []}

        with psycopg.connect(database_url) as connection:
            assert _snapshot_facts(connection, owner_id) == baseline_facts
            assert baseline_facts[0][3] == expected_snapshot
            assert connection.execute(
                """
                SELECT count(*), min(revision), max(revision)
                FROM public.market_eod_bars WHERE user_id = %s
                """,
                (owner_id,),
            ).fetchone() == (200, 1, 1)
            with connection.transaction():
                _as_authenticated_user(connection, other_id)
                assert connection.execute(
                    "SELECT count(*) FROM public.market_watchlist_entries"
                ).fetchone() == (0,)
                assert connection.execute(
                    "SELECT count(*) FROM public.market_eod_bars"
                ).fetchone() == (0,)
                assert connection.execute(
                    "SELECT count(*) FROM public.financial_snapshots"
                ).fetchone() == (0,)

        disabled_settings = Settings(
            database_url=database_url,
            marketstack_enabled=False,
            market_eod_owner_id=str(owner_id),
        )
        disabled_coordinator = MarketEodCoordinator(
            disabled_settings,
            ingestion_store,
            persistence,
            provider_factory,
            clock=lambda: run_at,
        )
        disabled = asyncio.run(disabled_coordinator.run(owner, run_at))
        assert disabled.status == "provider_disabled"
        assert len(providers) == 2
        hidden = asyncio.run(
            TrustedMarketAnalysisGateway(disabled_settings).list_analysis(owner)
        )
        assert hidden[0]["state"] == "provider_disabled"
        assert hidden[0]["bars"] == []
        assert hidden[0]["valuation"] is None
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT count(*) FROM public.market_eod_bars WHERE user_id = %s",
                (owner_id,),
            ).fetchone() == (0,)
            assert _snapshot_facts(connection, owner_id) == baseline_facts
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (owner_id, other_id),
            )
