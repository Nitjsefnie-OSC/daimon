"""Credential-reference validation without credential discovery or SDK imports."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, JsonValue, TypeAdapter, ValidationError

_RESERVED = ("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_")
_ROWS = TypeAdapter(list[dict[str, JsonValue]])
_MAX_BYTES = 1_048_576


class ProviderRuntimeConfigurationError(RuntimeError):
    """Static, typed operator errors; never include manifest content or paths."""

    def __init__(self, reason: Literal["reserved_key_reference", "invalid_manifest"]) -> None:
        self.reason = reason
        message = (
            "Provider runtime api_key_env uses a reserved provider prefix "
            "(OPENAI_, ANTHROPIC_, GOOGLE_, GEMINI_). Use a host-scoped name such as "
            "DAIMON_CHANNEL_OPENAI_KEY or DAIMON_CHANNEL_GEMINI_KEY."
            if reason == "reserved_key_reference"
            else "Provider runtime manifest must be a readable JSON list of objects, at most 1 MiB."
        )
        super().__init__(message)


def validate_api_key_env(value: str) -> str:
    """Refuse SDK ambient-variable namespaces before looking up any secret."""
    if value.upper().startswith(_RESERVED):
        raise ProviderRuntimeConfigurationError("reserved_key_reference")
    return value


ApiKeyEnvironment = Annotated[
    str,
    Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
    AfterValidator(validate_api_key_env),
]


def validate_configured_runtime_manifest() -> None:
    """Check an explicitly configured manifest's references, without constructing plans.

    Settings loaders call this only with mux and channel backends enabled. Missing
    manifests retain the existing selected-turn refusal; no discovery happens here.
    Scope/digest, full provider policy and key availability remain admission checks.
    """
    path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not path:
        return
    try:
        with Path(path).open("rb") as manifest:
            raw = manifest.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ProviderRuntimeConfigurationError("invalid_manifest")
        rows = _ROWS.validate_json(raw)
    except (OSError, ValidationError):
        raise ProviderRuntimeConfigurationError("invalid_manifest") from None
    for row in rows:
        reference = row.get("api_key_env")
        if isinstance(reference, str):
            validate_api_key_env(reference)
