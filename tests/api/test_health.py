import re

import pytest
from fastapi.testclient import TestClient

from apps.api import __version__
from services.contracts import HealthResponse


@pytest.mark.parametrize("path", ["/health", "/api/v1/health"])
def test_health_returns_contract_shape(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    body = HealthResponse.model_validate(response.json())
    assert body.status == "ok"
    assert body.service == "proxylead-backend"
    assert body.version == __version__
    assert body.environment == "test"


def test_health_needs_no_external_credentials(client: TestClient) -> None:
    # The default test settings contain no ArmorIQ/Slack/Anthropic/DB secrets.
    assert client.get("/health").status_code == 200


def test_response_carries_generated_request_id(client: TestClient) -> None:
    response = client.get("/health")
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


def test_valid_incoming_request_id_is_propagated(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-1234_abc"})
    assert response.headers["X-Request-ID"] == "trace-1234_abc"


@pytest.mark.parametrize("bad", ["short", "has spaces in it", "x" * 65, "semi;colon-1234"])
def test_malformed_incoming_request_id_is_replaced(client: TestClient, bad: str) -> None:
    response = client.get("/health", headers={"X-Request-ID": bad})
    assert response.headers["X-Request-ID"] != bad
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])
