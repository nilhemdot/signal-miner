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
    ref: object = None  # caller's handle on the stored signal


def in_window(sig: Sig, as_of: date) -> bool:
    age = (as_of - sig.observed_at).days
    return 0 <= age <= WINDOW_DAYS[sig.source]


def weighted(sig: Sig, as_of: date) -> float:
    age = (as_of - sig.observed_at).days
    return WEIGHTS[sig.source] * min(1.0, max(0.0, sig.strength)) * 0.5 ** (age / HALF_LIFE_DAYS)


def best_per_source(signals: list[Sig], as_of: date) -> dict[str, tuple[float, Sig]]:
    best: dict[str, tuple[float, Sig]] = {}
    for sig in signals:
        if not in_window(sig, as_of):
            continue
        p = weighted(sig, as_of)
        if sig.source not in best or p > best[sig.source][0]:
            best[sig.source] = (p, sig)
    return best


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
    remaining = 1.0
    for p, _sig in best_per_source(signals, as_of).values():
        remaining *= 1.0 - p
    return 100.0 * (1.0 - remaining)
