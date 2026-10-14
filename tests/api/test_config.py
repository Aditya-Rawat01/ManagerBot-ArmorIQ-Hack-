import pytest
from pydantic import ValidationError

from apps.api.core.config import Settings

from .conftest import make_settings


def test_defaults_need_no_secrets() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_env == "development"
    assert settings.armoriq_api_key is None
    assert settings.slack_bot_token is None
    assert settings.armoriq_agent_id == "proxylead"
    assert (settings.armoriq_mcp_memory_name, settings.armoriq_mcp_workspace_name) == (
        "memory-mcp",
        "workspace-mcp",
    )


def test_csv_lists_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://a.test, https://b.test ,")
    monkeypatch.setenv("SLACK_ALLOWED_CHANNELS", "C1,C2")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.cors_allowed_origins == ["http://a.test", "https://b.test"]
    assert settings.slack_allowed_channels == ["C1", "C2"]


def test_wildcard_cors_origin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="explicit origins"):
        make_settings(cors_allowed_origins="*")


def test_token_must_outlive_approval_window() -> None:
    # PRD v2 §7.1: TOKEN_VALIDITY_S*1000 > APPROVAL_TIMEOUT_MS + 120000
    with pytest.raises(ValidationError, match="TOKEN_VALIDITY_S"):
        make_settings(token_validity_s=900, approval_timeout_ms=900_000)
    assert make_settings(token_validity_s=1200, approval_timeout_ms=900_000)


def test_secrets_are_not_exposed_in_repr_or_dump() -> None:
    settings = make_settings(armoriq_api_key="ak_test_super_secret", slack_bot_token="xoxb-secret")
    assert "ak_test_super_secret" not in repr(settings)
    assert "xoxb-secret" not in settings.model_dump_json()
    assert settings.armoriq_api_key is not None
    assert settings.armoriq_api_key.get_secret_value() == "ak_test_super_secret"
