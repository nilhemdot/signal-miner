"""Score every company with signals and store the week's ranking in sm.weekly_scores."""
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from signal_miner.score.formula import Sig, score


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
    rows = conn.execute(
        """SELECT c.company_id, c.name, s.source, s.strength, s.observed_at::date
           FROM sm.signals s JOIN sm.companies c USING (company_id)
           WHERE s.observed_at::date <= %s"""
        , (as_of,)
    ).fetchall()
    by_company: dict[str, tuple[str, list[Sig]]] = {}
    for cid, name, source, strength, observed in rows:
        by_company.setdefault(str(cid), (name, []))[1].append(Sig(source, float(strength), observed))

    scored = []
    for cid, (name, sigs) in by_company.items():
        sc = score(sigs, as_of)
        if sc <= 0:
            continue
        sources = len({s.source for s in sigs})
        latest = max(s.observed_at for s in sigs)
        scored.append((sc, sources, latest, cid, name))
    # Ties: more sources first, then the most recent signal, then company_id for a stable order.
    scored.sort(key=lambda t: (-round(t[0], 6), -t[1], -t[2].toordinal(), t[3]))

    ws = week_start(as_of)
    ranked = []
    with conn.transaction():
        conn.execute("DELETE FROM sm.weekly_scores WHERE week_start = %s", (ws,))
        for i, (sc, sources, _latest, cid, name) in enumerate(scored, start=1):
            conn.execute(
                "INSERT INTO sm.weekly_scores (week_start, company_id, score, rank, components) VALUES (%s, %s, %s, %s, '{}')",
                (ws, cid, round(sc, 4), i),
            )
            ranked.append(Ranked(cid, name, sc, i, sources))
    return ranked
