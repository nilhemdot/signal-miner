"""AC 4 and the Form D-only ranked list: loading, offerings, idempotence, signals, ranking, CSV."""
import csv
import io
from datetime import date

import pytest
import respx

from signal_miner.http.client import FakeClock
from signal_miner.http.sec import make_sec_client
from signal_miner.output.leads import LEAD_COLUMNS, form_d_leads_csv
from signal_miner.score.weekly import rank_week
from signal_miner.sec.index import IndexRow
from signal_miner.sec.loader import ingest_day, ingest_filings
from tests.sec_fixtures import UA_ENV, mock_sec

DAY = date(2026, 11, 6)
AS_OF = date(2026, 11, 13)


def _row(form, cik, acc, company="x", day=DAY):
    return IndexRow(form, company, cik, day, acc)


ROWS = [
    _row("D/A", 2153109, "0002153109-26-000002", "1789 Capital Inception III LP"),
    _row("D", 2163963, "0002163963-26-000001", "1DER, Inc."),
    _row("D", 2163963, "0002163963-26-000002", "1DER, Inc."),
    _row("D", 2162824, "0002162824-26-000001", "Agency Tool Co"),
    _row("D", 2162824, "0002162824-26-000002", "Agency Tool Co"),
    _row("D", 2112552, "0002112552-26-000002", "AlphaLitigation Inc."),
    _row("D", 2163900, "0002163900-26-000001", "Boring Radiology, Inc."),
    _row("D", 9999999, "0009999999-26-000001", "Broken Inc."),   # malformed XML fixture
    _row("D", 8888888, "0008888888-26-000001", "Missing Inc."),  # 404
]


@pytest.fixture
def clean(writer):
    for t in ("signals", "weekly_scores", "form_d_offerings", "form_d_filings", "company_aliases", "companies"):
        writer.execute(f"DELETE FROM sm.{t}")
    writer.commit()
    return writer


@pytest.fixture
def loaded(clean):
    with respx.mock as router:
        mock_sec(router)
        client = make_sec_client(env=UA_ENV, clock=FakeClock())
        stats = ingest_filings(clean, client, ROWS)
    return clean, stats


def test_malformed_and_missing_filings_are_counted_not_raised(loaded):
    _, stats = loaded
    assert stats.malformed == 1
    assert stats.missing == 1


def test_amendment_and_original_share_one_offering_with_da_latest(loaded):
    conn, _ = loaded
    rows = conn.execute(
        "SELECT file_number, latest_accession, offering_indefinite, amount_sold FROM sm.form_d_offerings WHERE cik = 2153109"
    ).fetchall()
    assert rows == [("021-596118", "0002153109-26-000002", True, 64_450_000)]
    # The original D was fetched through previousAccessionNumber and stored as a filing.
    forms = conn.execute("SELECT accession, form_type, file_number FROM sm.form_d_filings WHERE cik = 2153109 ORDER BY accession").fetchall()
    assert forms == [("0002153109-26-000001", "D", "021-596118"), ("0002153109-26-000002", "D/A", "021-596118")]


def test_two_offerings_same_day_stay_separate(loaded):
    conn, _ = loaded
    n = conn.execute("SELECT count(*) FROM sm.form_d_offerings WHERE cik = 2163963").fetchone()[0]
    assert n == 2


def test_reloading_the_same_filings_changes_zero_rows(loaded):
    conn, first = loaded
    assert first.changed > 0
    with respx.mock as router:
        mock_sec(router)
        client = make_sec_client(env=UA_ENV, clock=FakeClock())
        second = ingest_filings(conn, client, ROWS)
    assert second.changed == 0


