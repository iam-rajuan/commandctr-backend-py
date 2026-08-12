from fastapi.testclient import TestClient

from app.main import app


def test_health_contract():
    response = TestClient(app).get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["message"] == "CommandCtr API is healthy"
    assert payload["data"]["service"] == "CommandCtr Backend"


def test_not_found_contract():
    response = TestClient(app).get("/api/v1/does-not-exist")
    assert response.status_code == 404
    payload = response.json()
    assert payload["success"] is False
    assert payload["message"] == "Route not found: GET /api/v1/does-not-exist"


def test_auth_required_contract():
    response = TestClient(app).get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["message"] == "Authorization token is required"
