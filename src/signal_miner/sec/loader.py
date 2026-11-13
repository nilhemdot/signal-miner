"""Load Form D filings into sm.form_d_filings, sm.form_d_offerings, companies and signals.

Every write is an upsert that skips unchanged rows, so reloading a day changes 0 rows.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx
import psycopg

from signal_miner.http.client import RetryExhausted
from signal_miner.er.normalize import normalize_name
from signal_miner.http.client import HttpClient
from signal_miner.score.formula import form_d_strength
from signal_miner.sec.filter import passes_startup_filter
from signal_miner.sec.formd import FormD, FormDParseError, parse_form_d
from signal_miner.sec.index import FORM_D_TYPES, IndexRow, fetch_daily_index

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SUBMISSIONS = "https://data.sec.gov/submissions"


@dataclass
class LoadStats:
    filings: int = 0
    malformed: int = 0
    missing: int = 0
    no_file_number: int = 0
    errors: int = 0
    changed: int = 0


def filing_dir_url(cik: int, accession: str) -> str:
    return f"{ARCHIVES}/{cik}/{accession.replace('-', '')}/"


class _Submissions:
    """accession -> (file_number, filing_date, form) per CIK, one request per CIK per run."""

    def __init__(self, client: HttpClient):
        self.client = client
        self.cache: dict[int, dict[str, tuple[str, date, str]]] = {}

    def lookup(self, cik: int, accession: str) -> tuple[str, date, str] | None:
        if cik not in self.cache:
            resp = self.client.get(f"{SUBMISSIONS}/CIK{cik:010d}.json")
            table = {}
            if resp.status_code == 200:
                r = resp.json()["filings"]["recent"]
                for acc, fn, fd, form in zip(r["accessionNumber"], r["fileNumber"], r["filingDate"], r["form"]):
                    table[acc] = (fn, date.fromisoformat(fd), form)
            self.cache[cik] = table
        return self.cache[cik].get(accession)


def _fetch_form_d(client: HttpClient, cik: int, accession: str) -> FormD | None | str:
    resp = client.get(f"{filing_dir_url(cik, accession)}primary_doc.xml")
    if resp.status_code == 404:
        return "missing"
    resp.raise_for_status()
    try:
        return parse_form_d(resp.content)
    except FormDParseError:
        return "malformed"


_UPSERT_FILING = """
INSERT INTO sm.form_d_filings (accession, cik, file_number, form_type, filed_date, previous_accession, amount_sold)
VALUES (%(accession)s, %(cik)s, %(file_number)s, %(form_type)s, %(filed_date)s, %(previous_accession)s, %(amount_sold)s)
ON CONFLICT (accession) DO UPDATE SET
    file_number = EXCLUDED.file_number, form_type = EXCLUDED.form_type, filed_date = EXCLUDED.filed_date,
    previous_accession = EXCLUDED.previous_accession, amount_sold = EXCLUDED.amount_sold
WHERE (sm.form_d_filings.file_number, sm.form_d_filings.form_type, sm.form_d_filings.filed_date,
       sm.form_d_filings.previous_accession, sm.form_d_filings.amount_sold)
   IS DISTINCT FROM (EXCLUDED.file_number, EXCLUDED.form_type, EXCLUDED.filed_date,
                     EXCLUDED.previous_accession, EXCLUDED.amount_sold)
"""

_OFFERING_COLS = ["latest_accession", "latest_filed_date", "latest_form_type", "entity_name", "entity_type", "city", "state",
                  "year_of_inc_bucket", "industry_group", "revenue_range", "offering_amount", "offering_indefinite",
                  "amount_sold", "date_of_first_sale", "first_sale_yet_to_occur", "related_person_count",
                  "passes_startup_filter", "company_id"]

# A newer filing replaces the offering's current state; an older one never overwrites it.
_UPSERT_OFFERING = f"""
INSERT INTO sm.form_d_offerings (cik, file_number, {", ".join(_OFFERING_COLS)})
VALUES (%(cik)s, %(file_number)s, {", ".join(f"%({c})s" for c in _OFFERING_COLS)})
ON CONFLICT (cik, file_number) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in _OFFERING_COLS)}
WHERE (
        EXCLUDED.latest_filed_date > sm.form_d_offerings.latest_filed_date
     OR %(previous_accession)s = sm.form_d_offerings.latest_accession
     OR (EXCLUDED.latest_filed_date = sm.form_d_offerings.latest_filed_date
         AND (EXCLUDED.latest_form_type, EXCLUDED.latest_accession)
             >= (sm.form_d_offerings.latest_form_type, sm.form_d_offerings.latest_accession))
  )
  AND ({", ".join(f"sm.form_d_offerings.{c}" for c in _OFFERING_COLS)})
      IS DISTINCT FROM ({", ".join(f"EXCLUDED.{c}" for c in _OFFERING_COLS)})
