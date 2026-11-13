"""AC 5: startup filter."""
import dataclasses

import pytest

from signal_miner.sec.filter import passes_startup_filter
from signal_miner.sec.formd import parse_form_d
from tests.sec_fixtures import FIX

TECH = parse_form_d((FIX / "2162824_000216282426000002.xml").read_bytes())  # Agency Tool Co, Other Technology


def test_includes_a_within_five_years_tech_issuer():
    assert TECH.year_of_inc_bucket == "withinFiveYears"
    assert passes_startup_filter(TECH)


@pytest.mark.parametrize("group", ["Computers", "Telecommunications", "Other Technology", "Business Services"])
def test_includes_each_allowed_industry(group):
    assert passes_startup_filter(dataclasses.replace(TECH, industry_group=group))


def test_includes_yet_to_be_formed():
    assert passes_startup_filter(dataclasses.replace(TECH, year_of_inc_bucket="yetToBeFormed"))


@pytest.mark.parametrize("change", [
    {"industry_group": "Pooled Investment Fund"},                 # a fund
    {"year_of_inc_bucket": "overFiveYears"},                      # older issuer
    {"year_of_inc_bucket": None},
    {"entity_type": "Limited Partnership"},                       # an LP
    {"entity_name": "Acme Fund I LP"},
    {"entity_name": "Acme SPV 3, LLC"},
    {"entity_name": "Acme Ventures LP"},
    {"industry_group": "Other"},
    {"industry_group": "Residential"},
    {"industry_group": None},
])
def test_excludes(change):
    assert not passes_startup_filter(dataclasses.replace(TECH, **change))


def test_real_fund_and_old_issuer_fixtures_are_excluded():
    assert not passes_startup_filter(parse_form_d((FIX / "2153109_000215310926000002.xml").read_bytes()))
    assert not passes_startup_filter(parse_form_d((FIX / "1929908_000192990826000003.xml").read_bytes()))


def test_lp_must_stand_alone_but_fund_and_spv_match_anywhere():
    # Spec 4.1: the name must not contain "fund" or "spv"; "lp" only counts as a separate LP marker.
    assert passes_startup_filter(dataclasses.replace(TECH, entity_name="Helpful Software Inc"))
    assert passes_startup_filter(dataclasses.replace(TECH, entity_name="Alpine Labs Inc"))
    for name in ("Refunding Labs Inc", "VentureFund Inc", "Acme Funds Inc", "Acme Partners, L.P.",
                 "Acme Partners LP", "Acme Holdings L P", "SPVCo Inc"):
        assert not passes_startup_filter(dataclasses.replace(TECH, entity_name=name)), name


@pytest.mark.parametrize("state, ok", [("CA", True), ("DC", True), ("de", True), ("X0", False), ("2M", False),
                                      ("A1", False), (None, False)])
def test_only_us_issuers_pass(state, ok):
    # EDGAR uses codes like X0 (United Kingdom) and 2M (Germany) for foreign issuers.
    assert passes_startup_filter(dataclasses.replace(TECH, state=state)) is ok
