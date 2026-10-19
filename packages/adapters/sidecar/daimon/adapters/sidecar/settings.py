"""Sidecar's independent environment allowlist; no provider or vault settings."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SidecarSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DAIMON_SIDECAR__", extra="ignore")

    database_url: SecretStr = Field(
        description=(
            "Lane database URL for a dedicated login granted only daimon_sidecar. "
            "Require TLS for remote databases."
        )
    )
    host: str = Field(
        default="0.0.0.0", description="Private listen address behind the callback-only TLS proxy."
    )
    port: int = Field(
        default=8780, ge=1, le=65535, description="Private callback and health HTTP port."
    )
    max_concurrent: int = Field(
        default=8,
        ge=1,
        le=32,
        description="Maximum concurrent callback transactions; excess requests receive 429.",
    )
    request_timeout_s: float = Field(
        default=10, ge=1, le=30, description="Total callback body and database deadline in seconds."
    )
