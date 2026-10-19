"""Real isolated Postgres fixtures; ingress uses the migrated SQL boundary."""

from daimon.testing.db import (  # noqa: F401
    db_clean,
    db_engine,
    db_schema,
    db_session,
    db_session_factory,
)
