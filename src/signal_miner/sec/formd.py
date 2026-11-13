"""Parse a Form D primary_doc.xml into the offering fields the pipeline keeps.

Person names, street addresses and phone numbers are never read out of the document.
"""
import xml.etree.ElementTree as ET
from xml.parsers import expat
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation


class FormDParseError(ValueError):
    pass


@dataclass(frozen=True)
class FormD:
    submission_type: str
    is_amendment: bool
    previous_accession: str | None
    cik: int
    entity_name: str
    entity_type: str | None
    city: str | None
    state: str | None
    year_of_inc_bucket: str | None  # withinFiveYears | overFiveYears | yetToBeFormed
    industry_group: str | None
    revenue_range: str | None
    offering_amount: Decimal | None
    offering_indefinite: bool
    amount_sold: Decimal | None
    date_of_first_sale: date | None
    first_sale_yet_to_occur: bool
    related_person_count: int


class _DtdRefused(Exception):
    pass


def _refuse(*_args) -> None:
    raise _DtdRefused("DTD or entity declaration not allowed")


def _safe_fromstring(payload: bytes) -> ET.Element:
    """Refuse any DOCTYPE or entity declaration (Form D never needs one), in any encoding, then parse.

    The first expat pass sees declarations before any entity expands, so nothing is expanded.
    """
    scan = expat.ParserCreate()
    scan.StartDoctypeDeclHandler = _refuse
    scan.EntityDeclHandler = _refuse
    try:
        scan.Parse(payload, True)
    except expat.ExpatError as exc:
        raise ET.ParseError(str(exc)) from exc
    return ET.fromstring(payload)


def _text(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _bool(root: ET.Element, path: str) -> bool:
    return (_text(root, path) or "").lower() == "true"


def _money(value: str | None) -> tuple[Decimal | None, bool]:
    """Returns (amount, indefinite). 'Indefinite' gives (None, True)."""
    if value is None:
        return None, False
    if value.lower() == "indefinite":
        return None, True
    try:
        return Decimal(value), False
    except InvalidOperation as exc:
        raise FormDParseError(f"bad amount {value!r}") from exc


def parse_form_d(payload: bytes) -> FormD:
    try:
        root = _safe_fromstring(payload)
    except (ET.ParseError, _DtdRefused) as exc:
        raise FormDParseError(str(exc)) from exc
    if root.tag != "edgarSubmission":
        raise FormDParseError(f"unexpected root {root.tag!r}")

    issuer = root.find("primaryIssuer")
    offering = root.find("offeringData")
    if issuer is None or offering is None:
        raise FormDParseError("missing primaryIssuer or offeringData")

    cik_text, name = _text(issuer, "cik"), _text(issuer, "entityName")
    if not cik_text or not cik_text.isdigit() or not name:
        raise FormDParseError("missing cik or entityName")

    bucket = None
    for tag in ("withinFiveYears", "overFiveYears", "yetToBeFormed"):
        if _bool(issuer, f"yearOfInc/{tag}"):
            bucket = tag
            break

    offering_amount, indefinite = _money(_text(offering, "offeringSalesAmounts/totalOfferingAmount"))
    amount_sold, _ = _money(_text(offering, "offeringSalesAmounts/totalAmountSold"))
    first_sale = _text(offering, "typeOfFiling/dateOfFirstSale/value")
    try:
        first_sale_date = date.fromisoformat(first_sale) if first_sale else None
    except ValueError as exc:
        raise FormDParseError(f"bad dateOfFirstSale {first_sale!r}") from exc

    return FormD(
        submission_type=_text(root, "submissionType") or "",
        is_amendment=_bool(offering, "typeOfFiling/newOrAmendment/isAmendment"),
        previous_accession=_text(offering, "typeOfFiling/newOrAmendment/previousAccessionNumber"),
        cik=int(cik_text),
        entity_name=name,
        entity_type=_text(issuer, "entityType"),
        city=_text(issuer, "issuerAddress/city"),
        state=_text(issuer, "issuerAddress/stateOrCountry"),
        year_of_inc_bucket=bucket,
        industry_group=_text(offering, "industryGroup/industryGroupType"),
        revenue_range=_text(offering, "issuerSize/revenueRange") or _text(offering, "issuerSize/aggregateNetAssetValueRange"),
        offering_amount=offering_amount,
        offering_indefinite=indefinite,
        amount_sold=amount_sold,
        date_of_first_sale=first_sale_date,
        first_sale_yet_to_occur=_bool(offering, "typeOfFiling/dateOfFirstSale/yetToOccur"),
        related_person_count=len(root.findall("relatedPersonsList/relatedPersonInfo")),
    )
