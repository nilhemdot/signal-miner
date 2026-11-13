"""Score every company with in-window signals and store the week's ranking in sm.weekly_scores.

Only signals observed on or before `as_of` and inside their source window count, so a week's
ranking and CSV are the same whenever they are rebuilt.
"""
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg
from psycopg.types.json import Jsonb

from signal_miner.score.formula import WINDOW_DAYS, Sig, best_per_source, in_window, score


@dataclass(frozen=True)
class Ranked:
    company_id: str
    name: str
    score: float
    rank: int
    sources: int


def week_start(as_of: date) -> date:
    return as_of - timedelta(days=as_of.weekday())


def rank_week(conn: psycopg.Connection, as_of: date) -> list[Ranked]:
    oldest = as_of - timedelta(days=max(WINDOW_DAYS.values()))
    rows = conn.execute(
        """SELECT c.company_id, c.name, s.signal_id, s.source, s.strength, s.observed_at::date
           FROM sm.signals s JOIN sm.companies c USING (company_id)
           WHERE s.observed_at::date BETWEEN %s AND %s""",
        (oldest, as_of),
    ).fetchall()
    by_company: dict[str, tuple[str, list[Sig]]] = {}
    for cid, name, sid, source, strength, observed in rows:
        sig = Sig(source, float(strength), observed, ref=sid)
        if in_window(sig, as_of):
            by_company.setdefault(str(cid), (name, []))[1].append(sig)

    scored = []
    for cid, (name, sigs) in by_company.items():
        sc = score(sigs, as_of)
        if sc <= 0:
            continue
        best = best_per_source(sigs, as_of)
        components = {
            "best": {src: {"signal_id": sig.ref, "p": round(p, 6)} for src, (p, sig) in sorted(best.items())},
            "signal_ids": sorted(s.ref for s in sigs),
        }
        latest = max(s.observed_at for s in sigs)
        scored.append((sc, len(best), latest, cid, name, components))
    # Ties: more sources first, then the most recent in-window signal, then company_id for a stable order.
    # Scores compare at the 4 decimals they are stored with.
    scored.sort(key=lambda t: (-round(t[0], 4), -t[1], -t[2].toordinal(), t[3]))

    ws = week_start(as_of)
    ranked = []
    with conn.transaction():
        conn.execute("DELETE FROM sm.weekly_scores WHERE week_start = %s", (ws,))
        for i, (sc, sources, _latest, cid, name, components) in enumerate(scored, start=1):
            conn.execute(
                "INSERT INTO sm.weekly_scores (week_start, company_id, score, rank, components) VALUES (%s, %s, %s, %s, %s)",
                (ws, cid, round(sc, 4), i, Jsonb(components)),
            )
            ranked.append(Ranked(cid, name, sc, i, sources))
    return ranked
