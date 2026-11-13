"""Command line entry points. Connection strings and the SEC contact come from the environment only."""
import argparse
import os
import sys
from datetime import date, datetime, timezone

import psycopg

from signal_miner.db.migrate import apply_migrations
from signal_miner.http.sec import make_sec_client
from signal_miner.output.leads import form_d_leads_csv
from signal_miner.score.weekly import rank_week
from signal_miner.sec.loader import catch_up, ingest_day


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} is not set")
    return value


def _date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="signal-miner")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply pending migrations (uses SM_ADMIN_DSN)")
    ing = sub.add_parser("ingest-form-d", help="load Form D filings (uses SM_DSN, SEC_USER_AGENT)")
    g = ing.add_mutually_exclusive_group()
    g.add_argument("--date", type=_date, help="one day, YYYY-MM-DD")
    g.add_argument("--catch-up", type=int, default=10, metavar="DAYS", help="days back to fill in (default 10)")
    lead = sub.add_parser("form-d-leads", help="rank the week and write the Form D lead CSV (uses SM_DSN)")
    lead.add_argument("--as-of", type=_date, default=None)
    lead.add_argument("--out", default="-", help="file path, or - for stdout")
    args = p.parse_args(argv)

    if args.cmd == "migrate":
        for name in apply_migrations(_env("SM_ADMIN_DSN")):
            print(f"applied {name}")
        return 0

    with psycopg.connect(_env("SM_DSN")) as conn:
        conn.execute("SET search_path = sm, extensions")
        if args.cmd == "ingest-form-d":
            client = make_sec_client()
            if args.date:
                results = [(args.date, *ingest_day(conn, client, args.date))]
            else:
                results = catch_up(conn, client, datetime.now(timezone.utc).date(), args.catch_up)
            for day, status, stats in results:
                print(f"{day} {status} {stats.__dict__}")
            return 0
        if args.cmd == "form-d-leads":
            as_of = args.as_of or datetime.now(timezone.utc).date()
            rank_week(conn, as_of)
            text = form_d_leads_csv(conn, as_of)
            if args.out == "-":
                sys.stdout.write(text)
            else:
                with open(args.out, "w") as fh:
                    fh.write(text)
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