"""

_UPSERT_SIGNAL = """
INSERT INTO sm.signals (company_id, source, signal_type, source_ref, observed_at, strength, evidence_url, details)
VALUES (%(company_id)s, 'form_d', %(signal_type)s, %(source_ref)s, %(observed_at)s, %(strength)s, %(evidence_url)s, %(details)s)
ON CONFLICT (source, signal_type, source_ref) DO UPDATE SET
    company_id = EXCLUDED.company_id, observed_at = EXCLUDED.observed_at, strength = EXCLUDED.strength,
    evidence_url = EXCLUDED.evidence_url, details = EXCLUDED.details
WHERE (sm.signals.company_id, sm.signals.observed_at, sm.signals.strength, sm.signals.evidence_url, sm.signals.details)
   IS DISTINCT FROM (EXCLUDED.company_id, EXCLUDED.observed_at, EXCLUDED.strength, EXCLUDED.evidence_url, EXCLUDED.details)
"""


def _company_for(conn: psycopg.Connection, f: FormD) -> tuple[str, int]:
    """Exact external-id match on ('sec', cik); creates the company on first sight. Returns (company_id, rows changed)."""
    row = conn.execute("SELECT company_id FROM sm.company_aliases WHERE source = 'sec' AND external_id = %s", (str(f.cik),)).fetchone()
    if row:
        return row[0], 0
    company_id = conn.execute(
        "INSERT INTO sm.companies (name, normalized_name, hq_state) VALUES (%s, %s, %s) RETURNING company_id",
        (f.entity_name, normalize_name(f.entity_name), f.state),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO sm.company_aliases (source, external_id, company_id, match_method, match_score) VALUES ('sec', %s, %s, 'external_id', 1)",
        (str(f.cik), company_id),
    )
    return company_id, 2


def _previous_sold(conn, f: FormD, file_number: str) -> Decimal | None:
    """Amount sold before this amendment: the filing it names, else the offering's stored state. None if unknown."""
    if f.previous_accession:
        row = conn.execute("SELECT amount_sold FROM sm.form_d_filings WHERE accession = %s", (f.previous_accession,)).fetchone()
        if row:
            return row[0] if row[0] is not None else Decimal(0)
    row = conn.execute(
        "SELECT amount_sold FROM sm.form_d_offerings WHERE cik = %s AND file_number = %s", (f.cik, file_number)
    ).fetchone()
    if row:
        return row[0] if row[0] is not None else Decimal(0)
    return None


def _signal_for(f: FormD, filed: date, prev_sold: Decimal | None) -> tuple[str, float] | None:
    if not f.is_amendment:
        kind = "form_d_raised" if f.amount_sold and f.amount_sold > 0 else "form_d_filed"
        return kind, form_d_strength(kind, f.amount_sold, filed, f.date_of_first_sale)
    # An amendment signals only a real increase over a known earlier amount.
    if prev_sold is not None and f.amount_sold is not None and f.amount_sold > prev_sold:
        return "form_d_amend", form_d_strength("form_d_amend", f.amount_sold, filed, f.date_of_first_sale)
    return None


