"""AC 27: roles, RLS, no contact-data columns, dump and restore."""
import shutil
import subprocess

import psycopg
import pytest

SM_TABLES = {
    "runs", "source_health", "companies", "company_aliases", "er_review_queue",
    "form_d_filings", "form_d_offerings", "hn_threads", "hn_posts", "tool_dictionary",
    "ats_boards", "job_postings", "gh_org_weekly", "gh_orgs", "signals", "weekly_scores",
    "enrichments", "api_budget", "api_usage", "outputs", "suppression",
}


def _tables(conn):
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'sm'").fetchall()
    return {r[0] for r in rows}


def test_every_spec_table_exists(writer):
    assert _tables(writer) == SM_TABLES


def test_pipeline_writer_reads_and_writes_every_table(writer):
    for t in sorted(SM_TABLES):
        writer.execute(f"SELECT count(*) FROM sm.{t}")
        writer.execute(
            "SELECT has_table_privilege('pipeline_writer', %s, 'INSERT, UPDATE, DELETE')",
            (f"sm.{t}",),
        )
    # A real write round trip, rolled back.
    writer.execute("INSERT INTO sm.suppression (domain, reason) VALUES ('example.com', 'test')")
    assert writer.execute("SELECT count(*) FROM sm.suppression").fetchone()[0] == 1
    writer.rollback()
    for t in sorted(SM_TABLES):
        ok = writer.execute(
            "SELECT has_table_privilege('pipeline_writer', %s, 'SELECT, INSERT, UPDATE, DELETE')",
            (f"sm.{t}",),
        ).fetchone()[0]
        assert ok, t


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_public_api_roles_get_42501(db_dsn, role):
    with psycopg.connect(db_dsn) as conn:
        conn.execute(f"SET ROLE {role}")
        for t in sorted(SM_TABLES):
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
                conn.execute(f"SELECT 1 FROM sm.{t}")
            assert exc.value.sqlstate == "42501"
            conn.rollback()
            conn.execute(f"SET ROLE {role}")


def test_rls_enabled_on_every_table_without_force(writer):
    rows = writer.execute(
        """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'sm' AND c.relkind = 'r'"""
    ).fetchall()
    assert {r[0] for r in rows} == SM_TABLES
    for name, enabled, forced in rows:
        assert enabled, name
        assert not forced, name


def test_no_column_is_named_for_contact_data(writer):
    cols = writer.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'sm'"
    ).fetchall()
    banned = ("email", "phone", "address")
    for table, col in cols:
        for word in banned:
            assert col != word and not col.endswith(f"_{word}"), f"{table}.{col}"


def test_pg_trgm_lives_in_extensions_schema(writer):
    schema = writer.execute(
        """SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
           WHERE e.extname = 'pg_trgm'"""
    ).fetchone()[0]
    assert schema == "extensions"


def test_migrations_are_idempotent(db_dsn):
    from signal_miner.db.migrate import apply_migrations

    assert apply_migrations(db_dsn) == []


def test_dump_restores_into_empty_database(db_dsn, make_db):
    pg_dump, psql = shutil.which("pg_dump"), shutil.which("psql")
    assert pg_dump and psql
    dump = subprocess.run([pg_dump, "--no-owner", "--schema=sm", "--schema=sm_meta", db_dsn],
                          capture_output=True, text=True, check=True, timeout=60).stdout
    target = make_db(migrate=False)
    with psycopg.connect(target, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS extensions")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions")
    res = subprocess.run([psql, "-v", "ON_ERROR_STOP=1", "-q", target], input=dump,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    with psycopg.connect(target) as conn:
        n = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'sm'").fetchone()[0]
    assert n == len(SM_TABLES)
