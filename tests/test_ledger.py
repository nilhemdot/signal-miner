"""AC 14: budget reservations, concurrency, and the skipped_budget fallthrough."""
import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from signal_miner.ledger.ledger import SKIPPED_BUDGET, Call, reserve, release, route

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def ledger_conn(writer):
    writer.execute("DELETE FROM sm.api_usage")
    writer.execute("DELETE FROM sm.api_budget")
    writer.commit()
    yield writer
    writer.execute("DELETE FROM sm.api_usage")
    writer.execute("DELETE FROM sm.api_budget")
    writer.commit()


def _cap(conn, provider, period, cap):
    conn.execute("INSERT INTO sm.api_budget (provider, period, cap) VALUES (%s, %s, %s)", (provider, period, cap))
    conn.commit()


def _used(conn, provider):
    return conn.execute("SELECT coalesce(sum(units), 0) FROM sm.api_usage WHERE provider = %s", (provider,)).fetchone()[0]


def test_reservation_within_cap_is_granted(ledger_conn):
    _cap(ledger_conn, "tavily", "month", 10)
    assert reserve(ledger_conn, "tavily", units=4, now=NOW) is not None
    assert _used(ledger_conn, "tavily") == 4


def test_reservation_over_cap_is_refused(ledger_conn):
    _cap(ledger_conn, "tavily", "month", 10)
    assert reserve(ledger_conn, "tavily", units=8, now=NOW) is not None
    assert reserve(ledger_conn, "tavily", units=3, now=NOW) is None
    assert _used(ledger_conn, "tavily") == 8


def test_provider_without_budget_row_is_refused(ledger_conn):
    assert reserve(ledger_conn, "unknown", units=1, now=NOW) is None


def test_every_period_cap_must_hold(ledger_conn):
    _cap(ledger_conn, "tinyfish", "hour", 2)
    _cap(ledger_conn, "tinyfish", "month", 100)
    assert reserve(ledger_conn, "tinyfish", units=2, now=NOW)
    assert reserve(ledger_conn, "tinyfish", units=1, now=NOW) is None
    # Next hour the hourly window resets; the monthly cap still has room.
    assert reserve(ledger_conn, "tinyfish", units=1, now=NOW + timedelta(hours=1))


def test_usage_from_a_previous_month_does_not_count(ledger_conn):
    _cap(ledger_conn, "exa", "month", 5)
    assert reserve(ledger_conn, "exa", units=5, now=NOW - timedelta(days=31))
    assert reserve(ledger_conn, "exa", units=5, now=NOW)


def test_release_returns_units(ledger_conn):
    _cap(ledger_conn, "firecrawl", "month", 3)
    rid = reserve(ledger_conn, "firecrawl", units=3, now=NOW)
    assert reserve(ledger_conn, "firecrawl", units=1, now=NOW) is None
    release(ledger_conn, rid)
    assert reserve(ledger_conn, "firecrawl", units=1, now=NOW)


def test_twenty_concurrent_reservations_never_exceed_cap(ledger_conn, db_dsn):
    _cap(ledger_conn, "parallel", "month", 7)
    granted = []
    barrier = threading.Barrier(20)

    def worker():
        with psycopg.connect(db_dsn) as conn:
            conn.execute("SET ROLE pipeline_writer")
            conn.commit()
            barrier.wait()
            if reserve(conn, "parallel", units=1, now=NOW) is not None:
                granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(granted) == 7
    assert _used(ledger_conn, "parallel") == 7


def test_route_falls_through_to_next_provider(ledger_conn):
    _cap(ledger_conn, "tinyfish", "hour", 0)
    _cap(ledger_conn, "tavily", "month", 10)
    chain = [Call("tinyfish", 1), Call("tavily", 1), Call("exa", 1)]
    provider, rid = route(ledger_conn, chain, now=NOW)
    assert provider == "tavily" and rid is not None


def test_route_returns_skipped_budget_when_every_provider_is_spent(ledger_conn):
    _cap(ledger_conn, "tinyfish", "hour", 0)
    _cap(ledger_conn, "tavily", "month", 0)
    provider, rid = route(ledger_conn, [Call("tinyfish", 1), Call("tavily", 1), Call("exa", 1)], now=NOW)
    assert provider == SKIPPED_BUDGET and rid is None
