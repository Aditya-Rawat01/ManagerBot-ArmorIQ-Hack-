"""API configuration from environment variables (and an optional `.env`).

Every secret is optional so the app can be imported, started, and unit-tested
without live credentials. Features that need a secret must check for it and
fail closed when it is missing.

A blank variable (`TOKEN_VALIDITY_S=`, as a hosting dashboard or compose
`${VAR:-}` produces) means unset (`env_ignore_empty`), in the environment and
in `.env` alike: the field takes its default, and a secret stays None. A blank
value never crashes startup and never becomes an empty-string setting.
"""

import math
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

API_DIR = Path(__file__).resolve().parents[1]  # apps/api
REPO_ROOT = API_DIR.parents[1]

AppEnv = Literal["development", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", API_DIR / ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # --- application ---------------------------------------------------------
    app_env: AppEnv = "development"
    log_level: LogLevel = "INFO"
    service_name: str = "proxylead-backend"
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )
    session_secret: SecretStr | None = None
    ctx_secret: SecretStr | None = None
    demo_passcode: SecretStr | None = None
    admin_token: SecretStr | None = None

    # --- database (Neon PostgreSQL; backend role = app metadata only) --------
    database_url_backend: SecretStr | None = None

    # --- ArmorIQ ---------------------------------------------------------------
    armoriq_api_key: SecretStr | None = None
    armoriq_agent_id: str = "proxylead"
    armoriq_console_url: str = "https://platform.armoriq.ai"
    armoriq_mcp_memory_name: str = "memory-mcp"
    armoriq_mcp_workspace_name: str = "workspace-mcp"
    token_validity_s: int = Field(default=1200, gt=0)
    approval_timeout_ms: int = Field(default=900_000, gt=0)
    # SDK observability shipper (span upload to ArmorIQ). Off unless opted in;
    # the gateway disables it per session (docs/SECURITY.md).
    armoriq_observability: bool = False

    # --- demo members (trusted identity mapping) ------------------------------
    persona_maya_email: SecretStr | None = None
    persona_sam_email: SecretStr | None = None
    persona_priya_email: SecretStr | None = None
    slack_user_maya_id: str | None = None
    slack_user_sam_id: str | None = None
    slack_user_priya_id: str | None = None

    # --- Slack (Socket Mode) ---------------------------------------------------
    slack_bot_token: SecretStr | None = None
    slack_app_token: SecretStr | None = None
    slack_team_id: str | None = None
    slack_allowed_channels: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- LLM: Google Gemini, Gemini Developer API (ADR-032) ---------------------
    # A Google AI Studio key, never a Vertex AI credential.
    gemini_api_key: SecretStr | None = None
    llm_model: str = "gemini-2.5-flash"

    # --- grounding (responder abstention; calibrated by E17, P3-08) ------------
    # PRD §12 MIN_SCORE. None until E17 calibrates it: the responder abstains
    # (`threshold_unset`) rather than guessing a threshold (D-23). P3-04 passes
    # it to `respond`; the stricter of it and a search result's `min_score`
    # applies.
    min_score: float | None = None

    # --- MCP tool server -----------------------------------------------------
    toolserver_base_url: str | None = None

    @field_validator("armoriq_observability", mode="before")
    @classmethod
    def _blank_observability(cls, value: object) -> object:
        # A blank value means unset: keep telemetry off. Blank environment
        # values never get here (env_ignore_empty); this covers explicit ones.
        if isinstance(value, str) and not value.strip():
            return False
        return value

    @field_validator("min_score", mode="before")
    @classmethod
    def _blank_min_score(cls, value: object) -> object:
        # `.env.example` ships `MIN_SCORE=` (empty) until E17 calibrates it.
        # env_ignore_empty already drops a blank environment value; this also
        # covers explicit (constructor) and whitespace-only values.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("min_score")
    @classmethod
    def _finite_min_score(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("MIN_SCORE must be a finite number")
        return value

    @field_validator("cors_allowed_origins", "slack_allowed_channels", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("cors_allowed_origins")
    @classmethod
    def _no_wildcard_origin(cls, origins: list[str]) -> list[str]:
        # Credentials (session cookie) will be allowed, so "*" is never acceptable.
        if "*" in origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must list explicit origins, not '*'")
        return origins

    @model_validator(mode="after")
    def _token_outlives_approval(self) -> "Settings":
        # PRD v2 §7.1: an approval must never outlast the intent token it resumes.
        if self.token_validity_s * 1000 <= self.approval_timeout_ms + 120_000:
            raise ValueError(
                "TOKEN_VALIDITY_S*1000 must exceed APPROVAL_TIMEOUT_MS + 120000 "
                f"(got {self.token_validity_s}s and {self.approval_timeout_ms}ms)"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings, read once from the environment."""
    return Settings()
