"""Trusted persistence for append-only manual account and ledger workflows."""

import asyncio
import hashlib
import json
from datetime import UTC
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.manual_accounts import (
    AccountRole,
    CashMovementCommand,
    CorrectionCommand,
    ManualAccountCreate,
    ManualAccountUpdate,
    TransferCommand,
)


class ManualAccountConflictError(RuntimeError):
    """Raised when a valid request conflicts with immutable account history."""


class ManualAccountValidationError(RuntimeError):
    """Raised when a workflow cannot create a valid ledger fact."""


class TrustedManualAccountGateway:
    """Use the server-only database connection for trusted manual operations."""

    def __init__(self, settings: Settings) -> None:
        self._database_url = settings.database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )

    async def list_accounts(self, user: AuthenticatedUser) -> list[dict[str, object]]:
        return await asyncio.to_thread(self._list_accounts, user.id)

    def _list_accounts(self, user_id: str) -> list[dict[str, object]]:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """
                SELECT id::text, name, role, archived_at::text,
                       emergency_reserve_target_eur::text
                FROM public.financial_accounts
                WHERE user_id = %s
                ORDER BY created_at, id
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def create_account(
        self, user: AuthenticatedUser, command: ManualAccountCreate
    ) -> dict[str, object]:
        return await asyncio.to_thread(self._create_account, user.id, command)

    def _create_account(
        self, user_id: str, command: ManualAccountCreate
    ) -> dict[str, object]:
        try:
            with psycopg.connect(
                self._database_url, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        INSERT INTO public.financial_accounts (
                            user_id, name, role, emergency_reserve_target_eur
                        )
                        VALUES (%s, %s, %s, %s)
                        RETURNING id::text, name, role, archived_at::text,
                                  emergency_reserve_target_eur::text
                        """,
                        (
                            user_id,
                            command.name,
                            command.role.value,
                            command.emergency_reserve_target_eur,
                        ),
                    ).fetchone()
                    assert row is not None
                    self._audit(
                        connection,
                        user_id,
                        "manual_account.created",
                        {"account_id": row["id"], "role": row["role"]},
                    )
                    return dict(row)
        except psycopg.errors.UniqueViolation as error:
            raise ManualAccountConflictError(
                "an active brokerage account already exists"
            ) from error

    async def update_account(
        self, user: AuthenticatedUser, account_id: str, command: ManualAccountUpdate
    ) -> dict[str, object] | None:
        return await asyncio.to_thread(
            self._update_account, user.id, account_id, command
        )

    def _update_account(
        self, user_id: str, account_id: str, command: ManualAccountUpdate
    ) -> dict[str, object] | None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                account = self._account(connection, user_id, account_id)
                if account is None:
                    return None
                fields: list[str] = []
                values: list[object] = []
                if "name" in command.model_fields_set:
                    fields.append("name = %s")
                    values.append(command.name)
                if "emergency_reserve_target_eur" in command.model_fields_set:
                    if account["role"] != AccountRole.EMERGENCY_RESERVE.value:
                        raise ManualAccountValidationError(
                            "only emergency_reserve accounts may have a EUR target"
                        )
                    fields.append("emergency_reserve_target_eur = %s")
                    values.append(command.emergency_reserve_target_eur)
                values.extend([account["id"], user_id])
                row = connection.execute(
                    f"""
                    UPDATE public.financial_accounts
                    SET {", ".join(fields)}, updated_at = timezone('utc', now())
                    WHERE id = %s AND user_id = %s
                    RETURNING id::text, name, role, archived_at::text,
                              emergency_reserve_target_eur::text
                    """,
                    values,
                ).fetchone()
                assert row is not None
                self._audit(
                    connection,
                    user_id,
                    "manual_account.metadata_updated",
                    {"account_id": row["id"]},
                )
                return dict(row)

    async def archive_account(
        self, user: AuthenticatedUser, account_id: str
    ) -> dict[str, object] | None:
        return await asyncio.to_thread(self._archive_account, user.id, account_id)

    def _archive_account(
        self, user_id: str, account_id: str
    ) -> dict[str, object] | None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                account = self._account(connection, user_id, account_id)
                if account is None:
                    return None
                if account["archived_at"] is None:
                    row = connection.execute(
                        """
                        UPDATE public.financial_accounts
                        SET archived_at = timezone('utc', now()),
                            updated_at = timezone('utc', now())
                        WHERE id = %s AND user_id = %s
                        RETURNING id::text, name, role, archived_at::text,
                                  emergency_reserve_target_eur::text
                        """,
                        (account["id"], user_id),
                    ).fetchone()
                    assert row is not None
                    self._audit(
                        connection,
                        user_id,
                        "manual_account.archived",
                        {"account_id": row["id"]},
                    )
                    return dict(row)
                return self._account_response(account)

    async def record_cash_movement(
        self,
        user: AuthenticatedUser,
        account_id: str,
        command: CashMovementCommand | CorrectionCommand,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        return await asyncio.to_thread(
            self._record_cash_movement, user.id, account_id, command, idempotency_key
        )

    def _record_cash_movement(
        self,
        user_id: str,
        account_id: str,
        command: CashMovementCommand | CorrectionCommand,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        operation_kind = (
            command.mode if isinstance(command, CorrectionCommand) else command.kind
        )
        payload = self._movement_payload(account_id, command, operation_kind)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                operation_id, existing = self._reserve_idempotency(
                    connection, user_id, idempotency_key, operation_kind, payload
                )
                if existing is not None:
                    return existing
                account = self._active_manual_account(connection, user_id, account_id)
                if account is None:
                    return None
                event_type, direction, correction_of, reversal_of = (
                    self._movement_shape(connection, user_id, account, command)
                )
                if command.kind == "opening_balance":
                    opening_exists = connection.execute(
                        """
                        SELECT 1 FROM public.financial_events
                        WHERE user_id = %s AND account_id = %s
                            AND source_provider = 'manual'
                        LIMIT 1
                        """,
                        (user_id, account["id"]),
                    ).fetchone()
                    if opening_exists is not None:
                        raise ManualAccountConflictError(
                            "a manual opening balance already exists for this account"
                        )
                event_id = self._insert_cash_event(
                    connection,
                    user_id=user_id,
                    account_id=account["id"],
                    source_reference=f"manual:{operation_id}:{operation_kind}",
                    event_type=event_type,
                    occurred_at=command.occurred_at,
                    amount=command.amount,
                    currency=command.currency,
                    direction=direction,
                    correction_of_event_id=correction_of,
                    reversal_of_event_id=reversal_of,
                )
                result = {"event_ids": [event_id], "transfer_group_reference": None}
                self._complete_idempotency(connection, user_id, idempotency_key, result)
                self._audit(
                    connection,
                    user_id,
                    f"manual_ledger.{operation_kind}",
                    {"account_id": str(account["id"]), "event_id": event_id},
                )
                return result

    async def record_transfer(
        self, user: AuthenticatedUser, command: TransferCommand, idempotency_key: str
    ) -> dict[str, object] | None:
        return await asyncio.to_thread(
            self._record_transfer, user.id, command, idempotency_key
        )

    def _record_transfer(
        self, user_id: str, command: TransferCommand, idempotency_key: str
    ) -> dict[str, object] | None:
        payload = self._transfer_payload(command)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                operation_id, existing = self._reserve_idempotency(
                    connection, user_id, idempotency_key, "transfer", payload
                )
                if existing is not None:
                    return existing
                source = self._active_manual_account(
                    connection, user_id, command.from_account_id
                )
                destination = self._active_manual_account(
                    connection, user_id, command.to_account_id
                )
                if source is None or destination is None:
                    return None
                group_reference = f"manual-transfer:{operation_id}"
                outbound_id = self._insert_cash_event(
                    connection,
                    user_id=user_id,
                    account_id=source["id"],
                    source_reference=f"manual:{operation_id}:transfer:out",
                    event_type="withdrawal",
                    occurred_at=command.occurred_at,
                    amount=command.amount,
                    currency=command.currency,
                    direction="out",
                    source_group_reference=group_reference,
                )
                inbound_id = self._insert_cash_event(
                    connection,
                    user_id=user_id,
                    account_id=destination["id"],
                    source_reference=f"manual:{operation_id}:transfer:in",
                    event_type="deposit",
                    occurred_at=command.occurred_at,
                    amount=command.amount,
                    currency=command.currency,
                    direction="in",
                    source_group_reference=group_reference,
                )
                result = {
                    "event_ids": [outbound_id, inbound_id],
                    "transfer_group_reference": group_reference,
                }
                self._complete_idempotency(connection, user_id, idempotency_key, result)
                self._audit(
                    connection,
                    user_id,
                    "manual_ledger.transfer",
                    {
                        "source_account_id": str(source["id"]),
                        "destination_account_id": str(destination["id"]),
                        "event_ids": [outbound_id, inbound_id],
                    },
                )
                return result

    @staticmethod
    def _account(
        connection: psycopg.Connection[Any], user_id: str, account_id: str
    ) -> dict[str, Any] | None:
        return connection.execute(
            """
            SELECT id, user_id, name, role, archived_at
            FROM public.financial_accounts
            WHERE user_id = %s AND id::text = %s
            """,
            (user_id, account_id),
        ).fetchone()

    def _active_manual_account(
        self, connection: psycopg.Connection[Any], user_id: str, account_id: str
    ) -> dict[str, Any] | None:
        account = self._account(connection, user_id, account_id)
        if account is None:
            return None
        if account["archived_at"] is not None:
            raise ManualAccountConflictError(
                "archived accounts cannot accept new activity"
            )
        if account["role"] == AccountRole.BROKERAGE.value:
            raise ManualAccountValidationError(
                "manual workflows are limited to cash, savings, and emergency reserves"
            )
        return account

    @staticmethod
    def _account_response(account: dict[str, Any]) -> dict[str, object]:
        return {
            "id": str(account["id"]),
            "name": account["name"],
            "role": account["role"],
            "archived_at": str(account["archived_at"])
            if account["archived_at"]
            else None,
            "emergency_reserve_target_eur": (
                str(account["emergency_reserve_target_eur"])
                if account.get("emergency_reserve_target_eur") is not None
                else None
            ),
        }

    @staticmethod
    def _audit(
        connection: psycopg.Connection[Any],
        user_id: str,
        event_type: str,
        metadata: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO public.audit_events (actor_id, event_type, metadata)
            VALUES (%s, %s, %s)
            """,
            (user_id, event_type, Jsonb(metadata)),
        )

    @staticmethod
    def _complete_idempotency(
        connection: psycopg.Connection[Any],
        user_id: str,
        key: str,
        result: dict[str, object],
    ) -> None:
        connection.execute(
            """
            UPDATE public.manual_ledger_idempotency_keys
            SET result = %s
            WHERE user_id = %s AND idempotency_key = %s
            """,
            (Jsonb(result), user_id, key),
        )

    @staticmethod
    def _reserve_idempotency(
        connection: psycopg.Connection[Any],
        user_id: str,
        key: str,
        operation_kind: str,
        payload: dict[str, str],
    ) -> tuple[str, dict[str, object] | None]:
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        row = connection.execute(
            """
            INSERT INTO public.manual_ledger_idempotency_keys (
                user_id, idempotency_key, operation_kind, request_fingerprint
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
            RETURNING operation_id::text, operation_kind, request_fingerprint, result
            """,
            (user_id, key, operation_kind, fingerprint),
        ).fetchone()
        assert row is not None
        if (
            row["operation_kind"] != operation_kind
            or row["request_fingerprint"] != fingerprint
        ):
            raise ManualAccountConflictError(
                "Idempotency-Key was reused for another request"
            )
        if row["result"] is not None:
            return row["operation_id"], dict(row["result"])
        return row["operation_id"], None

    @staticmethod
    def _insert_cash_event(
        connection: psycopg.Connection[Any],
        *,
        user_id: str,
        account_id: object,
        source_reference: str,
        event_type: str,
        occurred_at: object,
        amount: Decimal,
        currency: str,
        direction: str,
        source_group_reference: str | None = None,
        correction_of_event_id: object | None = None,
        reversal_of_event_id: object | None = None,
    ) -> str:
        event = connection.execute(
            """
            INSERT INTO public.financial_events (
                user_id, account_id, source_provider, source_event_reference,
                source_group_reference, event_type, occurred_at,
                correction_of_event_id, reversal_of_event_id
            )
            VALUES (%s, %s, 'manual', %s, %s, %s, %s, %s, %s)
            RETURNING id::text
            """,
            (
                user_id,
                account_id,
                source_reference,
                source_group_reference,
                event_type,
                occurred_at,
                correction_of_event_id,
                reversal_of_event_id,
            ),
        ).fetchone()
        assert event is not None
        connection.execute(
            """
            INSERT INTO public.financial_event_legs (
                event_id, user_id, account_id, position, leg_kind, direction,
                cash_amount, cash_currency
            )
            VALUES (%s, %s, %s, 1, 'cash', %s, %s, %s)
            """,
            (event["id"], user_id, account_id, direction, amount, currency),
        )
        return event["id"]

    def _movement_shape(
        self,
        connection: psycopg.Connection[Any],
        user_id: str,
        account: dict[str, Any],
        command: CashMovementCommand | CorrectionCommand,
    ) -> tuple[str, str, object | None, object | None]:
        if not isinstance(command, CorrectionCommand):
            if command.kind in {"opening_balance", "deposit"}:
                return "deposit", "in", None, None
            if command.kind == "withdrawal":
                return "withdrawal", "out", None, None
            raise ManualAccountValidationError("unsupported manual movement")
        target = connection.execute(
            """
            SELECT events.id, events.event_type, legs.direction, legs.cash_amount,
                   legs.cash_currency
            FROM public.financial_events AS events
            JOIN public.financial_event_legs AS legs
                ON legs.event_id = events.id AND legs.position = 1
            WHERE events.id::text = %s AND events.user_id = %s
                AND events.account_id = %s AND events.source_provider = 'manual'
                AND events.event_type IN ('deposit', 'withdrawal')
            """,
            (command.target_event_id, user_id, account["id"]),
        ).fetchone()
        if target is None:
            raise ManualAccountValidationError("manual cash target event was not found")
        if command.mode == "reversal":
            if (
                command.amount != target["cash_amount"]
                or command.currency != target["cash_currency"]
            ):
                raise ManualAccountValidationError(
                    "a reversal must exactly negate its target cash fact"
                )
            direction = "out" if target["direction"] == "in" else "in"
            return "reversal", direction, None, target["id"]
        assert command.direction is not None
        return "correction", command.direction, target["id"], None

    @staticmethod
    def _timestamp(value: object) -> str:
        if hasattr(value, "astimezone"):
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return str(value)

    def _movement_payload(
        self,
        account_id: str,
        command: CashMovementCommand | CorrectionCommand,
        kind: str,
    ) -> dict[str, str]:
        payload = {
            "account_id": account_id,
            "amount": str(command.amount),
            "currency": command.currency,
            "kind": kind,
            "occurred_at": self._timestamp(command.occurred_at),
        }
        if isinstance(command, CorrectionCommand):
            payload.update(
                {
                    "direction": command.direction or "",
                    "target_event_id": command.target_event_id,
                }
            )
        return payload

    def _transfer_payload(self, command: TransferCommand) -> dict[str, str]:
        return {
            "amount": str(command.amount),
            "currency": command.currency,
            "from_account_id": command.from_account_id,
            "occurred_at": self._timestamp(command.occurred_at),
            "to_account_id": command.to_account_id,
        }
