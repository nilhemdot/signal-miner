"""EDGAR daily form index: list, fetch and parse form.YYYYMMDD.idx files."""
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from signal_miner.http.client import HttpClient

BASE = "https://www.sec.gov/Archives/edgar/daily-index"
FORM_D_TYPES = frozenset({"D", "D/A"})

# Columns are matched from the right (file, date, cik); the rest splits on 2+ spaces into form type and company.
_TAIL = re.compile(r"^(?P<left>.*?)\s+(?P<cik>\d+)\s+(?P<date>\d{8})\s+(?P<file>edgar/data/\S+)\s*$")
_FILE_ACCESSION = re.compile(r"(\d{10}-\d{2}-\d{6})\.txt$")


@dataclass(frozen=True)
class IndexRow:
    form_type: str
    company: str
    cik: int
    date_filed: date
    accession: str


@dataclass
class IndexResult:
    status: str  # "ok" (rows may be empty on weekends and holidays) or "pending" (not published yet)
    rows: list[IndexRow] = field(default_factory=list)


def parse_form_index(text: str, keep: frozenset[str] | None = None) -> list[IndexRow]:
    rows = []
    in_body = False
    for line in text.splitlines():
        if not in_body:
            in_body = line.startswith("---")
            continue
        m = _TAIL.match(line)
        if not m:
            continue
        parts = re.split(r"\s{2,}", m["left"].strip(), maxsplit=1)
        if len(parts) != 2:
            continue
        form_type, company = parts[0].strip(), parts[1].strip()
        if keep is not None and form_type not in keep:
            continue
        acc = _FILE_ACCESSION.search(m["file"])
        if not acc:
            continue
        rows.append(IndexRow(form_type, company, int(m["cik"]), datetime.strptime(m["date"], "%Y%m%d").date(), acc[1]))
    return rows


def _quarter_dir(day: date) -> str:
    return f"{BASE}/{day.year}/QTR{(day.month - 1) // 3 + 1}"


def _next_quarter_start(day: date) -> date:
    q = (day.month - 1) // 3
    return date(day.year + 1, 1, 1) if q == 3 else date(day.year, 3 * (q + 1) + 1, 1)


def listed_form_index_dates(client: HttpClient, day: date) -> set[date]:
    """Dates with a form index in the quarter containing `day`. An unpublished quarter lists nothing."""
    resp = client.get(f"{_quarter_dir(day)}/index.json")
    if resp.status_code in (403, 404):  # EDGAR answers 403 for a directory that does not exist yet
        return set()
    resp.raise_for_status()
    out = set()
    for item in resp.json()["directory"]["item"]:
        m = re.fullmatch(r"form\.(\d{8})\.idx", item.get("name", ""))
        if m:
            out.add(datetime.strptime(m[1], "%Y%m%d").date())
    return out


def fetch_daily_index(client: HttpClient, day: date, keep: frozenset[str] = FORM_D_TYPES) -> IndexResult:
    listed = listed_form_index_dates(client, day)
    if day not in listed:
        # A date before a later published index was a weekend or holiday; otherwise it is not published yet.
        if listed and day < max(listed):
            return IndexResult("ok")
        if listed_form_index_dates(client, _next_quarter_start(day)):
            return IndexResult("ok")  # last days of a quarter: the next file landed in the next quarter
        return IndexResult("pending")
    resp = client.get(f"{_quarter_dir(day)}/form.{day:%Y%m%d}.idx")
    resp.raise_for_status()
    return IndexResult("ok", parse_form_index(resp.text, keep))