def test_only_filtered_offerings_get_companies_and_signals(loaded):
    conn, _ = loaded
    names = {r[0] for r in conn.execute("SELECT name FROM sm.companies")}
    assert names == {"Agency Tool Co", "AlphaLitigation Inc.", "Boring Radiology, Inc."}
    types = conn.execute("SELECT signal_type, count(*) FROM sm.signals GROUP BY 1").fetchall()
    assert types == [("form_d_raised", 4)]  # Agency Tool Co has two offerings
    urls = [r[0] for r in conn.execute("SELECT evidence_url FROM sm.signals")]
    assert all(u.startswith("https://www.sec.gov/Archives/edgar/data/") for u in urls)


def test_rank_week_orders_by_score(loaded):
    conn, _ = loaded
    ranked = rank_week(conn, AS_OF)
    assert [r.name for r in ranked] == ["Agency Tool Co", "AlphaLitigation Inc.", "Boring Radiology, Inc."]
    assert [round(r.score, 2) for r in ranked] == [37.58, 30.31, 22.76]
    assert [r.rank for r in ranked] == [1, 2, 3]
    stored = conn.execute("SELECT count(*) FROM sm.weekly_scores WHERE week_start = '2026-11-09'").fetchone()[0]
    assert stored == 3


def test_form_d_lead_csv(loaded):
    conn, _ = loaded
    rank_week(conn, AS_OF)
    text = form_d_leads_csv(conn, AS_OF)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0].keys()) == LEAD_COLUMNS
    assert [r["company"] for r in rows] == ["Agency Tool Co", "AlphaLitigation Inc.", "Boring Radiology, Inc."]
    top = rows[0]
    assert top["rank"] == "1" and top["score"] == "37.58"
    assert top["amount_sold"] == "8736117" and top["filed_date"] == "2026-11-06" and top["first_sale_date"] == "2026-10-26"
    assert top["signal_types"] == "form_d_raised"
    assert top["evidence_urls"].startswith("https://www.sec.gov/Archives/edgar/data/2162824/")
    assert form_d_leads_csv(conn, AS_OF) == text  # deterministic


@respx.mock
def test_ingest_day_on_a_weekend_is_ok_with_zero_filings(clean):
    mock_sec(respx.mock)
    client = make_sec_client(env=UA_ENV, clock=FakeClock())
    status, stats = ingest_day(clean, client, date(2026, 11, 7))
    assert status == "ok" and stats.filings == 0


def test_why_now_names_the_raise(loaded):
    conn, _ = loaded
    rank_week(conn, AS_OF)
    top = next(csv.DictReader(io.StringIO(form_d_leads_csv(conn, AS_OF))))
    assert top["why_now"] == "Raised $8.7M (Form D filed 2026-11-06)"


def test_catch_up_ingests_missed_days_once(clean):
    from signal_miner.sec.loader import catch_up

    with respx.mock as router:
        mock_sec(router)
        client = make_sec_client(env=UA_ENV, clock=FakeClock())
        first = catch_up(clean, client, today=date(2026, 11, 9), max_days=3)   # Nov 6, 7, 8
        assert [(d.isoformat(), s) for d, s, _ in first] == [("2026-11-06", "ok"), ("2026-11-07", "ok"), ("2026-11-08", "ok")]
        s = first[0][2]  # only 16 of the day's 288 filings have XML fixtures; the rest 404
        assert s.filings + s.missing + s.malformed >= 288 and s.filings >= 10
        again = catch_up(clean, client, today=date(2026, 11, 9), max_days=3)
        assert again == []


def test_an_older_filing_arriving_late_never_overwrites_the_offering(loaded):
    conn, _ = loaded
    with respx.mock as router:
        mock_sec(router)
        client = make_sec_client(env=UA_ENV, clock=FakeClock())
        ingest_filings(conn, client, [_row("D", 2153109, "0002153109-26-000001", day=date(2026, 9, 2))])
    latest = conn.execute("SELECT latest_accession, amount_sold FROM sm.form_d_offerings WHERE cik = 2153109").fetchone()
    assert latest == ("0002153109-26-000002", 64_450_000)
