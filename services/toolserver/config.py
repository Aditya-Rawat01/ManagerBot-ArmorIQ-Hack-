"""MCP tool-server configuration from environment variables (and an optional `.env`)."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

SERVER_DIR = Path(__file__).resolve().parent  # services/toolserver
REPO_ROOT = SERVER_DIR.parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", SERVER_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    service_name: str = "proxylead-mcp-server"

    # Service credentials for the two registered endpoints. If a token is not
    # configured, that endpoint rejects every request (fail closed).
    toolserver_bearer_memory: SecretStr | None = None
    toolserver_bearer_workspace: SecretStr | None = None

    # Host header values accepted by the MCP transport (DNS-rebinding
    # protection). Set to the public hostname when deployed.
    mcp_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost:8100", "127.0.0.1:8100"]
    )

    # Scaffold-only ping tools (no data access); see ADR-011.
    mcp_enable_diagnostic_tools: bool = True

    # Used from Phase 2 onward (identity envelope, knowledge store).
    ctx_secret: SecretStr | None = None
    database_url_toolserver: SecretStr | None = None

    # Query-embedding model (PRD §12 name). Must equal
    # services.knowledge.embeddings.MODEL_ID; the lifespan refuses to warm any
    # other model (fail closed, never a hosted embedder).
    embed_model: str = "BAAI/bge-small-en-v1.5"

    @field_validator("mcp_allowed_hosts", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("mcp_allowed_hosts")
    @classmethod
    def _require_hosts(cls, hosts: list[str]) -> list[str]:
        if not hosts:
            raise ValueError("MCP_ALLOWED_HOSTS must list at least one host")
        return hosts


@lru_cache
def get_settings() -> Settings:
    return Settings()
