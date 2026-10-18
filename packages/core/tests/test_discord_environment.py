from __future__ import annotations

import pytest
from daimon.core.config import DiscordSettings, EnvironmentSettings, load_settings
from pydantic import SecretStr


def test_discord_environment_defaults_to_production_behavior() -> None:
    assert EnvironmentSettings().name == ""
    assert DiscordSettings(bot_token=SecretStr("test")).command_guild_ids == []


def test_discord_environment_and_guild_ids_load_from_nested_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_DISCORD__BOT_TOKEN", "discord-test")
    monkeypatch.setenv("DAIMON_ENVIRONMENT__NAME", "staging")
    monkeypatch.setenv("DAIMON_DISCORD__COMMAND_GUILD_IDS", "[111,222]")

    settings = load_settings(_env_file=None)

    assert settings.environment.name == "staging"
    assert settings.discord is not None
    assert settings.discord.command_guild_ids == [111, 222]
