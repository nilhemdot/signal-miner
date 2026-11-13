"""Lead CSV (spec section 2). Slice 1 fills the Form D columns; later slices fill the rest."""
import csv
import io
from datetime import date
from decimal import Decimal

import psycopg

from signal_miner.score.weekly import week_start

LEAD_COLUMNS = ["rank", "score", "company", "domain", "domain_confidence", "state", "industry_group",
                "signal_types", "amount_sold", "filed_date", "first_sale_date", "open_roles", "tools_named",
                "why_now", "what_they_do", "evidence_urls"]
MIN_SCORE = 20.0

# Every Form D column comes from the signal that drove the score, so the CSV reflects the week as of `as_of`.
_QUERY = """
SELECT w.rank, w.score, c.name, c.domain, c.domain_confidence, c.hq_state,
       best.details, best.observed_at::date,
       (SELECT string_agg(DISTINCT s.signal_type, ';' ORDER BY s.signal_type)
          FROM sm.signals s WHERE s.signal_id IN (SELECT jsonb_array_elements_text(w.components->'signal_ids')::bigint)),
       (SELECT string_agg(s.evidence_url, ';' ORDER BY s.observed_at DESC, s.evidence_url)
          FROM sm.signals s WHERE s.signal_id IN (SELECT jsonb_array_elements_text(w.components->'signal_ids')::bigint))
FROM sm.weekly_scores w
JOIN sm.companies c USING (company_id)
LEFT JOIN sm.signals best ON best.signal_id = (w.components->'best'->'form_d'->>'signal_id')::bigint
WHERE w.week_start = %s AND w.score >= %s
ORDER BY w.rank
"""


def _money_short(v) -> str:
    v = float(v)
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def why_now(sold, filed: date) -> str:
    if sold and sold > 0:
        return f"Raised {_money_short(sold)} (Form D filed {filed.isoformat()})"
    return f"Filed Form D, $0 sold so far ({filed.isoformat()})"


def _fmt_amount(v) -> str:
    if v is None:
        return ""
    return str(int(v)) if v == int(v) else str(v)


def form_d_leads_csv(conn: psycopg.Connection, as_of: date) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(LEAD_COLUMNS)
    for (rank, sc, name, domain, conf, state, details, filed, types, urls) in conn.execute(
        _QUERY, (week_start(as_of), MIN_SCORE)
    ):
        d = details or {}
        sold = Decimal(d["amount_sold"]) if d.get("amount_sold") is not None else None
        w.writerow([rank, f"{sc:.2f}", name, domain or "", conf, d.get("state") or state or "",
                    d.get("industry_group") or "", types or "", _fmt_amount(sold),
                    filed.isoformat() if filed else "", d.get("first_sale") or "",
                    "", "", why_now(sold, filed) if filed else "", "", urls or ""])
    return buf.getvalue()
