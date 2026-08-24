"""Local-Supabase tests for trusted watchlist persistence and RLS."""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.market_data import (
    InstrumentIdentity,
    InstrumentKind,
    ListingIdentity,
    ProviderMapping,
    ResolutionCandidate,
    ResolutionOutcome,
    ResolutionStatus,
)
from pia_api.services.market_watchlist import TrustedMarketWatchlistGateway

pytestmark = pytest.mark.local_supabase

ISIN = "US0378331005"


@pytest.fixture(scope="module")
def database_url() -> str:
    if os.environ.get("PIA_RUN_LOCAL_SUPABASE_TESTS") != "1":
        pytest.skip("set PIA_RUN_LOCAL_SUPABASE_TESTS=1 to run local Supabase tests")
    return Settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


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
        (user_id, f"watchlist-{user_id}@example.test"),
    )


def _as_authenticated_user(connection, user_id: uuid.UUID) -> None:
    connection.execute("SET LOCAL ROLE authenticated")
    connection.execute(
        "SELECT set_config('request.jwt.claim.sub', %s, true)", (str(user_id),)
    )
    connection.execute(
        "SELECT set_config('request.jwt.claim.role', 'authenticated', true)"
    )


class SyntheticResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_isin(self, isin: str) -> ResolutionOutcome:
        self.calls += 1
        instrument_id = uuid.uuid4()
        now = datetime.now(UTC)
        return ResolutionOutcome(
            requested_isin=isin,
            provider="synthetic-resolver",
            status=ResolutionStatus.SUPPORTED,
            retrieved_at=now,
            source_url="https://resolver.example.test/v3/mapping",
            candidates=(
                ResolutionCandidate(
                    instrument=InstrumentIdentity(
                        isin=isin,
                        share_class_figi="BBG000000001",
                        instrument_kind=InstrumentKind.COMMON_STOCK,
                    ),
                    display_name="Synthetic Portfolio Equity",
                    listing=ListingIdentity(
                        instrument_id=instrument_id,
                        mic="XNAS",
                        quote_currency="USD",
                    ),
                    mapping=ProviderMapping(
                        instrument_id=instrument_id,
                        provider="synthetic-eod",
                        provider_symbol="SYNX.XNAS",
                        provider_exchange_code="NAS",
                        mic="XNAS",
                        quote_currency="USD",
                        mapping_version=1,
                        valid_from=now,
                        resolved_at=now,
                        resolution_source_url=(
                            "https://resolver.example.test/v3/mapping"
                        ),
                        resolution_status=ResolutionStatus.SUPPORTED,
                    ),
                ),
            ),
        )


def _seed_observed_snapshot(connection, owner_id: uuid.UUID) -> None:
    account_id = uuid.uuid4()
    event_ids = [uuid.uuid4(), uuid.uuid4()]
    source_ids = [ISIN, "BROKER-SYMBOL"]
    connection.execute(
        "INSERT INTO public.financial_accounts (id, user_id) VALUES (%s, %s)",
        (account_id, owner_id),
    )
    for event_id, source_id, position in zip(
        event_ids, source_ids, (1, 2), strict=True
    ):
        connection.execute(
            """
            INSERT INTO public.financial_instruments (user_id, instrument_id)
            VALUES (%s, %s)
            """,
            (owner_id, source_id),
        )
        connection.execute(
            """
            INSERT INTO public.financial_events (
                id, user_id, account_id, source_provider,
                source_event_reference, event_type, occurred_at
            ) VALUES (
                %s, %s, %s, 'synthetic-broker', %s,
                'observed_position_movement', now()
            )
            """,
            (event_id, owner_id, account_id, f"observed-{position}"),
        )
        connection.execute(
            """
            INSERT INTO public.financial_event_legs (
                event_id, user_id, account_id, position, leg_kind, direction,
                instrument_id, quantity
            ) VALUES (%s, %s, %s, 1, 'instrument', 'in', %s, %s)
            """,
            (event_id, owner_id, account_id, source_id, f"{position}.500"),
        )
    content = {
        "positions": {
            "owner": [
                {
                    "instrument_id": source_id,
                    "quantity": f"{position}.500",
                    "evidence_event_ids": [str(event_id)],
                }
                for event_id, source_id, position in zip(
                    event_ids, source_ids, (1, 2), strict=True
                )
            ]
        }
    }
    connection.execute(
        """
        INSERT INTO public.financial_snapshots (
            user_id, input_fingerprint, input_watermark, input_counts, content
        ) VALUES (%s, %s, '{}', '{}', %s)
        """,
        (
            owner_id,
            uuid.uuid4().hex + uuid.uuid4().hex,
            psycopg.types.json.Jsonb(content),
        ),
    )


