"""Budget ledger for paid API calls (spec section 8).

A call reserves units before it runs. The budget rows of a provider are locked for the
duration of the check, so concurrent reservations serialize and can never overshoot a cap.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

SKIPPED_BUDGET = "skipped_budget"


@dataclass(frozen=True)
class Call:
    provider: str
    units: float
    est_usd: float = 0.0


def reserve(conn: psycopg.Connection, provider: str, units: float, *, est_usd: float = 0.0,
            run_id=None, now: datetime | None = None) -> int | None:
    """Reserve units against every cap of `provider`. Returns the usage id, or None if any cap would be exceeded."""
    now = now or datetime.now(timezone.utc)
    with conn.transaction():
        caps = conn.execute(
            "SELECT period, cap FROM sm.api_budget WHERE provider = %s ORDER BY period FOR UPDATE",
            (provider,),
        ).fetchall()
        if not caps:
            return None
        for period, cap in caps:
            used = conn.execute(
                """SELECT coalesce(sum(units), 0) FROM sm.api_usage
                   WHERE provider = %(p)s
                     AND reserved_at >= date_trunc(%(period)s, %(now)s::timestamptz, 'UTC')
                     AND reserved_at <= %(now)s""",
                {"p": provider, "period": period, "now": now},
            ).fetchone()[0]
            if used + units > cap:
                return None
        return conn.execute(
            """INSERT INTO sm.api_usage (run_id, provider, units, est_usd, reserved_at)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (run_id, provider, units, est_usd, now),
        ).fetchone()[0]


def commit(conn: psycopg.Connection, usage_id: int) -> None:
    with conn.transaction():
        conn.execute("UPDATE sm.api_usage SET committed = true WHERE id = %s", (usage_id,))


def release(conn: psycopg.Connection, usage_id: int) -> None:
    """Return a reservation whose call was never made."""
    with conn.transaction():
        conn.execute("DELETE FROM sm.api_usage WHERE id = %s AND NOT committed", (usage_id,))


def route(conn: psycopg.Connection, chain: list[Call], *, run_id=None,
          now: datetime | None = None) -> tuple[str, int | None]:
    """Reserve on the first provider in `chain` with room. Returns (provider, usage_id) or (SKIPPED_BUDGET, None)."""
    for call in chain:
        rid = reserve(conn, call.provider, call.units, est_usd=call.est_usd, run_id=run_id, now=now)
        if rid is not None:
            return call.provider, rid
    return SKIPPED_BUDGET, None
