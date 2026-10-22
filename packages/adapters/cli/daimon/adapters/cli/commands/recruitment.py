"""Operator-only tenant staffing enrollment and authorization configuration."""

from pathlib import Path
from uuid import UUID

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.github_credentials import build_multifernet
from daimon.core.mcp_auth import read_private_credential_bytes
from daimon.core.recruitment import RecruitmentConfig
from daimon.core.stores.recruitment import RecruitmentStore
from rich.console import Console

recruitment_app = typer.Typer(help="Configure tenant recruitment without exposing candidate tools.")


@recruitment_app.command("configure")
def configure(tenant_id: UUID, config_file: Path) -> None:
    """Load private JSON with enabled, guild_id, recruiter IDs/roles and retention.

    Increment revision when changing configuration. Service API secrets belong
    in trusted process settings, never in this file or chat messages.
    """
    console = Console(highlight=False)
    try:
        config = RecruitmentConfig.model_validate_json(read_private_credential_bytes(config_file))
    except (ValueError, OSError):
        console.print("Invalid private recruitment configuration file.")
        raise typer.Exit(2) from None
    settings = load_settings()

    async def run() -> None:
        fernet = build_multifernet(tuple(k.get_secret_value() for k in settings.crypto.keys))
        async with build_runtime(settings) as runtime:
            await RecruitmentStore(runtime.sessionmaker, fernet, tenant_id).configure(config)
        console.print(f"Recruitment configuration saved for {tenant_id}.")

    run_cli(run(), console=console)
