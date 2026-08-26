"""Scheduling and provider-isolation tests for controlled EOD ingestion."""

import asyncio
import inspect
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from pia_api.api import market as market_api
from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.market_data import (
    CompletenessStatus,
    DailyBar,
    FetchOutcome,
    FetchStatus,
    ProviderMapping,
    QuotaState,
    ResolutionStatus,
    assess_fetch_outcome,
)
from pia_api.services.market_ingestion import (
    EligibleMapping,
    MarketEodCoordinator,
    ProviderGate,
    scheduled_target,
)

OWNER = AuthenticatedUser(id="10000000-0000-0000-0000-000000000001", email=None)
RUN_AT = datetime(2026, 8, 26, 6, tzinfo=UTC)


def _mapping() -> ProviderMapping:
    return ProviderMapping(
        instrument_id=UUID("20000000-0000-0000-0000-000000000001"),
        provider="marketstack",
        provider_symbol="SYNX",
        provider_exchange_code="XMAD",
        mic="XMAD",
        quote_currency="EUR",
        mapping_version=1,
        valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 8, 1, tzinfo=UTC),
        resolution_source_url="https://mapping.example.test/marketstack/SYNX",
        resolution_status=ResolutionStatus.SUPPORTED,
    )


class _Store:
    def __init__(self, gate: ProviderGate, eligible=()) -> None:
        self.gate = gate
        self.eligible = eligible
        self.recorded = []
        self.purges = []

    async def provider_gate(self, user, run_at):
        return self.gate

    async def eligible_mappings(self, user):
        return self.eligible

    async def reserve_quota(self, user, attempt, run_at):
        return QuotaState(limit=100, used=1, remaining=99)

    async def record_job(self, *args):
        self.recorded.append(args)

    async def purge_content(self, user, run_at, *, all_provider_content):
        self.purges.append(all_provider_content)
        return 0


class _Persistence:
    def __init__(self) -> None:
        self.calls = []

    async def persist_fetch(self, *args):
        self.calls.append(args)


class _Provider:
    def __init__(self, status=FetchStatus.COMPLETED) -> None:
        self.status = status
        self.requests = []

    async def fetch(self, mapping, start_date, end_date):
        self.requests.append((mapping, start_date, end_date))
        run_id = uuid4()
        bars = ()
        completeness = CompletenessStatus.UNAVAILABLE
        if self.status is FetchStatus.COMPLETED:
            bars = (
                DailyBar(
                    listing_id=mapping.instrument_id,
                    market_date=end_date,
                    open="10.10",
                    high="11.20",
                    low="9.90",
                    close="11.00",
                    volume=1200,
                    provider="marketstack",
                    provider_symbol=mapping.provider_symbol,
                    mic=mapping.mic,
                    quote_currency=mapping.quote_currency,
                    provider_as_of=RUN_AT,
                    retrieved_at=RUN_AT,
                    ingestion_run_id=run_id,
                    source_url="https://api.marketstack.com/v2/eod?symbols=SYNX",
                    mapping_version=1,
                    completeness_status=CompletenessStatus.COMPLETE,
                    revision=1,
                    response_sha256="a" * 64,
                ),
            )
            completeness = CompletenessStatus.COMPLETE
        return FetchOutcome(
            ingestion_run_id=run_id,
            provider="marketstack",
            provider_symbol=mapping.provider_symbol,
            mic=mapping.mic,
            quote_currency=mapping.quote_currency,
            requested_start=start_date,
            requested_end=end_date,
            provider_as_of=RUN_AT,
            started_at=RUN_AT,
            retrieved_at=RUN_AT,
            source_url="https://api.marketstack.com/v2/eod?symbols=SYNX",
            request_parameters={"symbols": "SYNX"},
            response_sha256="a" * 64,
            completeness_status=completeness,
            status=self.status,
            quota_state=QuotaState(limit=100, used=1, remaining=99),
            bars=bars,
        )


def _coordinator(store, persistence, provider, *, enabled=True):
    settings = Settings(
        marketstack_enabled=enabled,
        marketstack_access_key="test-key",
        market_eod_owner_id=OWNER.id,
    )
    return MarketEodCoordinator(
        settings,
        store,
        persistence,
        lambda key, budget: provider,
        clock=lambda: RUN_AT,
    )


def test_schedule_targets_only_previous_weekday_after_approved_utc_window() -> None:
    assert scheduled_target(datetime(2026, 8, 25, 5, 59, tzinfo=UTC)) is None
    assert scheduled_target(datetime(2026, 8, 25, 6, tzinfo=UTC)) == date(2026, 8, 24)
    assert scheduled_target(datetime(2026, 8, 23, 7, tzinfo=UTC)) is None


def test_disabled_or_unattested_provider_never_constructs_adapter() -> None:
    store = _Store(ProviderGate(False, "provider_disabled"))
    persistence = _Persistence()
    constructed = 0

    def factory(key, budget):
        nonlocal constructed
        constructed += 1
        return _Provider()

    coordinator = MarketEodCoordinator(
        Settings(marketstack_enabled=True, marketstack_access_key="test-key"),
        store,
        persistence,
        factory,
        clock=lambda: RUN_AT,
    )
    result = asyncio.run(coordinator.run(OWNER, RUN_AT))

    assert result.status == "provider_disabled"
    assert constructed == 0
    assert persistence.calls == []
    assert store.purges == [True]


def test_coordinator_fetches_eligible_mapping_and_persists_assessment() -> None:
    mapping = _mapping()
    store = _Store(
        ProviderGate(True, "enabled"),
        (EligibleMapping(uuid4(), mapping, date(2026, 8, 24)),),
    )
    persistence = _Persistence()
    provider = _Provider()

    result = asyncio.run(_coordinator(store, persistence, provider).run(OWNER, RUN_AT))

    assert result.status == "completed"
    assert result.target_date == date(2026, 8, 25)
    assert provider.requests[0][1:] == (date(2026, 8, 18), date(2026, 8, 25))
    assert len(persistence.calls) == 1
    assert store.purges == [False]
    outcome = persistence.calls[0][3]
    assessment = persistence.calls[0][4]
    assert assessment == assess_fetch_outcome(
        outcome, mapping=mapping, target_date=date(2026, 8, 25)
    )


def test_http_market_routes_have_no_provider_or_scheduler_dependency() -> None:
    source = inspect.getsource(market_api)

    assert "MarketstackDailyBarProvider" not in source
    assert "MarketEodCoordinator" not in source
    assert "api.marketstack.com" not in source
