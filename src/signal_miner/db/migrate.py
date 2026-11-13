"""Apply migrations/NNN_*.sql in order, once each, tracked in sm_meta.applied."""
from importlib import resources
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(str(resources.files("signal_miner.migrations")))


class NoMigrationsFound(RuntimeError):
    pass

_BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS sm_meta;
CREATE TABLE IF NOT EXISTS sm_meta.applied (
    name        text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sm_meta.applied ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON SCHEMA sm_meta FROM PUBLIC;
"""


def apply_migrations(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations as the connecting admin role. Returns the names applied."""
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise NoMigrationsFound(f"no NNN_*.sql files in {directory}")
    applied_now = []
    with psycopg.connect(dsn) as conn:
        conn.execute(_BOOTSTRAP)
        conn.commit()
        done = {r[0] for r in conn.execute("SELECT name FROM sm_meta.applied")}
        for f in files:
            if f.name in done:
                continue
            with conn.transaction():
                conn.execute(f.read_text())
                conn.execute("INSERT INTO sm_meta.applied (name) VALUES (%s)", (f.name,))
            applied_now.append(f.name)
    return applied_now


if __name__ == "__main__":
    import os
    import sys

    dsn = os.environ.get("SM_ADMIN_DSN")
    if not dsn:
        sys.exit("SM_ADMIN_DSN is not set")
    for name in apply_migrations(dsn):
        print(f"applied {name}")
