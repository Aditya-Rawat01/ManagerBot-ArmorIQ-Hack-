import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never depend on a developer's shell environment or `.env` file."""
    field_names = {name.upper() for name in Settings.model_fields}
    for key in list(os.environ):
        if key.upper() in field_names:
            monkeypatch.delenv(key, raising=False)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"app_env": "test", "cors_allowed_origins": "http://localhost:5173"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
