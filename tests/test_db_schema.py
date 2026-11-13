"""AC 27: roles, RLS, no contact-data columns, dump and restore."""
import os
import re
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


CONTACT_COLUMN = re.compile(r"e_?mail|phone|mobile|\btel\b|_tel$|^tel_|address|addr|street|zip|postal")
# Holds a bare domain (acme.io), never a mailbox; AC 8 checks stored text for real addresses.
CONTACT_ALLOWLIST = {("gh_orgs", "email_domain")}


def test_no_column_is_named_for_contact_data(writer):
    cols = writer.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'sm'"
    ).fetchall()
    offenders = [(t, c) for t, c in cols if CONTACT_COLUMN.search(c) and (t, c) not in CONTACT_ALLOWLIST]
    assert offenders == []


def test_contact_column_pattern_catches_common_names():
    for name in ("email", "contact_email", "e_mail", "phone", "mobile", "tel", "street", "zip_code", "addr", "postal_code"):
        assert CONTACT_COLUMN.search(name), name
    for name in ("domain", "hq_state", "title", "total", "hotel_count"):
        assert not CONTACT_COLUMN.search(name), name


def test_pg_trgm_lives_in_extensions_schema(writer):
    schema = writer.execute(
        """SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
           WHERE e.extname = 'pg_trgm'"""
    ).fetchone()[0]
    assert schema == "extensions"


def test_migrations_are_idempotent(db_dsn):
    from signal_miner.db.migrate import apply_migrations

    assert apply_migrations(db_dsn) == []


def _restore(dump: str, make_db) -> str:
    target = make_db(migrate=False)
    with psycopg.connect(target, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS extensions")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions")
    res = subprocess.run([shutil.which("psql"), "-v", "ON_ERROR_STOP=1", "-q", target], input=dump,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return target


def test_dump_restores_into_empty_database_and_stays_usable(db_dsn, make_db):
    """AC 27. CI runs this against Postgres 15 (SM_REQUIRE_PG_MAJOR=15); locally it runs on whatever server is up."""
    want = os.environ.get("SM_REQUIRE_PG_MAJOR")
    with psycopg.connect(db_dsn) as conn:
        major = conn.info.server_version // 10000
    if want:
        assert major == int(want)
    pg_dump = shutil.which("pg_dump")
    assert pg_dump and shutil.which("psql")
    # Run the dump as the owner role, the way the weekly backup job does (spec section 6).
    dump = subprocess.run([pg_dump, "--role=pipeline_writer", "--schema=sm", "--schema=sm_meta", db_dsn],
                          capture_output=True, text=True, check=True, timeout=60).stdout
    target = _restore(dump, make_db)
    with psycopg.connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'sm'").fetchone()[0] == len(SM_TABLES)
        conn.execute("SET ROLE pipeline_writer")
        conn.execute("INSERT INTO sm.suppression (domain, reason) VALUES ('restored.example', 'test')")
        assert conn.execute("SELECT count(*) FROM sm.suppression").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sm_meta.applied").fetchone()[0] >= 1
        conn.rollback()
        conn.execute("SET ROLE anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM sm.companies")


def test_pipeline_login_is_a_least_privilege_member(db_dsn):
    with psycopg.connect(db_dsn) as conn:
        row = conn.execute(
            """SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls,
                      pg_has_role('pipeline_login', 'pipeline_writer', 'MEMBER')
               FROM pg_roles WHERE rolname = 'pipeline_login'"""
        ).fetchone()
    assert row == (True, False, False, False, False, True)


def test_missing_migrations_directory_fails_loudly(db_dsn, tmp_path):
    from signal_miner.db.migrate import NoMigrationsFound, apply_migrations

    with pytest.raises(NoMigrationsFound):
        apply_migrations(db_dsn, directory=tmp_path)
