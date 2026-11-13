"""A tiny in-memory SEC for edge cases the real fixtures do not cover."""
import re
from dataclasses import dataclass, field
from datetime import date

import httpx
import respx

from tests.sec_fixtures import FIX

TEMPLATE = (FIX / "2162824_000216282426000002.xml").read_text()  # Agency Tool Co, a real Form D


def make_xml(cik: int, name: str = "Example Startup Inc", *, amendment: bool = False, sold: int = 1_000_000,
             previous: str | None = None, first_sale: str = "2026-10-30", state: str = "CA",
             industry: str = "Other Technology") -> str:
    x = TEMPLATE
    x = re.sub(r"<cik>\d+</cik>", f"<cik>{cik:010d}</cik>", x)
    x = re.sub(r"<entityName>[^<]*</entityName>", f"<entityName>{name}</entityName>", x)
    x = re.sub(r"<submissionType>[^<]*</submissionType>", f"<submissionType>{'D/A' if amendment else 'D'}</submissionType>", x)
    x = re.sub(r"<stateOrCountry>[^<]*</stateOrCountry>", f"<stateOrCountry>{state}</stateOrCountry>", x, count=1)
    x = re.sub(r"<industryGroupType>[^<]*</industryGroupType>", f"<industryGroupType>{industry}</industryGroupType>", x)
    x = re.sub(r"<totalAmountSold>[^<]*</totalAmountSold>", f"<totalAmountSold>{sold}</totalAmountSold>", x)
    x = re.sub(r"<dateOfFirstSale>.*?</dateOfFirstSale>", f"<dateOfFirstSale><value>{first_sale}</value></dateOfFirstSale>", x, flags=re.S)
    nav = f"<isAmendment>{'true' if amendment else 'false'}</isAmendment>"
    if previous:
        nav += f"<previousAccessionNumber>{previous}</previousAccessionNumber>"
    x = re.sub(r"<newOrAmendment>.*?</newOrAmendment>", f"<newOrAmendment>{nav}</newOrAmendment>", x, flags=re.S)
    return x


@dataclass
class FakeSec:
    docs: dict[str, str] = field(default_factory=dict)            # accession -> XML (served under any CIK path)
    subs: dict[int, list[tuple[str, str, str, str]]] = field(default_factory=dict)  # cik -> (acc, form, file_no, date)
    errors: dict[str, int] = field(default_factory=dict)          # accession -> HTTP status to return
    listings: dict[str, list[str]] = field(default_factory=dict)  # "2026/QTR4" -> ["form.20261106.idx", ...]
    indexes: dict[str, str] = field(default_factory=dict)         # "20261106" -> idx text

    def add(self, cik: int, acc: str, form: str, file_no: str, filed: date, xml: str) -> None:
        self.docs[acc] = xml
        self.subs.setdefault(cik, []).append((acc, form, file_no, filed.isoformat()))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if m := re.search(r"/daily-index/(\d{4}/QTR\d)/index\.json$", path):
            if m[1] not in self.listings:
                return httpx.Response(403)
            return httpx.Response(200, json={"directory": {"item": [{"name": n} for n in self.listings[m[1]]]}})
        if m := re.search(r"/form\.(\d{8})\.idx$", path):
            return httpx.Response(200, text=self.indexes[m[1]]) if m[1] in self.indexes else httpx.Response(403)
        if m := re.search(r"/data/\d+/(\d{18})/primary_doc\.xml$", path):
            acc = f"{m[1][:10]}-{m[1][10:12]}-{m[1][12:]}"
            if acc in self.errors:
                return httpx.Response(self.errors[acc])
            return httpx.Response(200, text=self.docs[acc]) if acc in self.docs else httpx.Response(404)
        if m := re.search(r"/submissions/CIK(\d{10})\.json$", path):
            rows = sorted(self.subs.get(int(m[1]), []), key=lambda r: r[3], reverse=True)
            return httpx.Response(200, json={"filings": {"recent": {
                "accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows],
                "fileNumber": [r[2] for r in rows], "filingDate": [r[3] for r in rows]}, "files": []}})
        return httpx.Response(404)

    def mount(self, router: respx.Router) -> None:
        router.route(host__in=["www.sec.gov", "data.sec.gov"]).mock(side_effect=self.handler)


def index_text(rows: list[tuple[str, str, int, date, str]]) -> str:
    """rows: (form, company, cik, filed, accession) -> an EDGAR-style form index."""
    lines = ["Description:           Daily Index of EDGAR Dissemination Feed by Form Type", "", "",
             "Form Type   Company Name                                                  CIK",
             "      Date Filed  File Name", "-" * 141]
    for form, company, cik, filed, acc in rows:
        lines.append(f"{form:<17}{company:<62}{cik:<12}{filed:%Y%m%d}    edgar/data/{cik}/{acc}.txt")
    return "\n".join(lines) + "\n"
