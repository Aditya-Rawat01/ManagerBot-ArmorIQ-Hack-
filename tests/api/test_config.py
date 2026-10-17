from pathlib import Path

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


def test_armoriq_observability_is_off_unless_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).armoriq_observability is False  # type: ignore[call-arg]
    monkeypatch.setenv("ARMORIQ_OBSERVABILITY", "")
    assert Settings(_env_file=None).armoriq_observability is False  # type: ignore[call-arg]
    monkeypatch.setenv("ARMORIQ_OBSERVABILITY", "false")
    assert Settings(_env_file=None).armoriq_observability is False  # type: ignore[call-arg]
    monkeypatch.setenv("ARMORIQ_OBSERVABILITY", "true")
    assert Settings(_env_file=None).armoriq_observability is True  # type: ignore[call-arg]


def test_wildcard_cors_origin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="explicit origins"):
        make_settings(cors_allowed_origins="*")


def test_token_must_outlive_approval_window() -> None:
    # PRD v2 §7.1: TOKEN_VALIDITY_S*1000 > APPROVAL_TIMEOUT_MS + 120000
    with pytest.raises(ValidationError, match="TOKEN_VALIDITY_S"):
        make_settings(token_validity_s=900, approval_timeout_ms=900_000)
    assert make_settings(token_validity_s=1200, approval_timeout_ms=900_000)


# A blank variable means unset (env_ignore_empty): a Render dashboard entry
# such as `TOKEN_VALIDITY_S=` once crashed API startup.
_BLANK_MEANS_DEFAULT = {
    "TOKEN_VALIDITY_S": ("token_validity_s", 1200),
    "APPROVAL_TIMEOUT_MS": ("approval_timeout_ms", 900_000),
    "APP_ENV": ("app_env", "development"),
    "LOG_LEVEL": ("log_level", "INFO"),
    "ARMORIQ_MCP_MEMORY_NAME": ("armoriq_mcp_memory_name", "memory-mcp"),
    "ARMORIQ_MCP_WORKSPACE_NAME": ("armoriq_mcp_workspace_name", "workspace-mcp"),
    "ARMORIQ_AGENT_ID": ("armoriq_agent_id", "proxylead"),
    "ARMORIQ_CONSOLE_URL": ("armoriq_console_url", "https://platform.armoriq.ai"),
    "LLM_MODEL": ("llm_model", "gemini-2.5-flash"),
    "MIN_SCORE": ("min_score", None),
    "ARMORIQ_OBSERVABILITY": ("armoriq_observability", False),
    "SLACK_ALLOWED_CHANNELS": ("slack_allowed_channels", []),
    "SLACK_TEAM_ID": ("slack_team_id", None),
    "SLACK_USER_SAM_ID": ("slack_user_sam_id", None),
    "TOOLSERVER_BASE_URL": ("toolserver_base_url", None),
    # Blank CORS now means the local dev default, no longer an empty list.
    "CORS_ALLOWED_ORIGINS": ("cors_allowed_origins", ["http://localhost:5173"]),
}
_BLANK_SECRETS = {
    "GEMINI_API_KEY": "gemini_api_key",
    "ARMORIQ_API_KEY": "armoriq_api_key",
    "CTX_SECRET": "ctx_secret",
    "SESSION_SECRET": "session_secret",
    "DEMO_PASSCODE": "demo_passcode",
    "ADMIN_TOKEN": "admin_token",
    "DATABASE_URL_BACKEND": "database_url_backend",
    "PERSONA_MAYA_EMAIL": "persona_maya_email",
    "SLACK_BOT_TOKEN": "slack_bot_token",
    "SLACK_APP_TOKEN": "slack_app_token",
}


def _assert_blank_means_unset(settings: Settings) -> None:
    for name, (field, default) in _BLANK_MEANS_DEFAULT.items():
        assert getattr(settings, field) == default, name
    for name, field in _BLANK_SECRETS.items():
        assert getattr(settings, field) is None, name


def test_blank_environment_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_BLANK_MEANS_DEFAULT, *_BLANK_SECRETS):
        monkeypatch.setenv(name, "")
    _assert_blank_means_unset(Settings(_env_file=None))  # type: ignore[call-arg]


def test_blank_dotenv_values_mean_unset(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "".join(f"{name}=\n" for name in (*_BLANK_MEANS_DEFAULT, *_BLANK_SECRETS)),
        encoding="utf-8",
    )
    _assert_blank_means_unset(Settings(_env_file=env_file))  # type: ignore[call-arg]


def test_blank_secrets_are_reported_missing_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.api.orchestrator import REQUIRED_SETTINGS, missing_orchestrator_settings

    for _, name in REQUIRED_SETTINGS:
        monkeypatch.setenv(name, "")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert missing_orchestrator_settings(settings) == [name for _, name in REQUIRED_SETTINGS]


def test_api_boots_with_a_blank_token_validity(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app

    monkeypatch.setenv("TOKEN_VALIDITY_S", "")
    monkeypatch.setenv("APPROVAL_TIMEOUT_MS", "")
    monkeypatch.setattr("apps.api.main.configure_logging", lambda level: None)
    app = create_app(Settings(_env_file=None, app_env="test"))  # type: ignore[call-arg]
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_set_values_still_apply_and_still_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_VALIDITY_S", "2000")
    monkeypatch.setenv("LLM_MODEL", "gemini-3.8-flash")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (settings.token_validity_s, settings.llm_model) == (2000, "gemini-3.8-flash")
    monkeypatch.setenv("TOKEN_VALIDITY_S", "not-a-number")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("TOKEN_VALIDITY_S", " ")  # whitespace is a value, not blank
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_secrets_are_not_exposed_in_repr_or_dump() -> None:
    settings = make_settings(armoriq_api_key="ak_test_super_secret", slack_bot_token="xoxb-secret")
    assert "ak_test_super_secret" not in repr(settings)
    assert "xoxb-secret" not in settings.model_dump_json()
    assert settings.armoriq_api_key is not None
    assert settings.armoriq_api_key.get_secret_value() == "ak_test_super_secret"
