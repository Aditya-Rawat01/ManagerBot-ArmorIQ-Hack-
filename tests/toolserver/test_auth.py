import pytest
from fastapi.testclient import TestClient

from services.contracts import ErrorResponse
from services.toolserver.main import create_app

from .conftest import INITIALIZE, MCP_HEADERS, MEMORY_TOKEN, WORKSPACE_TOKEN, auth, make_settings


@pytest.mark.parametrize(
    "headers",
    [
        MCP_HEADERS,  # no Authorization header
        auth("wrong-token"),
        {**MCP_HEADERS, "Authorization": f"Basic {MEMORY_TOKEN}"},  # wrong scheme
        auth(WORKSPACE_TOKEN),  # another endpoint's credential
    ],
    ids=["missing", "wrong-token", "wrong-scheme", "other-endpoint-token"],
)
def test_memory_endpoint_rejects_bad_credentials(client: TestClient, headers: dict) -> None:
    response = client.post("/memory/mcp", json=INITIALIZE, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert ErrorResponse.model_validate(response.json()).error.code == "unauthorized"


def test_valid_credential_reaches_the_mcp_transport(client: TestClient) -> None:
    assert (
        client.post("/memory/mcp", json=INITIALIZE, headers=auth(MEMORY_TOKEN)).status_code == 200
    )
    assert (
        client.post("/workspace/mcp", json=INITIALIZE, headers=auth(WORKSPACE_TOKEN)).status_code
        == 200
    )


def test_unconfigured_endpoint_fails_closed() -> None:
    settings = make_settings(toolserver_bearer_workspace=None)
    with TestClient(create_app(settings)) as client:
        response = client.post("/workspace/mcp", json=INITIALIZE, headers=auth("anything"))
        assert response.status_code == 503
        assert ErrorResponse.model_validate(response.json()).error.code == "endpoint_not_configured"
        # The other endpoint keeps working with its own credential.
        assert (
            client.post("/memory/mcp", json=INITIALIZE, headers=auth(MEMORY_TOKEN)).status_code
            == 200
        )
