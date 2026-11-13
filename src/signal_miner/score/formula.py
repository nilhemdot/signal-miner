"""Signal strength and company score (spec section 7)."""
import math
from dataclasses import dataclass
from datetime import date

WEIGHTS = {"form_d": 0.6, "jobs": 0.5, "hn": 0.4, "github": 0.3}
WINDOW_DAYS = {"form_d": 28, "jobs": 28, "hn": 45, "github": 28}
HALF_LIFE_DAYS = 14
STALE_FIRST_SALE_DAYS = 90


@dataclass(frozen=True)
class Sig:
    source: str
    strength: float
    observed_at: date


def amount_factor(sold) -> float:
    """a = clamp(log10(sold / 250,000) / 2, 0, 1)."""
    sold = float(sold or 0)
    if sold <= 0:
        return 0.0
    return min(1.0, max(0.0, math.log10(sold / 250_000) / 2))


def form_d_strength(signal_type: str, sold, filed: date, first_sale: date | None) -> float:
    a = amount_factor(sold)
    if signal_type == "form_d_raised":
        s = 0.5 + 0.5 * a
    elif signal_type == "form_d_filed":
        s = 0.4
    elif signal_type == "form_d_amend":
        s = 0.3 + 0.3 * a
    else:
        raise ValueError(f"not a Form D signal type: {signal_type!r}")
    if first_sale is not None and (filed - first_sale).days > STALE_FIRST_SALE_DAYS:
        s /= 2
    return s


def score(signals: list[Sig], as_of: date) -> float:
    """100 * (1 - prod(1 - p_src)), where p_src is the best weighted, decayed signal per source."""
    best: dict[str, float] = {}
    for sig in signals:
        age = (as_of - sig.observed_at).days
        if age < 0 or age > WINDOW_DAYS[sig.source]:
            continue
        p = WEIGHTS[sig.source] * min(1.0, max(0.0, sig.strength)) * 0.5 ** (age / HALF_LIFE_DAYS)
        best[sig.source] = max(best.get(sig.source, 0.0), p)
    remaining = 1.0
    for p in best.values():
        remaining *= 1.0 - p
    return 100.0 * (1.0 - remaining)
