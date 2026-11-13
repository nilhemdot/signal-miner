"""AC 3: Form D XML parsing on real filings."""
from datetime import date

import pytest

from signal_miner.sec.formd import FormDParseError, parse_form_d
from tests.sec_fixtures import FIX


def _load(name):
    return parse_form_d((FIX / name).read_bytes())


def test_amendment_with_indefinite_offering_amount():
    f = _load("2153109_000215310926000002.xml")
    assert f.submission_type == "D/A"
    assert f.is_amendment
    assert f.previous_accession == "0002153109-26-000001"
    assert f.offering_amount is None and f.offering_indefinite
    assert f.amount_sold == 64_450_000
    assert f.date_of_first_sale == date(2026, 10, 30)
    assert not f.first_sale_yet_to_occur
    assert f.entity_type == "Limited Partnership"
    assert f.industry_group == "Pooled Investment Fund"
    assert f.revenue_range == "Decline to Disclose"
    assert f.year_of_inc_bucket == "withinFiveYears"
    assert f.related_person_count == 2
    assert f.state == "FL"


def test_first_sale_yet_to_occur_sets_flag():
    f = _load("2153109_000215310926000001.xml")
    assert f.first_sale_yet_to_occur
    assert f.date_of_first_sale is None
    assert f.amount_sold == 0
    assert f.previous_accession is None


def test_operating_company_fields():
    f = _load("2162824_000216282426000002.xml")
    assert f.entity_name == "Agency Tool Co"
    assert f.industry_group == "Other Technology"
    assert f.offering_amount is not None and not f.offering_indefinite
    assert f.amount_sold == 8_736_117


def test_over_five_years_bucket():
    assert _load("1929908_000192990826000003.xml").year_of_inc_bucket == "overFiveYears"


@pytest.mark.parametrize("payload", [b"", b"<edgarSubmission><primaryIssuer>", b"<html>Not XML</html>", b"\x00\xff"])
def test_malformed_xml_raises_a_typed_error(payload):
    with pytest.raises(FormDParseError):
        parse_form_d(payload)


def test_parser_never_returns_person_names():
    f = _load("2153109_000215310926000002.xml")
    assert not any(hasattr(f, a) for a in ("related_persons", "signature_name", "first_name"))
