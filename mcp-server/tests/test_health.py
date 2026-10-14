from fastapi.testclient import TestClient

from app import __version__
from app.main import create_app
from proxylead_contracts import HealthResponse

from .conftest import make_settings


def test_health_returns_contract_shape(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = HealthResponse.model_validate(response.json())
    assert (body.service, body.version, body.environment) == (
        "proxylead-mcp-server",
        __version__,
        "test",
    )


def test_health_is_public_even_without_credentials() -> None:
    settings = make_settings(toolserver_bearer_memory=None, toolserver_bearer_workspace=None)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
