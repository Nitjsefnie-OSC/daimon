"""Sidecar's independent environment allowlist; no provider or vault settings."""

from daimon.core.continuity.page_events import KEY_ID
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SidecarSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DAIMON_SIDECAR__", extra="ignore", hide_input_in_errors=True
    )

    database_url: SecretStr = Field(
        description=(
            "Lane database URL for a dedicated login granted only daimon_sidecar. "
            "Require TLS for remote databases."
        )
    )
    signing_keys: dict[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "OS-host page-event HMAC keys by key ID, each at least 32 random bytes. "
            "Supply from lane secret management; keys never enter browser HTML or watch rows. "
            "Only configured OS page hosts may sign events; outside senders are unsupported."
        ),
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

    @field_validator("signing_keys")
    @classmethod
    def bounded_os_keys(cls, keys: dict[str, SecretStr]) -> dict[str, SecretStr]:
        if len(keys) > 16 or any(
            not KEY_ID.fullmatch(name) or not 32 <= len(key.get_secret_value().encode()) <= 256
            for name, key in keys.items()
        ):
            raise ValueError("Use at most sixteen OS keys, each 32 to 256 bytes, with safe key IDs")
        return keys
