"""Startup filter for Form D issuers (spec section 4.1)."""
import re

from signal_miner.sec.formd import FormD

STARTUP_INDUSTRIES = frozenset({"Computers", "Telecommunications", "Other Technology", "Business Services"})
YOUNG_BUCKETS = frozenset({"withinFiveYears", "yetToBeFormed"})
EXCLUDED_ENTITY_TYPES = frozenset({"Limited Partnership"})
# Whole words only, so "Refunding" and "Helpful" pass.
_FUND_NAME = re.compile(r"\b(fund|spv|lp|l\.p\.)\b", re.IGNORECASE)


def passes_startup_filter(f: FormD) -> bool:
    return (
        f.year_of_inc_bucket in YOUNG_BUCKETS
        and f.industry_group in STARTUP_INDUSTRIES
        and f.entity_type not in EXCLUDED_ENTITY_TYPES
        and not _FUND_NAME.search(f.entity_name)
    )