def test_trusted_watchlist_workflow_preserves_source_identity_and_owner_scope(
    database_url: str,
) -> None:
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    resolver = SyntheticResolver()
    gateway = TrustedMarketWatchlistGateway(
        Settings(database_url=database_url), resolver
    )
    owner = AuthenticatedUser(id=str(owner_id), email=None)
    other = AuthenticatedUser(id=str(other_id), email=None)
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(connection, owner_id)
            _insert_auth_user(connection, other_id)
            _seed_observed_snapshot(connection, owner_id)

        before = asyncio.run(gateway.list_portfolio_candidates(owner))
        assert [
            (item["source_instrument_id"], item["coverage_status"]) for item in before
        ] == [
            (ISIN, "unresolved"),
            ("BROKER-SYMBOL", "unsupported_source_identity"),
        ]
        assert {item["source_kind"] for item in before} == {"observed"}

        added = asyncio.run(gateway.add(owner, ISIN))
        duplicate = asyncio.run(gateway.add(owner, ISIN))
        assert added.status == "added"
        assert duplicate.status == "duplicate"
        assert resolver.calls == 1
        assert len(asyncio.run(gateway.list_entries(owner))) == 1
        assert asyncio.run(gateway.list_entries(other)) == []

        after = asyncio.run(gateway.list_portfolio_candidates(owner))
        assert after[0]["coverage_status"] == "supported"
        assert after[0]["instrument"]["isin"] == ISIN
        assert after[1]["action"] == "Supply a validated ISIN; PIA will not infer one."

        entry_id = added.entry["id"]
        assert not asyncio.run(gateway.remove(other, entry_id))
        assert asyncio.run(gateway.remove(owner, entry_id))
        assert asyncio.run(gateway.list_entries(owner)) == []
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (owner_id, other_id),
            )


def test_watchlist_rls_denies_anonymous_cross_owner_and_client_writes(
    database_url: str,
) -> None:
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(connection, owner_id)
            _insert_auth_user(connection, other_id)
            instrument_id = connection.execute(
                """
                INSERT INTO public.market_instruments (
                    user_id, isin, share_class_figi, instrument_kind, display_name,
                    resolution_status, resolution_source_url, resolved_at
                ) VALUES (
                    %s, %s, 'BBG000000001', 'common_stock', 'Synthetic Equity',
                    'supported', 'https://resolver.example.test/v3/mapping', now()
                ) RETURNING id
                """,
                (owner_id, ISIN),
            ).fetchone()[0]
            entry_id = connection.execute(
                """
                INSERT INTO public.market_watchlist_entries (user_id, instrument_id)
                VALUES (%s, %s) RETURNING id
                """,
                (owner_id, instrument_id),
            ).fetchone()[0]

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, owner_id)
                assert connection.execute(
                    "SELECT id FROM public.market_watchlist_entries"
                ).fetchall() == [(entry_id,)]
                for statement in (
                    "DELETE FROM public.market_watchlist_entries",
                    "INSERT INTO public.market_watchlist_entries "
                    "(user_id, instrument_id) VALUES (auth.uid(), gen_random_uuid())",
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with connection.transaction():
                            connection.execute(statement)

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, other_id)
                assert (
                    connection.execute(
                        "SELECT id FROM public.market_watchlist_entries"
                    ).fetchall()
                    == []
                )

        with psycopg.connect(database_url) as connection:
            connection.execute("SET LOCAL ROLE anon")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT id FROM public.market_watchlist_entries")
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (owner_id, other_id),
            )


def test_watchlist_migration_round_trip_preserves_market_identity(
    database_url: str,
) -> None:
    api_dir = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "run", "alembic", "downgrade", "20260823_11"],
        cwd=api_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT to_regclass('public.market_watchlist_entries')"
                ).fetchone()[0]
                is None
            )
            assert (
                connection.execute(
                    "SELECT to_regclass('public.market_instruments')"
                ).fetchone()[0]
                == "market_instruments"
            )
    finally:
        subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=api_dir,
            check=True,
            capture_output=True,
            text=True,
        )
