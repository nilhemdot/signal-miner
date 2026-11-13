"""Budget ledger for paid API calls (spec section 8).

A call reserves units before it runs. The budget rows of a provider are locked for the
duration of the check, so concurrent reservations serialize and can never overshoot a cap.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg

SKIPPED_BUDGET = "skipped_budget"

# Short caps are rate limits, so they use a rolling window; long caps follow the provider's calendar reset (UTC).
ROLLING = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1)}
CALENDAR = {"day", "week", "month"}


def window_start(period: str, now: datetime) -> datetime:
    if period in ROLLING:
        return now - ROLLING[period]
    now = now.astimezone(timezone.utc)
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":  # ISO week, Monday 00:00 UTC
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown period {period!r}")


@dataclass(frozen=True)
class Call:
    provider: str
    units: float
    est_usd: float = 0.0


def reserve(conn: psycopg.Connection, provider: str, units: float, *, est_usd: float = 0.0,
            run_id=None, now: datetime | None = None) -> int | None:
    """Reserve units against every cap of `provider`. Returns the usage id, or None if any cap would be exceeded."""
    now = now or datetime.now(timezone.utc)
    units_d = Decimal(str(units))
    with conn.transaction():
        caps = conn.execute(
            "SELECT period, cap FROM sm.api_budget WHERE provider = %s ORDER BY period FOR UPDATE",
            (provider,),
        ).fetchall()
        if not caps:
            return None
        for period, cap in caps:
            # No upper bound: a row stamped later than `now` by another worker still counts.
            # Rolling windows exclude their start instant; calendar windows include it.
            op = ">" if period in ROLLING else ">="
            used = conn.execute(
                f"SELECT coalesce(sum(units), 0) FROM sm.api_usage WHERE provider = %s AND reserved_at {op} %s",
                (provider, window_start(period, now)),
            ).fetchone()[0]
            if used + units_d > cap:
                return None
        return conn.execute(
            """INSERT INTO sm.api_usage (run_id, provider, units, est_usd, reserved_at)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (run_id, provider, units_d, Decimal(str(est_usd)), now),
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
