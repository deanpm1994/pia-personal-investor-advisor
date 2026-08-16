import asyncio
from dataclasses import dataclass

from fastapi.testclient import TestClient

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.main import create_app
from pia_api.services.staged_imports import (
    StagedImportConfirmationError,
    StagedImportNotConfiguredError,
    SupabaseStagedImportGateway,
)


@dataclass
class Gateway:
    calls: list[tuple[str, str]]

    async def stage(self, user, filename, content_type, content):
        self.calls.append((user.id, filename))
        return {
            "id": "import-1",
            "status": "review_ready",
            "row_count": 1,
            "event_count": 1,
            "diagnostic_count": 0,
            "observed_event_count": 0,
            "confirmation_eligible": True,
            "rows": [],
        }

    async def review(self, user, import_id):
        if import_id != "import-1" or user.id != "owner":
            return None
        return {
            "id": import_id,
            "status": "blocked",
            "row_count": 1,
            "event_count": 0,
            "diagnostic_count": 1,
            "observed_event_count": 0,
            "confirmation_eligible": False,
            "diagnostics": [
                {
                    "code": "TRCSV018_INVALID_ENCODING",
                    "message": "CSV file must be valid UTF-8",
                }
            ],
            "rows": [
                {
                    "row_number": 2,
                    "events": [],
                    "diagnostics": [
                        {"code": "TRCSV007_INVALID_DECIMAL", "message": "invalid"}
                    ],
                }
            ],
        }

    async def confirm(self, user, import_id):
        if import_id != "import-1" or user.id != "owner":
            return None
        return {
            "id": import_id,
            "status": "confirmed",
            "row_count": 1,
            "event_count": 1,
            "diagnostic_count": 0,
            "observed_event_count": 0,
            "confirmation_eligible": False,
            "rows": [],
        }


class Verifier:
    async def verify(self, token):
        if token not in {"owner-token", "other-token"}:
            from jwt import InvalidTokenError

            raise InvalidTokenError("bad")
        user_id = "owner" if token == "owner-token" else "other"
        return AuthenticatedUser(id=user_id, email=f"{user_id}@example.test")


class UnavailableGateway:
    async def stage(self, _user, _filename, _content_type, _content):
        raise StagedImportNotConfiguredError(
            "Supabase import staging is not configured"
        )

    async def review(self, _user, _import_id):
        return None

    async def confirm(self, _user, _import_id):
        raise StagedImportNotConfiguredError(
            "Supabase import staging is not configured"
        )


class ConflictGateway(Gateway):
    async def confirm(self, _user, _import_id):
        raise StagedImportConfirmationError("not ready")


def test_import_routes_require_authentication_and_never_return_raw_rows():
    app = create_app()
    app.state.jwt_verifier = Verifier()
    app.state.import_gateway = Gateway([])
    client = TestClient(app)

    assert client.post("/v1/imports", content=b"csv").status_code == 401
    response = client.post(
        "/v1/imports",
        content=b"csv",
        headers={
            "Authorization": "Bearer owner-token",
            "X-Import-Filename": "history.csv",
            "Content-Type": "text/csv",
        },
    )
    assert response.status_code == 201
    assert response.json()["confirmation_eligible"] is True
    review = client.get(
        "/v1/imports/import-1", headers={"Authorization": "Bearer owner-token"}
    )
    assert review.status_code == 200
    assert review.json()["status"] == "blocked"
    assert review.json()["confirmation_eligible"] is False
    assert review.json()["diagnostics"] == [
        {"code": "TRCSV018_INVALID_ENCODING", "message": "CSV file must be valid UTF-8"}
    ]
    assert "source_row" not in str(review.json())
    assert (
        client.get(
            "/v1/imports/import-1", headers={"Authorization": "Bearer other-token"}
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/v1/imports/other", headers={"Authorization": "Bearer owner-token"}
        ).status_code
        == 404
    )


