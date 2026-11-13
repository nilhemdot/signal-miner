"""Regression tests for the slice 1 review: ordering, amendments, errors, co-issuers, quarter ends, as_of, ties."""
import csv
import io
from datetime import date

import pytest
import respx

from signal_miner.http.client import FakeClock
from signal_miner.http.sec import make_sec_client
from signal_miner.output.leads import form_d_leads_csv
from signal_miner.score.formula import amount_factor
from signal_miner.score.weekly import rank_week
from signal_miner.sec.index import IndexRow, fetch_daily_index
from signal_miner.sec.loader import catch_up, ingest_day, ingest_filings
from tests.fake_sec import FakeSec, index_text, make_xml
from tests.sec_fixtures import UA_ENV

DAY = date(2026, 11, 6)
CIK = 7000001


@pytest.fixture
def db(writer):
    for t in ("signals", "weekly_scores", "form_d_offerings", "form_d_filings", "company_aliases", "companies", "runs"):
        writer.execute(f"DELETE FROM sm.{t}")
    writer.commit()
    return writer


def _run(db, sec: FakeSec, rows):
    with respx.mock as router:
        sec.mount(router)
        return ingest_filings(db, make_sec_client(env=UA_ENV, clock=FakeClock()), rows)


def _offering(db, cik=CIK):
    return db.execute("SELECT latest_accession, amount_sold, latest_form_type FROM sm.form_d_offerings WHERE cik = %s", (cik,)).fetchall()


def _signals(db):
    return db.execute("SELECT signal_type, source_ref, round(strength::numeric, 3) FROM sm.signals ORDER BY source_ref").fetchall()


# Same-day ordering ---------------------------------------------------------------------------

D_ACC, DA_ACC = "0007000001-26-000001", "0001213900-26-000500"  # the D/A was filed by an agent: lower prefix


def _same_day_sec():
    sec = FakeSec()
    sec.add(CIK, D_ACC, "D", "021-1", DAY, make_xml(CIK, sold=0))
    sec.add(CIK, DA_ACC, "D/A", "021-1", DAY, make_xml(CIK, amendment=True, sold=2_000_000, previous=D_ACC))
    return sec


@pytest.mark.parametrize("order", [[D_ACC, DA_ACC], [DA_ACC, D_ACC]])
def test_same_day_amendment_wins_whatever_the_accession_prefix(db, order):
    sec = _same_day_sec()
    for acc in order:
        _run(db, sec, [IndexRow("D/A" if acc == DA_ACC else "D", "x", CIK, DAY, acc)])
    assert _offering(db) == [(DA_ACC, 2_000_000, "D/A")]


# Amendment signals ---------------------------------------------------------------------------

def test_amendment_without_previous_accession_compares_with_stored_offering(db):
    sec = FakeSec()
    sec.add(CIK, "0007000001-26-000001", "D", "021-1", date(2026, 10, 15), make_xml(CIK, sold=5_000_000))
    sec.add(CIK, "0007000001-26-000002", "D/A", "021-1", DAY, make_xml(CIK, amendment=True, sold=5_000_000))
    _run(db, sec, [IndexRow("D", "x", CIK, date(2026, 10, 15), "0007000001-26-000001")])
    _run(db, sec, [IndexRow("D/A", "x", CIK, DAY, "0007000001-26-000002")])
    assert [s[0] for s in _signals(db)] == ["form_d_raised"]  # equal sold: no amend signal


def test_amendment_with_a_real_increase_fires_amend(db):
    sec = FakeSec()
    sec.add(CIK, "0007000001-26-000001", "D", "021-1", date(2026, 10, 15), make_xml(CIK, sold=1_000_000))
    sec.add(CIK, "0007000001-26-000002", "D/A", "021-1", DAY,
            make_xml(CIK, amendment=True, sold=25_000_000, previous="0007000001-26-000001"))
    _run(db, sec, [IndexRow("D/A", "x", CIK, DAY, "0007000001-26-000002")])
    sigs = _signals(db)
    assert sigs[1][:2] == ("form_d_amend", "0007000001-26-000002")
    assert float(sigs[1][2]) == pytest.approx(0.3 + 0.3 * amount_factor(25_000_000), abs=1e-3)


