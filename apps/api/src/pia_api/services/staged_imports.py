"""Supabase REST adapter for owner-scoped staged imports."""

import hashlib
from collections import defaultdict
from uuid import uuid4

import httpx

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.providers.trade_republic_csv import parse_trade_republic_csv


class SupabaseStagedImportGateway:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _headers(self, user: AuthenticatedUser) -> dict[str, str]:
        if not self._settings.supabase_anon_key or not user.access_token:
            raise RuntimeError("Supabase import staging is not configured")
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
                await self._post(
                    client,
                    headers,
                    "staged_imports",
                    {
                        "id": import_id,
                        "user_id": user.id,
                        "source_provider": "trade-republic",
                        "source_format": batch.format_version,
                    },
                )
                await self._post(
                    client,
                    headers,
                    "staged_import_files",
                    {
                        "user_id": user.id,
                        "staged_import_id": import_id,
                        "bucket_id": "raw-imports",
                        "object_path": path,
                        "filename": filename,
                        "content_type": content_type,
                        "byte_size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    },
                )
                await self._post(
                    client,
                    headers,
                    "staged_import_state_events",
                    {
                        "user_id": user.id,
                        "staged_import_id": import_id,
                        "position": 1,
                        "state": "staged",
                    },
                )
                row_ids: dict[int, str] = {}
                for row in batch.rows:
                    result = await self._post(
                        client,
                        headers,
                        "staged_import_rows",
                        {
                            "user_id": user.id,
                            "staged_import_id": import_id,
                            "source_row_number": row.row_number,
                            "source_row": row.source_row,
                            "parsed_output": {
                                "candidates": [
                                    candidate.model_dump(mode="json")
                                    for candidate in row.candidates
                                ]
                            }
                            if row.candidates
                            else None,
                        },
                    )
                    row_ids[row.row_number] = result[0]["id"]
                    for diagnostic in row.diagnostics:
                        await self._post(
                            client,
                            headers,
                            "staged_import_validation_results",
                            {
                                "user_id": user.id,
                                "staged_import_id": import_id,
                                "staged_import_row_id": row_ids[row.row_number],
                                "code": diagnostic.code,
                                "severity": "error",
                                "message": diagnostic.message,
                            },
                        )
                for diagnostic in batch.diagnostics:
                    await self._post(
                        client,
                        headers,
                        "staged_import_validation_results",
                        {
                            "user_id": user.id,
                            "staged_import_id": import_id,
                            "code": diagnostic.code,
                            "severity": "error",
                            "message": diagnostic.message,
                        },
                    )
                for position, state in (
                    (2, "parsed"),
                    (3, "validated"),
                    (4, "review_ready" if batch.confirmation_eligible else "blocked"),
                ):
                    await self._post(
                        client,
                        headers,
                        "staged_import_state_events",
                        {
                            "user_id": user.id,
                            "staged_import_id": import_id,
                            "position": position,
                            "state": state,
                        },
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
                params={"id": f"eq.{import_id}", "select": "id"},
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
        for item in diagnostics.json():
            by_row[item.get("staged_import_row_id")].append(
                {"code": item["code"], "message": item["message"]}
            )
        review_rows = [
            {
                "row_number": row["source_row_number"],
                "events": (row.get("parsed_output") or {}).get("candidates", []),
                "diagnostics": by_row[row["id"]],
            }
            for row in rows.json()
        ]
        status = states.json()[0]["state"] if states.json() else "staged"
        return {
            "id": import_id,
            "status": status,
            "row_count": len(review_rows),
            "event_count": sum(len(row["events"]) for row in review_rows),
            "diagnostic_count": len(diagnostics.json()),
            "confirmation_eligible": status == "review_ready",
            "rows": review_rows,
        }

    async def _post(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        table: str,
        payload: dict[str, object],
    ) -> list[dict[str, object]]:
        response = await client.post(
            f"{self._settings.supabase_url.rstrip('/')}/rest/v1/{table}",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()