def _load_one(conn, client, subs: _Submissions, stats: LoadStats, index_cik: int, accession: str, fallback_day: date,
              depth: int = 0) -> None:
    parsed = _fetch_form_d(client, index_cik, accession)
    if parsed == "missing":
        stats.missing += 1
        return
    if parsed == "malformed":
        stats.malformed += 1
        return
    f: FormD = parsed
    cik = f.cik  # the primary issuer; a co-issuer index row points at the same document
    meta = subs.lookup(cik, accession)
    file_number, filed = (meta[0], meta[1]) if meta else (None, fallback_day)

    # Load the filing an amendment points at, one level deep, so the offering and amend signal see it.
    if f.is_amendment and f.previous_accession and depth == 0:
        known = conn.execute("SELECT 1 FROM sm.form_d_filings WHERE accession = %s", (f.previous_accession,)).fetchone()
        if not known:
            prev_meta = subs.lookup(cik, f.previous_accession)
            _load_one(conn, client, subs, stats, cik, f.previous_accession, prev_meta[1] if prev_meta else fallback_day, depth + 1)

    stats.filings += 1
    form_type = "D/A" if f.is_amendment or f.submission_type == "D/A" else "D"
    prev_sold = _previous_sold(conn, f, file_number) if (f.is_amendment and file_number) else None
    cur = conn.execute(_UPSERT_FILING, {
        "accession": accession, "cik": cik, "file_number": file_number, "form_type": form_type,
        "filed_date": filed, "previous_accession": f.previous_accession, "amount_sold": f.amount_sold,
    })
    stats.changed += cur.rowcount
    if file_number is None:
        stats.no_file_number += 1
        return

    passes = passes_startup_filter(f)
    company_id = None
    if passes:
        company_id, created = _company_for(conn, f)
        stats.changed += created

    cur = conn.execute(_UPSERT_OFFERING, {
        "cik": cik, "file_number": file_number, "latest_accession": accession, "latest_filed_date": filed,
        "latest_form_type": form_type, "previous_accession": f.previous_accession,
        "entity_name": f.entity_name, "entity_type": f.entity_type, "city": f.city, "state": f.state,
        "year_of_inc_bucket": f.year_of_inc_bucket, "industry_group": f.industry_group,
        "revenue_range": f.revenue_range, "offering_amount": f.offering_amount,
        "offering_indefinite": f.offering_indefinite, "amount_sold": f.amount_sold,
        "date_of_first_sale": f.date_of_first_sale, "first_sale_yet_to_occur": f.first_sale_yet_to_occur,
        "related_person_count": f.related_person_count, "passes_startup_filter": passes, "company_id": company_id,
    })
    stats.changed += cur.rowcount

    if passes:
        sig = _signal_for(f, filed, prev_sold)
        if sig:
            kind, strength = sig
            cur = conn.execute(_UPSERT_SIGNAL, {
                "company_id": company_id, "signal_type": kind, "source_ref": accession, "observed_at": filed,
                "strength": round(strength, 6), "evidence_url": filing_dir_url(cik, accession),
                "details": psycopg.types.json.Jsonb({
                    "amount_sold": str(f.amount_sold) if f.amount_sold is not None else None,
                    "file_number": file_number,
                    "first_sale": f.date_of_first_sale.isoformat() if f.date_of_first_sale else None,
                    "industry_group": f.industry_group,
                    "state": f.state,
                }),
            })
            stats.changed += cur.rowcount


def ingest_filings(conn: psycopg.Connection, client: HttpClient, rows: list[IndexRow]) -> LoadStats:
    """Load each accession once; one failing filing is counted and skipped, the rest still load."""
    stats = LoadStats()
    subs = _Submissions(client)
    seen = set()
    for row in sorted(rows, key=lambda r: (r.date_filed, r.accession, r.cik)):
        if row.form_type not in FORM_D_TYPES or row.accession in seen:
            continue
        seen.add(row.accession)
        try:
            with conn.transaction():
                _load_one(conn, client, subs, stats, row.cik, row.accession, row.date_filed)
        except (httpx.HTTPError, RetryExhausted):
            stats.errors += 1
    return stats


def ingest_day(conn: psycopg.Connection, client: HttpClient, day: date) -> tuple[str, LoadStats]:
    """Status: ok, partial (some filings failed), or pending (index not published yet)."""
    result = fetch_daily_index(client, day)
    if result.status != "ok":
        return result.status, LoadStats()
    stats = ingest_filings(conn, client, result.rows)
    return ("partial" if stats.errors else "ok"), stats


def catch_up(conn: psycopg.Connection, client: HttpClient, today: date, max_days: int = 10) -> list[tuple[date, str, LoadStats]]:
    """Ingest each of the last `max_days` days before `today` without an ok run.

    Each day is attempted independently: a failed or partial day is recorded and retried on the next run,
    and never blocks the days after it.
    """
    from datetime import timedelta

    done = []
    for back in range(max_days, 0, -1):
        day = today - timedelta(days=back)
        job = f"form_d:{day.isoformat()}"
        if conn.execute("SELECT 1 FROM sm.runs WHERE job = %s AND status = 'ok'", (job,)).fetchone():
            continue
        try:
            status, stats = ingest_day(conn, client, day)
        except (httpx.HTTPError, RetryExhausted) as exc:
            status, stats = "failed", LoadStats(errors=1)
            error = str(exc)[:500]
        else:
            error = None
        if status != "pending":
            with conn.transaction():
                conn.execute(
                    "INSERT INTO sm.runs (job, status, finished_at, stats, error) VALUES (%s, %s, now(), %s, %s)",
                    (job, status, psycopg.types.json.Jsonb(stats.__dict__), error),
                )
        done.append((day, status, stats))
    return done
