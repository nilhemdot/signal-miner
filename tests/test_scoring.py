"""AC 19: scoring formula on hand-computed fixtures and 100 property cases."""
import math
import random
from datetime import date, timedelta

import pytest

from signal_miner.score.formula import Sig, form_d_strength, score

AS_OF = date(2026, 11, 13)


def _sig(source, s, age_days):
    return Sig(source=source, strength=s, observed_at=AS_OF - timedelta(days=age_days))


@pytest.mark.parametrize("sigs, expected", [
    # 1. One Form D signal with a = 1 at age 0: 100 * 0.6 * 1 = 60.
    ([_sig("form_d", 1.0, 0)], 60.0),
    # 2. Same signal 14 days old: decay halves it.
    ([_sig("form_d", 1.0, 14)], 30.0),
    # 3. Two Form D signals: only the strongest counts (max per source).
    ([_sig("form_d", 1.0, 0), _sig("form_d", 0.4, 0)], 60.0),
    # 4. Form D 1.0 and jobs 1.0 at age 0: 100 * (1 - 0.4 * 0.5) = 80.
    ([_sig("form_d", 1.0, 0), _sig("jobs", 1.0, 0)], 80.0),
    # 5. All four sources at s = 1, age 0: 100 * (1 - 0.4 * 0.5 * 0.6 * 0.7) = 91.6.
    ([_sig("form_d", 1, 0), _sig("jobs", 1, 0), _sig("hn", 1, 0), _sig("github", 1, 0)], 91.6),
    # 6. Form D outside its 28-day window, HN inside its 45-day window: 100 * 0.4 * 0.6 * 0.5^(40/14).
    ([_sig("form_d", 1.0, 29), _sig("hn", 0.6, 40)], 100 * 0.4 * 0.6 * 0.5 ** (40 / 14)),
])
def test_hand_computed_scores(sigs, expected):
    assert score(sigs, AS_OF) == pytest.approx(expected, abs=0.01)


def test_no_signals_scores_zero():
    assert score([], AS_OF) == 0.0


def test_future_signals_are_ignored():
    assert score([_sig("form_d", 1.0, -3)], AS_OF) == 0.0


@pytest.mark.parametrize("sold, first_sale_gap, expected", [
    (25_000_000, 0, 1.0),     # a = 1
    (250_000, 0, 0.5),        # a = 0
    (100_000, 0, 0.5),        # a clamps at 0
    (2_500_000, 0, 0.75),     # a = 0.5
    (25_000_000, 120, 0.5),   # first sale > 90 days before filing: half strength
])
def test_form_d_raised_strength(sold, first_sale_gap, expected):
    filed = date(2026, 11, 6)
    first_sale = filed - timedelta(days=first_sale_gap)
    assert form_d_strength("form_d_raised", sold, filed, first_sale) == pytest.approx(expected)


def test_form_d_filed_and_amend_strengths():
    filed = date(2026, 11, 6)
    assert form_d_strength("form_d_filed", 0, filed, None) == pytest.approx(0.4)
    assert form_d_strength("form_d_amend", 25_000_000, filed, filed) == pytest.approx(0.6)


@pytest.mark.parametrize("seed", range(100))
def test_property_bounded_and_monotone(seed):
    rng = random.Random(seed)
    sources = ["form_d", "jobs", "hn", "github"]
    sigs = [_sig(rng.choice(sources), rng.random(), rng.randint(0, 50)) for _ in range(rng.randint(0, 8))]
    base = score(sigs, AS_OF)
    assert 0.0 <= base <= 100.0 and not math.isnan(base)
    extra = _sig(rng.choice(sources), rng.random(), rng.randint(0, 50))
    assert score(sigs + [extra], AS_OF) >= base - 1e-9


@pytest.mark.parametrize("gap, expected", [(90, 1.0), (91, 0.5)])
def test_half_strength_starts_after_90_days(gap, expected):
    filed = date(2026, 11, 6)
    assert form_d_strength("form_d_raised", 25_000_000, filed, filed - timedelta(days=gap)) == pytest.approx(expected)


@pytest.mark.parametrize("age, expected", [(28, 15.0), (29, 0.0)])
def test_form_d_window_is_28_days_inclusive(age, expected):
    # Age 28: 100 * 0.6 * 1.0 * 0.5^2 = 15.0. Age 29: outside the window.
    assert score([_sig("form_d", 1.0, age)], AS_OF) == pytest.approx(expected)
