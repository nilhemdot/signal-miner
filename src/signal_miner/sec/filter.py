"""Startup filter for Form D issuers (spec section 4.1)."""
import re

from signal_miner.sec.formd import FormD

STARTUP_INDUSTRIES = frozenset({"Computers", "Telecommunications", "Other Technology", "Business Services"})
YOUNG_BUCKETS = frozenset({"withinFiveYears", "yetToBeFormed"})
EXCLUDED_ENTITY_TYPES = frozenset({"Limited Partnership"})
US_STATES = frozenset("""AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM
NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY""".split())
# Spec 4.1: the name must not contain "fund" or "spv", and must not be an LP ("LP", "L.P.", "L P").
_FUND_NAME = re.compile(r"fund|spv|\bl\.?\s?p\.?(?!\w)", re.IGNORECASE)


def passes_startup_filter(f: FormD) -> bool:
    return (
        f.year_of_inc_bucket in YOUNG_BUCKETS
        and f.industry_group in STARTUP_INDUSTRIES
        and f.entity_type not in EXCLUDED_ENTITY_TYPES
        and not _FUND_NAME.search(f.entity_name)
        and (f.state or "").upper() in US_STATES
    )
