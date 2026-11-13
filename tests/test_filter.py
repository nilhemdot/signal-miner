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


def test_names_that_merely_contain_letters_are_not_excluded():
    assert passes_startup_filter(dataclasses.replace(TECH, entity_name="Helpful Software Inc"))  # "lp" inside a word
    assert passes_startup_filter(dataclasses.replace(TECH, entity_name="Refunding Labs Inc"))    # "fund" inside a word
