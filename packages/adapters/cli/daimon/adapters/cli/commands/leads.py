"""Deployment operator configuration for native tenant lead intake."""

import json
from pathlib import Path
from uuid import UUID

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.github_credentials import build_multifernet
from daimon.core.lead_intake import IntakeConfig
from daimon.core.mcp_auth import read_private_credential_bytes
from daimon.core.stores.lead_intake import IntakeStore
from rich.console import Console

leads_app = typer.Typer(help="Configure tenant lead intake and encrypted integration credentials.")


@leads_app.command("configure")
def configure(tenant_id: UUID, config_file: Path, credentials_file: Path) -> None:
    """Load tenant configuration and credentials from private operator-owned JSON files.

    Starts in capture unless the config explicitly names active or crm_paused.
    Reuse the revision for a mode-only cutover; increment it for destination or
    connection changes. Never paste integration credentials into chat.
    """
    settings = load_settings()
    console = Console(highlight=False)
    # Private-file validation precedes parsing; errors never print field values.
    try:
        config = IntakeConfig.model_validate_json(read_private_credential_bytes(config_file))
        raw = json.loads(read_private_credential_bytes(credentials_file))
        if not isinstance(raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
        ):
            raise ValueError("invalid credentials")
        if "hubspot_access_token" not in raw or not (
            "gmail_access_token" in raw or "gmail_refresh_token" in raw
        ):
            raise ValueError("missing credentials")
    except (ValueError, OSError):
        console.print("Invalid private intake configuration or credential file.")
        raise typer.Exit(2) from None

    async def run() -> None:
        async with build_runtime(settings) as runtime:
            fernet = build_multifernet(
                tuple(key.get_secret_value() for key in settings.crypto.keys)
            )
            store = IntakeStore(runtime.sessionmaker, fernet, tenant_id)
            await store.configure(config, raw)
        console.print(f"Lead intake configuration saved for {tenant_id}: {config.mode}.")

    run_cli(run(), console=console)