def test_amendment_with_no_prior_state_fires_nothing(db):
    sec = FakeSec()
    sec.add(CIK, "0007000001-26-000002", "D/A", "021-1", DAY, make_xml(CIK, amendment=True, sold=9_000_000))
    _run(db, sec, [IndexRow("D/A", "x", CIK, DAY, "0007000001-26-000002")])
    assert _signals(db) == []


def test_original_with_nothing_sold_fires_form_d_filed(db):
    sec = FakeSec()
    sec.add(CIK, "0007000001-26-000001", "D", "021-1", DAY, make_xml(CIK, sold=0))
    _run(db, sec, [IndexRow("D", "x", CIK, DAY, "0007000001-26-000001")])
    assert [(t, r, float(s)) for t, r, s in _signals(db)] == [("form_d_filed", "0007000001-26-000001", 0.4)]


# Errors --------------------------------------------------------------------------------------

def test_one_failing_filing_is_counted_and_the_rest_load(db):
    sec = FakeSec()
    sec.add(CIK, "0007000001-26-000001", "D", "021-1", DAY, make_xml(CIK))
    sec.add(7000002, "0007000002-26-000001", "D", "021-2", DAY, make_xml(7000002, name="Second Startup Inc"))
    sec.errors["0007000001-26-000001"] = 500
    stats = _run(db, sec, [IndexRow("D", "x", CIK, DAY, "0007000001-26-000001"),
                           IndexRow("D", "y", 7000002, DAY, "0007000002-26-000001")])
    assert stats.errors == 1 and stats.filings == 1
    assert _offering(db, 7000002) != []


def test_catch_up_records_a_partial_day_and_moves_on(db):
    sec = FakeSec()
    d5, d6 = date(2026, 11, 5), date(2026, 11, 6)
    sec.listings["2026/QTR4"] = ["form.20261105.idx", "form.20261106.idx"]
    sec.indexes["20261105"] = index_text([("D", "Bad Inc", CIK, d5, "0007000001-26-000001")])
    sec.indexes["20261106"] = index_text([("D", "Good Inc", 7000002, d6, "0007000002-26-000001")])
    sec.add(CIK, "0007000001-26-000001", "D", "021-1", d5, make_xml(CIK))
    sec.add(7000002, "0007000002-26-000001", "D", "021-2", d6, make_xml(7000002))
    sec.errors["0007000001-26-000001"] = 500
    with respx.mock as router:
        sec.mount(router)
        out = catch_up(db, make_sec_client(env=UA_ENV, clock=FakeClock()), today=date(2026, 11, 7), max_days=2)
    assert [(d, s) for d, s, _ in out] == [(d5, "partial"), (d6, "ok")]
    runs = dict(db.execute("SELECT job, status FROM sm.runs").fetchall())
    assert runs == {"form_d:2026-11-05": "partial", "form_d:2026-11-06": "ok"}


# Co-issuers ----------------------------------------------------------------------------------

def test_co_issuer_rows_load_one_filing_under_the_primary_issuer(db):
    sec = FakeSec()
    acc = "0007000001-26-000009"
    sec.add(CIK, acc, "D", "021-9", DAY, make_xml(CIK))
    sec.subs[7000099] = [(acc, "D", "021-9-01", DAY.isoformat())]  # the co-issuer's own file number
    stats = _run(db, sec, [IndexRow("D", "Co Issuer LLC", 7000099, DAY, acc), IndexRow("D", "Primary Inc", CIK, DAY, acc)])
    assert stats.filings == 1
    assert db.execute("SELECT cik, file_number FROM sm.form_d_offerings").fetchall() == [(CIK, "021-9")]
    assert db.execute("SELECT cik FROM sm.form_d_filings").fetchall() == [(CIK,)]


