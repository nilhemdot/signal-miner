"""Lead CSV (spec section 2). Slice 1 fills the Form D columns; later slices fill the rest."""
import csv
import io
from datetime import date

import psycopg

from signal_miner.score.weekly import week_start

LEAD_COLUMNS = ["rank", "score", "company", "domain", "domain_confidence", "state", "industry_group",
                "signal_types", "amount_sold", "filed_date", "first_sale_date", "open_roles", "tools_named",
                "why_now", "what_they_do", "evidence_urls"]
MIN_SCORE = 20.0

_QUERY = """
SELECT w.rank, w.score, c.name, c.domain, c.domain_confidence,
       o.state, o.industry_group, o.amount_sold, o.latest_filed_date, o.date_of_first_sale,
       (SELECT string_agg(DISTINCT s.signal_type, ';' ORDER BY s.signal_type) FROM sm.signals s WHERE s.company_id = c.company_id),
       (SELECT string_agg(s.evidence_url, ';' ORDER BY s.observed_at DESC, s.evidence_url) FROM sm.signals s WHERE s.company_id = c.company_id)
FROM sm.weekly_scores w
JOIN sm.companies c USING (company_id)
-- The offering shown is the company's largest raise among its newest filings.
JOIN LATERAL (
    SELECT * FROM sm.form_d_offerings o WHERE o.company_id = c.company_id
    ORDER BY o.latest_filed_date DESC, o.amount_sold DESC NULLS LAST, o.file_number LIMIT 1
) o ON true
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
    for (rank, sc, name, domain, conf, state, industry, sold, filed, first_sale, types, urls) in conn.execute(
        _QUERY, (week_start(as_of), MIN_SCORE)
    ):
        w.writerow([rank, f"{sc:.2f}", name, domain or "", conf, state or "", industry or "", types or "",
                    _fmt_amount(sold), filed.isoformat(), first_sale.isoformat() if first_sale else "",
                    "", "", why_now(sold, filed), "", urls or ""])
    return buf.getvalue()