def test_import_confirmation_requires_the_owner_and_returns_confirmed_review():
    app = create_app()
    app.state.jwt_verifier = Verifier()
    app.state.import_gateway = Gateway([])
    client = TestClient(app)

    assert client.post("/v1/imports/import-1/confirm").status_code == 401
    assert (
        client.post(
            "/v1/imports/import-1/confirm",
            headers={"Authorization": "Bearer other-token"},
        ).status_code
        == 404
    )

    response = client.post(
        "/v1/imports/import-1/confirm",
        headers={"Authorization": "Bearer owner-token"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert response.json()["confirmation_eligible"] is False


def test_import_confirmation_reports_a_non_confirmable_batch_without_server_error():
    app = create_app()
    app.state.jwt_verifier = Verifier()
    app.state.import_gateway = ConflictGateway([])
    client = TestClient(app)

    response = client.post(
        "/v1/imports/import-1/confirm",
        headers={"Authorization": "Bearer owner-token"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "not ready"


def test_import_routes_allow_the_configured_browser_origin():
    app = create_app(Settings(web_origin="http://localhost:3000"))
    client = TestClient(app)

    response = client.options(
        "/v1/imports",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization,content-type,x-import-filename"
            ),
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert (
        "x-import-filename" in response.headers["access-control-allow-headers"].lower()
    )

    blocked = client.options(
        "/v1/imports",
        headers={
            "Origin": "https://untrusted.example.test",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert blocked.status_code == 400
    assert "access-control-allow-origin" not in blocked.headers


def test_import_routes_report_unconfigured_staging_without_a_server_error():
    app = create_app()
    app.state.jwt_verifier = Verifier()
    app.state.import_gateway = UnavailableGateway()
    client = TestClient(app)

    response = client.post(
        "/v1/imports",
        content=b"csv",
        headers={
            "Authorization": "Bearer owner-token",
            "X-Import-Filename": "history.csv",
            "Content-Type": "text/csv",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Import staging is unavailable"


def test_gateway_stages_invalid_encoding_as_a_safe_blocked_review(monkeypatch) -> None:
    class FakeTrustedWriter:
        calls: list[dict[str, object]] = []

        async def stage(self, **kwargs) -> None:
            self.calls.append(kwargs)

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        calls: list[tuple[str, str, object]] = []

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            payload = kwargs.get("json", kwargs.get("content"))
            self.calls.append(("POST", url, payload))
            if url.endswith("/staged_import_rows"):
                return Response([{"id": "row-2"}])
            return Response([])

        async def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs.get("params")))
            if url.endswith("/staged_imports"):
                return Response([{"id": "import-1"}])
            if url.endswith("/staged_import_rows"):
                return Response([])
            if url.endswith("/staged_import_validation_results"):
                return Response(
                    [
                        {
                            "staged_import_row_id": None,
                            "code": "TRCSV018_INVALID_ENCODING",
                            "message": "CSV file must be valid UTF-8",
                        }
                    ]
                )
            if url.endswith("/staged_import_state_events"):
                return Response([{"state": "blocked"}])
            raise AssertionError(f"Unexpected GET request: {url}")

    monkeypatch.setattr(
        "pia_api.services.staged_imports.httpx.AsyncClient", FakeAsyncClient
    )
    gateway = SupabaseStagedImportGateway(
        Settings(
            supabase_url="https://supabase.example.test", supabase_anon_key="anon"
        ),
        writer=FakeTrustedWriter(),
    )

    review = asyncio.run(
        gateway.stage(
            AuthenticatedUser(
                id="owner", email="owner@example.test", access_token="token"
            ),
            "history.csv",
            "text/csv",
            b"\xff",
        )
    )

    assert review == {
        "id": review["id"],
        "status": "blocked",
        "row_count": 0,
        "event_count": 0,
        "diagnostic_count": 1,
        "observed_event_count": 0,
        "confirmation_eligible": False,
        "diagnostics": [
            {
                "code": "TRCSV018_INVALID_ENCODING",
                "message": "CSV file must be valid UTF-8",
            }
        ],
        "rows": [],
    }
    assert FakeTrustedWriter.calls[0]["batch"].confirmation_eligible is False
    assert "source_row" not in str(review)
