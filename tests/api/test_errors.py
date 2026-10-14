from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.core.errors import ApiError
from services.contracts import ErrorBody, ErrorResponse


@pytest.fixture
def client_with_probe_routes(app: FastAPI) -> Iterator[TestClient]:
    @app.get("/_probe/items/{item_id}")
    def typed(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @app.get("/_probe/api-error")
    def api_error() -> None:
        raise ApiError(409, "conflict_example", "Something conflicted.", details={"hint": "x"})

    @app.get("/_probe/crash")
    def crash() -> None:
        raise RuntimeError("secret internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _error(response: httpx.Response) -> ErrorBody:
    return ErrorResponse.model_validate(response.json()).error


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    error = _error(response)
    assert error.code == "not_found"
    assert error.request_id == response.headers["X-Request-ID"]


def test_validation_error_does_not_echo_input(client_with_probe_routes: TestClient) -> None:
    response = client_with_probe_routes.get("/_probe/items/not-a-number-SECRET")
    assert response.status_code == 422
    assert _error(response).code == "validation_error"
    assert "SECRET" not in response.text


def test_api_error_is_returned_verbatim(client_with_probe_routes: TestClient) -> None:
    response = client_with_probe_routes.get("/_probe/api-error")
    assert response.status_code == 409
    error = _error(response)
    assert (error.code, error.message, error.details) == (
        "conflict_example",
        "Something conflicted.",
        {"hint": "x"},
    )


def test_unhandled_exception_is_generic_500(client_with_probe_routes: TestClient) -> None:
    response = client_with_probe_routes.get("/_probe/crash")
    assert response.status_code == 500
    error = _error(response)
    assert error.code == "internal_error"
    assert "secret internal detail" not in response.text
    assert error.request_id == response.headers["X-Request-ID"]
