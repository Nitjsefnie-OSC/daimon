"""The image check must detect a changed or absent installed distribution."""

from scripts.check_report_host_lock import differences, expected_versions


def test_lock_check_compares_active_pins_and_normalized_names() -> None:
    export = """\
FastAPI==0.136.3 \\
    --hash=sha256:abcdef
typing_extensions==4.15.0 ; python_version >= '3.12' \\
    --hash=sha256:abcdef
win-only==1.0 ; sys_platform == 'win32' \\
    --hash=sha256:abcdef
"""
    expected = expected_versions(export, {"name": "report-host", "version": "0.2.0"})
    assert expected == {
        "fastapi": "0.136.3",
        "report-host": "0.2.0",
        "typing-extensions": "4.15.0",
    }
    assert (
        differences(
            expected,
            {"fastapi": "0.136.3", "report-host": "0.2.0", "typing-extensions": "4.15.0"},
        )
        == []
    )
    assert differences(
        expected,
        {"fastapi": "0.136.4", "report-host": "0.2.0"},
    ) == [
        "fastapi: locked 0.136.3, installed 0.136.4",
        "missing typing-extensions==4.15.0",
    ]
