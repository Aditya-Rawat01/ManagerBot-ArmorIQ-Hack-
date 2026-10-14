import httpx
from fastapi.testclient import TestClient


def _preflight(client: TestClient, origin: str) -> httpx.Response:
    return client.options(
        "/api/v1/health",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


def test_configured_origin_is_allowed(client: TestClient) -> None:
    response = _preflight(client, "http://localhost:5173")
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert response.headers.get("X-Request-ID")


def test_unknown_origin_is_not_allowed(client: TestClient) -> None:
    response = _preflight(client, "https://evil.example.com")
    assert "access-control-allow-origin" not in response.headers
