import pytest
from fastapi.testclient import TestClient

from services.contracts import ErrorResponse, HealthResponse
from services.knowledge import embeddings
from services.toolserver import __version__
from services.toolserver.main import create_app

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


def test_health_is_503_until_the_embedding_model_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed startup warm is a readiness failure (Render keeps the old
    deploy, the Docker HEALTHCHECK goes unhealthy), never a silent 200."""

    def broken_warm() -> None:
        raise OSError("model cache unreadable and no network")

    monkeypatch.setattr(embeddings, "warm", broken_warm)
    app = create_app(make_settings())
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    error = ErrorResponse.model_validate(response.json()).error
    assert error.code == "not_ready"
    assert "cache" not in error.message  # no exception detail leaks


def test_health_is_503_before_startup() -> None:
    # Lifespan not run (no `with`): the model was never warmed.
    client = TestClient(create_app(make_settings()))
    assert client.get("/health").status_code == 503
