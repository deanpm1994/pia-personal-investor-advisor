"""Trusted explicit refresh for immutable financial accounting snapshots."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.accounting import LedgerEvent
from pia_api.domain.financial_events import (
    CashLeg,
    FinancialEvent,
    InstrumentLeg,
    Money,
    Quantity,
    SourceIdentity,
    SourceReportedEurEvidence,
)
from pia_api.domain.financial_snapshots import (
    SnapshotAccount,
    build_snapshot_material,
)


class SnapshotRefreshError(RuntimeError):
    """Raised when a complete, auditable snapshot cannot be persisted."""


@dataclass(frozen=True)
class SnapshotRefreshResult:
    """Internal refresh outcome; HTTP response design belongs to P5.8."""

    snapshot_id: str
    fingerprint: str
    reused: bool


class TrustedSnapshotGateway:
    """Read immutable owner facts and atomically persist one completed snapshot."""

    def __init__(self, settings: Settings) -> None:
        self._database_url = settings.database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )

    async def refresh(self, user: AuthenticatedUser) -> SnapshotRefreshResult:
        """Perform an authenticated, explicit refresh with no partial completion."""
        return await asyncio.to_thread(self._refresh, user.id)

    def _refresh(self, user_id: str) -> SnapshotRefreshResult:
        try:
            owner_id = UUID(user_id)
        except (TypeError, ValueError) as error:
            raise SnapshotRefreshError(
                "authenticated user id must be a UUID"
            ) from error
        try:
            with psycopg.connect(
                self._database_url, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                    )
                    accounts, ledger_events = self._read_inputs(connection, owner_id)
                    material = build_snapshot_material(
                        accounts, ledger_events, owner_id=owner_id
                    )
                    inserted = connection.execute(
                        """
                        INSERT INTO public.financial_snapshots (
                            user_id, input_fingerprint, as_of, input_watermark,
                            input_counts, content
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (user_id, input_fingerprint) DO NOTHING
                        RETURNING id::text
                        """,
                        (
                            owner_id,
                            material.fingerprint,
                            material.as_of,
                            Jsonb(material.input_watermark),
                            Jsonb(material.input_counts),
                            Jsonb(material.content),
                        ),
                    ).fetchone()
                    if inserted is not None:
                        snapshot_id = inserted["id"]
                        self._audit_refresh(connection, owner_id, snapshot_id)
                        return SnapshotRefreshResult(
                            snapshot_id=snapshot_id,
                            fingerprint=material.fingerprint,
                            reused=False,
                        )
                    existing = connection.execute(
                        """
                        SELECT id::text
                        FROM public.financial_snapshots
                        WHERE user_id = %s AND input_fingerprint = %s
                        """,
                        (owner_id, material.fingerprint),
                    ).fetchone()
                    if existing is None:
                        raise SnapshotRefreshError("snapshot idempotency lookup failed")
                    return SnapshotRefreshResult(
                        snapshot_id=existing["id"],
                        fingerprint=material.fingerprint,
                        reused=True,
                    )
        except SnapshotRefreshError:
            raise
        except (psycopg.Error, TypeError, ValueError) as error:
            raise SnapshotRefreshError("snapshot refresh failed atomically") from error

    @staticmethod
    def _read_inputs(
        connection: psycopg.Connection[Any], owner_id: UUID
    ) -> tuple[tuple[SnapshotAccount, ...], tuple[LedgerEvent, ...]]:
        account_rows = connection.execute(
            """
            SELECT id, user_id, name, role, archived_at,
                   emergency_reserve_target_eur, updated_at
            FROM public.financial_accounts
            WHERE user_id = %s
            ORDER BY id
            """,
            (owner_id,),
        ).fetchall()
        accounts = tuple(
            SnapshotAccount(
                account_id=row["id"],
                owner_id=row["user_id"],
                name=row["name"],
                role=row["role"],
                archived_at=row["archived_at"],
                emergency_reserve_target_eur=row["emergency_reserve_target_eur"],
                updated_at=row["updated_at"],
            )
            for row in account_rows
        )
        event_rows = connection.execute(
            """
            SELECT id, user_id, account_id, source_provider,
                   source_event_reference, event_type, occurred_at,
                   source_reported_eur_amount, source_reported_eur_rate,
                   source_reported_eur_reported_at, correction_of_event_id,
                   reversal_of_event_id, source_group_reference, created_at
            FROM public.financial_events
            WHERE user_id = %s
            ORDER BY occurred_at, created_at, id
            """,
            (owner_id,),
        ).fetchall()
        leg_rows = connection.execute(
            """
            SELECT event_id, position, leg_kind, direction, cash_amount,
                   cash_currency, instrument_id, quantity
            FROM public.financial_event_legs
            WHERE user_id = %s
            ORDER BY event_id, position
            """,
            (owner_id,),
        ).fetchall()
        legs_by_event: dict[UUID, list[Any]] = {}
        for leg in leg_rows:
            legs_by_event.setdefault(leg["event_id"], []).append(leg)
        ledger_events = tuple(
            LedgerEvent(
                event_id=row["id"],
                created_at=row["created_at"],
                source_group_reference=row["source_group_reference"],
                event=FinancialEvent(
                    owner_id=row["user_id"],
                    account_id=row["account_id"],
                    source_identity=SourceIdentity(
                        provider=row["source_provider"],
                        event_reference=row["source_event_reference"],
                    ),
                    event_type=row["event_type"],
                    occurred_at=row["occurred_at"],
                    legs=tuple(
                        TrustedSnapshotGateway._leg_from_row(leg)
                        for leg in legs_by_event.get(row["id"], [])
                    ),
                    source_reported_eur=(
                        SourceReportedEurEvidence(
                            eur_amount=Money(
                                amount=row["source_reported_eur_amount"],
                                currency="EUR",
                            ),
                            source_rate=row["source_reported_eur_rate"],
                            reported_at=row["source_reported_eur_reported_at"],
                        )
                        if row["source_reported_eur_amount"] is not None
                        else None
                    ),
                    source_group_reference=row["source_group_reference"],
                    correction_of_event_id=row["correction_of_event_id"],
                    reversal_of_event_id=row["reversal_of_event_id"],
                ),
            )
            for row in event_rows
        )
        return accounts, ledger_events

    @staticmethod
    def _leg_from_row(row: dict[str, Any]) -> CashLeg | InstrumentLeg:
        if row["leg_kind"] == "cash":
            return CashLeg(
                direction=row["direction"],
                money=Money(amount=row["cash_amount"], currency=row["cash_currency"]),
            )
        return InstrumentLeg(
            direction=row["direction"],
            instrument_id=row["instrument_id"],
            quantity=Quantity(value=row["quantity"]),
        )

    @staticmethod
    def _audit_refresh(
        connection: psycopg.Connection[Any], owner_id: UUID, snapshot_id: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO public.audit_events (actor_id, event_type, metadata)
            VALUES (%s, 'financial_snapshot.refreshed', %s)
            """,
            (owner_id, Jsonb({"snapshot_id": snapshot_id})),
        )
