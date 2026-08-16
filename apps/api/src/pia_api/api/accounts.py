"""Authenticated private-account metadata and manual ledger workflows."""

from decimal import Decimal
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from pia_api.core.auth import AuthenticatedUser, get_authenticated_user
from pia_api.domain.manual_accounts import (
    CashMovementCommand,
    CorrectionCommand,
    ManualAccountCreate,
    ManualAccountUpdate,
    TransferCommand,
)
from pia_api.services.manual_accounts import (
    ManualAccountConflictError,
    ManualAccountValidationError,
)

router = APIRouter()


class AccountResponse(BaseModel):
    id: str
    name: str
    role: str
    archived_at: str | None
    emergency_reserve_target_eur: Decimal | None


class ManualOperationResponse(BaseModel):
    event_ids: list[str]
    transfer_group_reference: str | None


class ManualAccountGateway(Protocol):
    async def list_accounts(
        self, user: AuthenticatedUser
    ) -> list[dict[str, object]]: ...

    async def create_account(
        self, user: AuthenticatedUser, command: ManualAccountCreate
    ) -> dict[str, object]: ...

    async def update_account(
        self, user: AuthenticatedUser, account_id: str, command: ManualAccountUpdate
    ) -> dict[str, object] | None: ...

    async def archive_account(
        self, user: AuthenticatedUser, account_id: str
    ) -> dict[str, object] | None: ...

    async def record_cash_movement(
        self,
        user: AuthenticatedUser,
        account_id: str,
        command: CashMovementCommand | CorrectionCommand,
        idempotency_key: str,
    ) -> dict[str, object] | None: ...

    async def record_transfer(
        self,
        user: AuthenticatedUser,
        command: TransferCommand,
        idempotency_key: str,
    ) -> dict[str, object] | None: ...


def _gateway(request: Request) -> ManualAccountGateway:
    gateway = getattr(request.app.state, "manual_account_gateway", None)
    if gateway is None:
        raise HTTPException(
            status_code=503, detail="Manual account workflows are unavailable"
        )
    return gateway


def _not_found(result: dict[str, object] | None) -> dict[str, object]:
    if result is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return result


def _operation(
    result: dict[str, object] | None,
) -> dict[str, object]:
    if result is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return result


async def _with_conflict_boundary(awaitable):
    try:
        return await awaitable
    except ManualAccountValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ManualAccountConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=200),
]


@router.get("/v1/accounts", response_model=list[AccountResponse])
async def list_accounts(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[ManualAccountGateway, Depends(_gateway)],
) -> list[dict[str, object]]:
    return await gateway.list_accounts(user)


@router.post(
    "/v1/accounts", response_model=AccountResponse, status_code=status.HTTP_201_CREATED
)
async def create_account(
    command: ManualAccountCreate,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[ManualAccountGateway, Depends(_gateway)],
) -> dict[str, object]:
    return await _with_conflict_boundary(gateway.create_account(user, command))


@router.patch("/v1/accounts/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str,
    command: ManualAccountUpdate,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[ManualAccountGateway, Depends(_gateway)],
) -> dict[str, object]:
    return _not_found(
        await _with_conflict_boundary(gateway.update_account(user, account_id, command))
    )


@router.post("/v1/accounts/{account_id}/archive", response_model=AccountResponse)
async def archive_account(
    account_id: str,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[ManualAccountGateway, Depends(_gateway)],
) -> dict[str, object]:
    return _not_found(
        await _with_conflict_boundary(gateway.archive_account(user, account_id))
    )


async def _record_movement(
    account_id: str,
    command: CashMovementCommand,
    kind: str,
    user: AuthenticatedUser,
    gateway: ManualAccountGateway,
    idempotency_key: str,
) -> dict[str, object]:
    command = command.model_copy(update={"kind": kind})
    return _operation(
        await _with_conflict_boundary(
            gateway.record_cash_movement(user, account_id, command, idempotency_key)
        )
    )


@router.post(
    "/v1/accounts/{account_id}/opening-balance",
    response_model=ManualOperationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_opening_balance(
    account_id: str,
    command: CashMovementCommand,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[ManualAccountGateway, Depends(_gateway)],
    idempotency_key: IdempotencyKey,
) -> dict[str, object]:
    return await _record_movement(
        account_id, command, "opening_balance", user, gateway, idempotency_key
    )


@router.post(
    "/v1/accounts/{account_id}/deposits",
    response_model=ManualOperationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_deposit(
    account_id: str,
    command: CashMovementCommand,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[ManualAccountGateway, Depends(_gateway)],
    idempotency_key: IdempotencyKey,
) -> dict[str, object]:
    return await _record_movement(
        account_id, command, "deposit", user, gateway, idempotency_key
    )


@router.post(
    "/v1/accounts/{account_id}/withdrawals",
    response_model=ManualOperationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_withdrawal(
    account_id: str,
    command: CashMovementCommand,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[ManualAccountGateway, Depends(_gateway)],
    idempotency_key: IdempotencyKey,
) -> dict[str, object]:
    return await _record_movement(
        account_id, command, "withdrawal", user, gateway, idempotency_key
    )


@router.post(
    "/v1/accounts/{account_id}/corrections",
    response_model=ManualOperationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_correction(
    account_id: str,
    command: CorrectionCommand,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[ManualAccountGateway, Depends(_gateway)],
    idempotency_key: IdempotencyKey,
) -> dict[str, object]:
    return _operation(
        await _with_conflict_boundary(
            gateway.record_cash_movement(user, account_id, command, idempotency_key)
        )
    )


@router.post(
    "/v1/transfers",
    response_model=ManualOperationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_transfer(
    command: TransferCommand,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[ManualAccountGateway, Depends(_gateway)],
    idempotency_key: IdempotencyKey,
) -> dict[str, object]:
    return _operation(
        await _with_conflict_boundary(
            gateway.record_transfer(user, command, idempotency_key)
        )
    )
