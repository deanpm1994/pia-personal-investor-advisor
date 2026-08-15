"""Trusted server staging and owner-scoped review for imports."""

import asyncio
import hashlib
from collections import defaultdict
from uuid import uuid4

import httpx
import psycopg

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.providers.trade_republic_csv import parse_trade_republic_csv


class StagedImportNotConfiguredError(RuntimeError):
    """Raised when the API lacks the public Supabase gateway configuration."""


class StagedImportConfirmationError(RuntimeError):
    """Raised when a staged import cannot safely transition to confirmed."""


class TrustedStagedImportWriter:
    """Persist parser output through the API's server-only database connection."""

    def __init__(self, settings: Settings) -> None:
        self._database_url = settings.database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )

    async def stage(
        self,
        *,
        user_id: str,
        import_id: str,
        path: str,
        filename: str,
        content_type: str,
        content: bytes,
        batch: object,
    ) -> None:
        """Atomically persist one parser result inaccessible to browser roles."""
        await asyncio.to_thread(
            self._stage,
            user_id,
            import_id,
            path,
            filename,
            content_type,
            content,
            batch,
        )

    def _stage(
        self,
        user_id: str,
        import_id: str,
        path: str,
        filename: str,
        content_type: str,
        content: bytes,
        batch: object,
    ) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO public.staged_imports (
                        id, user_id, source_provider, source_format, trusted_staged_at
                    )
                    VALUES (%s, %s, 'trade-republic', %s, timezone('utc', now()))
                    """,
                    (import_id, user_id, batch.format_version),
                )
                connection.execute(
                    """
                    INSERT INTO public.staged_import_files (
                        user_id, staged_import_id, bucket_id, object_path, filename,
                        content_type, byte_size, sha256
                    )
                    VALUES (%s, %s, 'raw-imports', %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        import_id,
                        path,
                        filename,
                        content_type,
                        len(content),
                        hashlib.sha256(content).hexdigest(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO public.staged_import_state_events (
                        user_id, staged_import_id, position, state
                    )
                    VALUES (%s, %s, 1, 'staged')
                    """,
                    (user_id, import_id),
                )
                for row in batch.rows:
                    parsed_output = (
                        psycopg.types.json.Jsonb(
                            {
                                "candidates": [
                                    candidate.model_dump(mode="json")
                                    for candidate in row.candidates
                                ]
                            }
                        )
                        if row.candidates
                        else None
                    )
                    row_id = connection.execute(
                        """
                        INSERT INTO public.staged_import_rows (
                            user_id, staged_import_id, source_row_number, source_row,
                            parsed_output
                        )
                        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                        RETURNING id
                        """,
                        (
                            user_id,
                            import_id,
                            row.row_number,
                            psycopg.types.json.Jsonb(row.source_row),
                            parsed_output,
                        ),
                    ).fetchone()[0]
                    for diagnostic in row.diagnostics:
                        self._insert_diagnostic(
                            connection,
                            user_id,
                            import_id,
                            row_id,
                            diagnostic.code,
                            diagnostic.message,
                        )
                for diagnostic in batch.diagnostics:
                    self._insert_diagnostic(
                        connection,
                        user_id,
                        import_id,
                        None,
                        diagnostic.code,
                        diagnostic.message,
                    )
                for position, state in (
                    (2, "parsed"),
                    (3, "validated"),
                    (4, "review_ready" if batch.confirmation_eligible else "blocked"),
                ):
                    connection.execute(
                        """
                        INSERT INTO public.staged_import_state_events (
                            user_id, staged_import_id, position, state
                        )
                        VALUES (%s, %s, %s, %s)
                        """,
                        (user_id, import_id, position, state),
                    )

    @staticmethod
    def _insert_diagnostic(
        connection: psycopg.Connection,
        user_id: str,
        import_id: str,
        row_id: object | None,
        code: str,
        message: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO public.staged_import_validation_results (
                user_id, staged_import_id, staged_import_row_id, code, severity,
                message
            )
            VALUES (%s, %s, %s, %s, 'error', %s)
            """,
            (user_id, import_id, row_id, code, message),
        )


class SupabaseStagedImportGateway:
    def __init__(
        self, settings: Settings, writer: TrustedStagedImportWriter | None = None
    ) -> None:
        self._settings = settings
        self._writer = writer or TrustedStagedImportWriter(settings)

    def _headers(self, user: AuthenticatedUser) -> dict[str, str]:
        if not self._settings.supabase_anon_key or not user.access_token:
            raise StagedImportNotConfiguredError(
                "Supabase import staging is not configured"
            )
        return {
            "apikey": self._settings.supabase_anon_key,
            "Authorization": f"Bearer {user.access_token}",
            "Prefer": "return=representation",
        }

    async def stage(
        self, user: AuthenticatedUser, filename: str, content_type: str, content: bytes
    ) -> dict[str, object]:
        headers = self._headers(user)
        import_id, path = str(uuid4()), f"{user.id}/{uuid4()}.csv"
        batch = parse_trade_republic_csv(content)
        async with httpx.AsyncClient(timeout=10.0) as client:
            upload = await client.post(
                f"{self._settings.supabase_url}/storage/v1/object/raw-imports/{path}",
                content=content,
                headers={**headers, "Content-Type": content_type},
            )
            upload.raise_for_status()
            try:
                await self._writer.stage(
                    user_id=user.id,
                    import_id=import_id,
                    path=path,
                    filename=filename,
                    content_type=content_type,
                    content=content,
                    batch=batch,
                )
            except Exception:
                await client.delete(
                    f"{self._settings.supabase_url}/storage/v1/object/raw-imports/{path}",
                    headers=headers,
                )
                raise
        return await self.review(user, import_id) or {}

    async def review(
        self, user: AuthenticatedUser, import_id: str
    ) -> dict[str, object] | None:
        headers = self._headers(user)
        base = self._settings.supabase_url.rstrip("/") + "/rest/v1"
        async with httpx.AsyncClient(timeout=10.0) as client:
            imports = await client.get(
                f"{base}/staged_imports",
                params={"id": f"eq.{import_id}", "select": "id,trusted_staged_at"},
                headers=headers,
            )
            imports.raise_for_status()
            if not imports.json():
                return None
            rows = await client.get(
                f"{base}/staged_import_rows",
                params={
                    "staged_import_id": f"eq.{import_id}",
                    "select": "id,source_row_number,parsed_output",
                    "order": "source_row_number",
                },
                headers=headers,
            )
            rows.raise_for_status()
            diagnostics = await client.get(
                f"{base}/staged_import_validation_results",
                params={
                    "staged_import_id": f"eq.{import_id}",
                    "select": "staged_import_row_id,code,message",
                },
                headers=headers,
            )
            diagnostics.raise_for_status()
            states = await client.get(
                f"{base}/staged_import_state_events",
                params={
                    "staged_import_id": f"eq.{import_id}",
                    "select": "state",
                    "order": "position.desc",
                    "limit": "1",
                },
                headers=headers,
            )
            states.raise_for_status()
        by_row: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
        batch_diagnostics: list[dict[str, str]] = []
        for item in diagnostics.json():
            diagnostic = {"code": item["code"], "message": item["message"]}
            row_id = item.get("staged_import_row_id")
            if row_id is None:
                batch_diagnostics.append(diagnostic)
            else:
                by_row[row_id].append(diagnostic)
        review_rows = [
            {
                "row_number": row["source_row_number"],
                "events": (row.get("parsed_output") or {}).get("candidates", []),
                "diagnostics": by_row[row["id"]],
            }
            for row in rows.json()
        ]
        status = states.json()[0]["state"] if states.json() else "staged"
        trusted = imports.json()[0].get("trusted_staged_at") is not None
        return {
            "id": import_id,
            "status": status,
            "row_count": len(review_rows),
            "event_count": sum(len(row["events"]) for row in review_rows),
            "diagnostic_count": len(diagnostics.json()),
            "observed_event_count": sum(
                event.get("event_type")
                in {"observed_cash_movement", "observed_position_movement"}
                for row in review_rows
                for event in row["events"]
            ),
            "confirmation_eligible": trusted and status == "review_ready",
            "diagnostics": batch_diagnostics,
            "rows": review_rows,
        }

    async def confirm(
        self, user: AuthenticatedUser, import_id: str
    ) -> dict[str, object] | None:
        """Run the database-owned confirmation transaction for one owner import."""
        headers = self._headers(user)
        base = self._settings.supabase_url.rstrip("/") + "/rest/v1"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{base}/rpc/confirm_staged_import",
                json={"p_staged_import_id": import_id},
                headers=headers,
            )
            if response.status_code >= 400:
                detail = response.json()
                code = detail.get("code") if isinstance(detail, dict) else None
                if code == "P0002":
                    return None
                if code == "P0001":
                    raise StagedImportConfirmationError(
                        "This import cannot be confirmed in its current state"
                    )
                response.raise_for_status()
        return await self.review(user, import_id)
