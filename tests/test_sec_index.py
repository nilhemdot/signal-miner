"""AC 2: daily form index parsing, column-shift tolerance, non-business days."""
from datetime import date

import respx

from signal_miner.http.client import FakeClock
from signal_miner.http.sec import make_sec_client
from signal_miner.sec.index import FORM_D_TYPES, fetch_daily_index, parse_form_index
from tests.sec_fixtures import UA_ENV, fixture_text, mock_sec


def _counts(rows):
    return sum(r.form_type == "D" for r in rows), sum(r.form_type == "D/A" for r in rows)


def test_real_index_yields_172_d_and_116_da_rows():
    rows = parse_form_index(fixture_text("form.20261106.idx"), keep=FORM_D_TYPES)
    assert _counts(rows) == (172, 116)
    first = [r for r in rows if r.cik == 2163963]
    assert [r.accession for r in first] == ["0002163963-26-000001", "0002163963-26-000002"]
    assert first[0].company == "1DER, Inc."
    assert first[0].date_filed == date(2026, 11, 6)


def test_columns_shifted_three_characters_parse_the_same():
    text = fixture_text("form.20261106.idx")
    head, sep, body = text.partition("\n---")
    sep_line, _, rows = body.partition("\n")
    shifted = head + sep + sep_line + "\n" + "\n".join("   " + line if line.strip() else line for line in rows.split("\n"))
    a = parse_form_index(text, keep=FORM_D_TYPES)
    b = parse_form_index(shifted, keep=FORM_D_TYPES)
    assert a == b


def test_index_parser_keeps_only_requested_types():
    rows = parse_form_index(fixture_text("form.20261106.idx"), keep=FORM_D_TYPES)
    assert {r.form_type for r in rows} == {"D", "D/A"}


@respx.mock
def test_weekend_date_returns_ok_with_zero_rows():
    mock_sec(respx.mock)
    client = make_sec_client(env=UA_ENV, clock=FakeClock())
    result = fetch_daily_index(client, date(2026, 11, 7))  # Saturday: not listed in index.json
    assert result.status == "ok" and result.rows == []


@respx.mock
def test_business_day_index_is_fetched_and_parsed():
    mock_sec(respx.mock)
    client = make_sec_client(env=UA_ENV, clock=FakeClock())
    result = fetch_daily_index(client, date(2026, 11, 6))
    assert result.status == "ok"
    assert _counts(result.rows) == (172, 116)


@respx.mock
def test_date_after_newest_listed_file_is_pending():
    mock_sec(respx.mock)
    client = make_sec_client(env=UA_ENV, clock=FakeClock())
    assert fetch_daily_index(client, date(2026, 12, 15)).status == "pending"
