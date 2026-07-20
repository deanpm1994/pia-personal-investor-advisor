from dataclasses import dataclass

from fastapi.testclient import TestClient

from pia_api.core.auth import AuthenticatedUser
from pia_api.main import create_app


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
            "confirmation_eligible": True,
            "rows": [],
        }

    async def review(self, user, import_id):
        if import_id != "import-1":
            return None
        return {
            "id": import_id,
            "status": "blocked",
            "row_count": 1,
            "event_count": 0,
            "diagnostic_count": 1,
            "confirmation_eligible": False,
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


class Verifier:
    async def verify(self, token):
        if token != "owner-token":
            from jwt import InvalidTokenError

            raise InvalidTokenError("bad")
        return AuthenticatedUser(id="owner", email="owner@example.test")


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
    assert "source_row" not in str(review.json())
    assert (
        client.get(
            "/v1/imports/other", headers={"Authorization": "Bearer owner-token"}
        ).status_code
        == 404
    )
