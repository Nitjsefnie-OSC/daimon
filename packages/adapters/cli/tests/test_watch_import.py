"""Operator import CLI is discoverable and rejects oversized exports before parsing."""

from daimon.adapters.cli.commands.watches import watches_app
from typer.testing import CliRunner


def test_watch_import_cli_rejects_oversized_manifest(tmp_path):
    export = tmp_path / "watches.json"
    export.write_text("x" * 1048577)
    result = CliRunner().invoke(watches_app, ["import", str(export)])
    assert result.exit_code == 2 and "exceeds 1 MiB" in result.output


def test_watch_confirm_requires_all_binding_fields():
    result = CliRunner().invoke(watches_app, ["confirm", "--help"])
    assert result.exit_code == 0
    assert "OWNER_ACCOUNT_ID" in result.output
