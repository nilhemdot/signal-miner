import os
import uuid

import psycopg
import pytest

from signal_miner.db.migrate import apply_migrations

ADMIN_DSN = os.environ.get("SM_TEST_ADMIN_DSN", "postgresql://sm_admin:sm_admin@localhost:5432/postgres")


def _dsn_for(dbname: str) -> str:
    return psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=dbname)


@pytest.fixture(scope="session")
def make_db():
    """Factory for throwaway databases; every one is dropped at session end."""
    created = []

    def _make(migrate: bool = True) -> str:
        name = f"sm_test_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        dsn = _dsn_for(name)
        if migrate:
            apply_migrations(dsn)
        return dsn

    yield _make
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        for name in created:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def db_dsn(make_db):
    return make_db()


@pytest.fixture
def writer(db_dsn):
    """Connection acting as pipeline_writer, the role the pipeline runs as."""
    with psycopg.connect(db_dsn) as conn:
        conn.execute("SET ROLE pipeline_writer")
        conn.execute("SET search_path = sm, extensions")
        conn.commit()
        yield conn