# Quarter ends --------------------------------------------------------------------------------

@pytest.mark.parametrize("next_quarter_published, expected", [(True, "ok"), (False, "pending")])
def test_weekend_at_quarter_end(next_quarter_published, expected):
    sec = FakeSec()
    sec.listings["2028/QTR3"] = ["form.20280928.idx", "form.20280929.idx"]
    if next_quarter_published:
        sec.listings["2028/QTR4"] = ["form.20281002.idx"]
    with respx.mock as router:
        sec.mount(router)
        result = fetch_daily_index(make_sec_client(env=UA_ENV, clock=FakeClock()), date(2028, 9, 30))
    assert result.status == expected and result.rows == []


# As-of reproducibility and ties ---------------------------------------------------------------

def test_csv_for_a_fixed_date_ignores_later_filings_and_expired_signals(db):
    sec = FakeSec()
    sec.add(CIK, "0007000001-26-000001", "D", "021-1", DAY, make_xml(CIK, sold=1_000_000))
    _run(db, sec, [IndexRow("D", "x", CIK, DAY, "0007000001-26-000001")])
    as_of = date(2026, 11, 9)
    rank_week(db, as_of)
    before = form_d_leads_csv(db, as_of)
    sec.add(CIK, "0007000001-26-000002", "D/A", "021-1", date(2026, 11, 20),
            make_xml(CIK, amendment=True, sold=3_000_000, previous="0007000001-26-000001"))
    _run(db, sec, [IndexRow("D/A", "x", CIK, date(2026, 11, 20), "0007000001-26-000002")])
    rank_week(db, as_of)
    assert form_d_leads_csv(db, as_of) == before
    row = next(csv.DictReader(io.StringIO(before)))
    assert row["amount_sold"] == "1000000" and row["signal_types"] == "form_d_raised"
    assert row["evidence_urls"].count("https://") == 1


def _company(db, name):
    cid = db.execute("INSERT INTO sm.companies (name, normalized_name) VALUES (%s, %s) RETURNING company_id", (name, name.lower())).fetchone()[0]
    return cid


def _signal(db, cid, source, strength, observed, ref):
    db.execute("""INSERT INTO sm.signals (company_id, source, signal_type, source_ref, observed_at, strength, evidence_url)
                  VALUES (%s, %s, 't', %s, %s, %s, 'https://example.com/' || %s)""", (cid, source, ref, observed, strength, ref))


def test_ties_break_on_sources_then_recency_then_company_id(db):
    as_of = date(2026, 11, 20)
    two_sources = _company(db, "Two Sources")
    _signal(db, two_sources, "form_d", 0.25, as_of, "a1")
    _signal(db, two_sources, "hn", 0.15 / 0.85 / 0.4, as_of, "a2")        # 1 - 0.85 * (1 - 0.15/0.85) = 0.30
    recent = [_company(db, "Recent A"), _company(db, "Recent B")]
    for i, cid in enumerate(recent):
        _signal(db, cid, "form_d", 0.5, as_of, f"r{i}")                    # 0.6 * 0.5 = 0.30
    older = _company(db, "Older")
    _signal(db, older, "form_d", 1.0, date(2026, 11, 6), "o1")             # 0.6 * 1.0 * 0.5 = 0.30
    db.commit()
    ranked = rank_week(db, as_of)
    assert all(round(r.score, 4) == 30.0 for r in ranked)
    by_id = sorted(recent, key=str)
    names = {str(c): n for c, n in ((recent[0], "Recent A"), (recent[1], "Recent B"))}
    assert [r.name for r in ranked] == ["Two Sources", names[str(by_id[0])], names[str(by_id[1])], "Older"]
