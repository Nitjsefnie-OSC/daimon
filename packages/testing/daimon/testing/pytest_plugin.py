"""Shared database preflight, discovered through the pytest11 entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from daimon.testing.db import DatabasePreflight


@pytest.fixture(scope="session")
def db_preflight(db_schema: str) -> DatabasePreflight:
    """Admit the configured test database before any shared engine fixture runs.

    Import lazily so database-free tests do not load models or defer name writes.
    Existing conftests importing shared DB fixtures need no additional imports.
    """
    from daimon.testing.db import preflight_test_database

    return preflight_test_database(db_schema)
