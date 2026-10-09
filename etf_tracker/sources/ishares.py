"""iShares (BlackRock) holdings source for SOXX and IGV.

Two endpoints serve the same data (standard library only, see
:mod:`etf_tracker.http` for the transport):

* **CSV** (primary): the "fund document" download behind the *Download
  holdings* button of the product page.  A preamble (fund name, ``Fund
  Holdings as of``, ``Inception Date``, ``Shares Outstanding``, ...) is
  followed by a blank line, a ``Ticker,Name,...`` header and one quoted row
  per holding.  The share-count column is ``Quantity`` on this host and
  ``Shares`` on the ``blackrock.com/us/individual`` mirror; both are
  accepted.  The table ends at the first blank line or at the first line
  that cannot carry the required columns (a disclaimer footer printed
  without a separating blank line).  Any past trading date can be requested
  with ``asOfDate``; a non-trading or not-yet-published date yields HTTP 200
  with an *empty template* (``Fund Holdings as of,"-"`` and no rows).
* **JSON** (fallback): the ``get-product-data`` call the product page makes.
  Holdings are *parallel arrays* under
  ``componentsByNameMap.holdings.containersByNameMap.all.dataPointsByNameMap.<field>.value``
  with a scalar ``asOfDate`` (``YYYYMMDD``).  It carries more precision and
  ISIN/CUSIP/SEDOL, but no shares outstanding.  For an unpublished date the
  arrays are missing (``value`` is ``null``) while ``asOfDate`` still names
  the latest published date.

Conventions follow the package: weights are fractions, dates are
``datetime.date``, downloaded bytes are untrusted and parsed defensively
(bounded sizes and row counts, no ``eval``).

ADR detection (``Holding.is_adr``): the name contains the word ``ADR`` or
``AMERICAN DEPOSITARY`` (the file truncates names, so ``AMERICAN DEPOSIT``
is enough); otherwise an equity whose ``Location`` / ``countryOfRisk`` is
not ``United States`` is flagged by *heuristic* and its ticker recorded in
``Snapshot.meta['adr_heuristic']`` so a human can override it (this also
flags Canadian ordinary shares held by IGV, which the IGV rules never use).
Foreign companies whose US line is ordinary shares rather than a depositary
receipt are listed in :data:`NON_ADR_FOREIGN_ORDINARIES`; the heuristic
never flags them and ``Snapshot.meta['adr_overrides']`` records when that
exception was used.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from etf_tracker import http as _http
from etf_tracker.holdings import CASH, DERIVATIVE, EQUITY, OTHER, Holding, Snapshot
from etf_tracker.sources import (
    STATUS_ERROR,
    STATUS_NO_DATA,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    FetchResult,
)

__all__ = [
    "ETFS",
    "PORTFOLIO_IDS",
    "SOURCE_CSV",
    "SOURCE_JSON",
    "MIN_ROWS",
    "NON_ADR_FOREIGN_ORDINARIES",
    "WEIGHT_SUM_RANGE_PCT",
    "EARLIEST_AS_OF",
    "MAX_FUTURE_DAYS",
    "MAX_BODY_BYTES",
    "MAX_ROWS",
    "ParseError",
    "EmptyTemplateError",
    "build_csv_url",
    "build_json_url",
    "parse_csv",
    "parse_product_data_json",
    "detect_empty_template",
    "validate_snapshot",
    "fetch",
]

log = logging.getLogger(__name__)

ETFS: tuple[str, ...] = ("SOXX", "IGV")
PORTFOLIO_IDS: dict[str, str] = {"SOXX": "239705", "IGV": "239771"}

SOURCE_CSV = "blackrock-fund-document-csv"
SOURCE_JSON = "ishares-product-data-json"

#: Minimum number of holding rows (all asset classes) for a snapshot to be
#: accepted by :func:`fetch`.  SOXX holds 30 names + cash lines; IGV 100+.
MIN_ROWS: dict[str, int] = {"SOXX": 20, "IGV": 60}
#: Published weights (percent, cash and derivative lines included) must sum
#: to a value inside this closed interval.
WEIGHT_SUM_RANGE_PCT: tuple[float, float] = (98.0, 102.0)
#: Non-US companies whose US listing is *ordinary shares* (not ADRs), so the
#: location heuristic must not count them against the SOXX 10% ADR cap.
#: TSEM: Tower Semiconductor Ltd. (Israel), ordinary shares on NASDAQ and the
#: TASE.  Evidence from the data: on the 2026-09-18 reconstitution trade
#: date the ADR group sums to 10.35% without TSEM (the index caps it at 10%
#: at the reference date) but 11.59% with it.
#: NVMI: Nova Ltd. (Israel), ordinary shares on NASDAQ and the TASE (ISIN
#: IL0010845571); held by SOXX until the 2026-09-18 reconstitution.
NON_ADR_FOREIGN_ORDINARIES: frozenset[str] = frozenset({"TSEM", "NVMI"})
#: Plausibility window for the as-of date.  SOXX and IGV both launched on
#: 2001-07-10; a date more than a week after ``today`` can only come from a
#: corrupt file and would otherwise become the "latest" snapshot for good.
EARLIEST_AS_OF = date(2001, 7, 10)
MAX_FUTURE_DAYS = 7
#: Hard bounds on untrusted input.
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10_000
#: Cash lines sometimes carry a nominal price (``USD CASH`` at 100.00 with
#: the quantity in dollars).  When ``shares * price`` disagrees with the
#: authoritative market value by more than this relative amount the price
#: is replaced by ``market_value / shares``.
PRICE_CORRECTION_TOLERANCE = 0.01

CSV_URL_TEMPLATE = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/"
    "get-fund-document?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares"
    "&locale=en_US&userType=individual&component=holdings&portfolioId={portfolio_id}"
)
JSON_URL_TEMPLATE = (
    "https://www.ishares.com/varnish-api/blk-one01-product-data/product-data/api/v2/"
    "get-product-data?appSubType=ISHARES&appType=PRODUCT_PAGE&component=holdings.all"
    "&locale=en_US&portfolioId={portfolio_id}&targetSite=us-ishares&userType=individual"
    "&excludeContent=true&includeConfig=true"
)

_ASOF_KEY = "fund holdings as of"
_INCEPTION_KEY = "inception date"
_SHARES_OUT_KEY = "shares outstanding"
_NULL_STRINGS = frozenset({"", "-", "--", "n/a", "na", "nan", "null", "none"})
_ADR_NAME_RE = re.compile(r"\bADRS?\b|\bAMERICAN\s+DEPOSIT\w*", re.IGNORECASE)
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%Y%m%d")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
_PREAMBLE_SCAN_LIMIT = 200
_DOMESTIC_LOCATIONS = frozenset({"united states", "usa", "us", "united states of america"})
_REQUIRED_COLUMNS = ("Ticker", "Name", "Asset Class", "Market Value", "Weight (%)")
_SHARES_COLUMNS = ("Quantity", "Shares")


class ParseError(ValueError):
    """The body is not an iShares holdings file this module understands."""


class EmptyTemplateError(ParseError):
    """The endpoint answered with its empty template: no holdings for that date."""


# --------------------------------------------------------------------------
# URLs and arguments
# --------------------------------------------------------------------------


def _check_etf(etf: str) -> str:
    key = str(etf).strip().upper()
    if key not in PORTFOLIO_IDS:
        raise ValueError(f"unknown iShares ETF {etf!r}; expected one of {', '.join(ETFS)}")
    return key


def _coerce_as_of(as_of: date | datetime | str | None) -> date | None:
    """Accept ``None``, a date/datetime, ``YYYY-MM-DD`` or ``YYYYMMDD``."""
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    if isinstance(as_of, str):
        text = as_of.strip()
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    raise TypeError(f"as_of must be a date, an ISO string or None, not {type(as_of).__name__}")


def _as_of_param(as_of: date) -> str:
    text = as_of.strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", text):  # pragma: no cover - strftime guarantees this
        raise ValueError(f"cannot format {as_of!r} as YYYYMMDD")
    return text


def build_csv_url(etf: str, as_of: date | str | None = None) -> str:
    """URL of the fund-document CSV; ``as_of=None`` requests the latest file."""
    url = CSV_URL_TEMPLATE.format(portfolio_id=PORTFOLIO_IDS[_check_etf(etf)])
    day = _coerce_as_of(as_of)
    return url if day is None else f"{url}&asOfDate={_as_of_param(day)}"


def build_json_url(etf: str, as_of: date | str | None = None) -> str:
    """URL of the ``get-product-data`` JSON twin; ``as_of=None`` = latest."""
    url = JSON_URL_TEMPLATE.format(portfolio_id=PORTFOLIO_IDS[_check_etf(etf)])
    day = _coerce_as_of(as_of)
    return url if day is None else f"{url}&asOfDate={_as_of_param(day)}"


# --------------------------------------------------------------------------
# Scalar helpers
# --------------------------------------------------------------------------


def _to_text(data: bytes | bytearray | str) -> str:
    """Bound the size, strip a UTF-8 BOM and decode (replacement on errors)."""
    if isinstance(data, (bytes, bytearray)):
        if len(data) > MAX_BODY_BYTES:
            raise ParseError(f"body of {len(data)} bytes exceeds the {MAX_BODY_BYTES} byte limit")
        raw = bytes(data)
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        return raw.decode("utf-8", errors="replace")
    if isinstance(data, str):
        if len(data) > MAX_BODY_BYTES:
            raise ParseError(f"text of {len(data)} characters exceeds the {MAX_BODY_BYTES} limit")
        return data.lstrip("﻿")
    raise TypeError(f"expected bytes or str, not {type(data).__name__}")


def _number(value: Any) -> float | None:
    """Parse a CSV/JSON number: quoted, thousands separators, ``(x)`` for
    negative, ``%``/``$`` decorations; ``'-'``/empty/non-finite -> ``None``.

    Raises ``ValueError`` for text that is not a number at all.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if not isinstance(value, str):
        raise ValueError(f"not a number: {value!r}")
    text = value.strip().strip('"').strip()
    if text.lower() in _NULL_STRINGS:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace(",", "").replace("$", "").replace("%", "").replace(" ", "")
    number = float(text)
    if not math.isfinite(number):
        return None
    return -number if negative else number


def _clean(value: Any) -> str | None:
    """Strip a text field; ``None``/``'-'``/empty -> ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _NULL_STRINGS else text


def _parse_fund_date(value: Any) -> date | None:
    """``'Oct 08, 2026'`` (and a few other spellings) -> date; ``'-'`` -> None."""
    text = _clean(value)
    if text is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ParseError(f"unrecognised date {text!r}")


def _yyyymmdd(value: Any) -> date | None:
    """JSON ``asOfDate`` scalar (``20261007`` int or string) -> date."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        value = int(value)
    text = str(value).strip()
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _map_asset_class(raw: str | None) -> str:
    """iShares ``Asset Class`` labels -> :data:`etf_tracker.holdings.ASSET_CLASSES`."""
    text = (raw or "").strip().lower()
    if text == "equity":
        return EQUITY
    if text in {"money market", "cash", "cash collateral and margins"}:
        return CASH
    if text in {"futures", "forwards", "options", "swaps"}:
        return DERIVATIVE
    if "cash" in text or "money market" in text:
        return CASH
    if any(word in text for word in ("future", "forward", "option", "swap", "derivative")):
        return DERIVATIVE
    if any(word in text for word in ("equity", "stock", "common", "preferred", "adr")):
        return EQUITY
    return OTHER


def _synthesize_ticker(name: str | None, index: int) -> str:
    base = _NON_ALNUM_RE.sub("_", (name or "").upper()).strip("_")
    return f"_{base}" if base else f"_ROW{index}"


def _adr_flag(name: str | None, location: str | None, asset_class: str) -> tuple[bool, bool]:
    """Return ``(is_adr, by_heuristic)`` for one row."""
    if asset_class != EQUITY:
        return False, False
    if name and _ADR_NAME_RE.search(name):
        return True, False
    loc = (location or "").strip().lower()
    if loc and loc not in _NULL_STRINGS and loc not in _DOMESTIC_LOCATIONS:
        return True, True
    return False, False


# --------------------------------------------------------------------------
# Row -> Holding (shared by the CSV and JSON parsers)
# --------------------------------------------------------------------------


@dataclass
class _Context:
    """Accumulates per-snapshot side information while rows are converted."""

    etf: str
    as_of: date
    source: str
    adr_heuristic: list[str] = field(default_factory=list)
    adr_overrides: list[str] = field(default_factory=list)
    derivative_notional: dict[str, float] = field(default_factory=dict)
    price_corrected: list[str] = field(default_factory=list)
    synthesized_tickers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    weight_sum_pct: float = 0.0
    equity_count: int = 0

    def number(self, value: Any, label: str, row_id: str) -> float | None:
        try:
            return _number(value)
        except ValueError:
            self.warnings.append(f"{row_id}: unreadable {label} {value!r}")
            return None

    def holding(
        self,
        index: int,
        *,
        ticker: Any,
        name: Any,
        asset_class_raw: Any,
        shares: Any,
        price: Any,
        market_value: Any,
        weight_pct: Any,
        currency: Any = None,
        sector: Any = None,
        location: Any = None,
        notional: Any = None,
    ) -> Holding:
        name_text = _clean(name) or ""
        ticker_text = _clean(ticker)
        if ticker_text is None:
            ticker_text = _synthesize_ticker(name_text, index)
            self.synthesized_tickers.append(ticker_text)
        row_id = ticker_text
        asset_class = _map_asset_class(_clean(asset_class_raw))
        shares_f = self.number(shares, "shares", row_id)
        price_f = self.number(price, "price", row_id)
        mv_f = self.number(market_value, "market value", row_id)
        pct = self.number(weight_pct, "weight", row_id)
        weight = None if pct is None else pct / 100.0
        if pct is not None:
            self.weight_sum_pct += pct
        notional_f = self.number(notional, "notional value", row_id)

        if (
            asset_class == CASH
            and shares_f not in (None, 0.0)
            and price_f is not None
            and mv_f not in (None, 0.0)
        ):
            implied = shares_f * price_f
            if abs(implied - mv_f) / abs(mv_f) > PRICE_CORRECTION_TOLERANCE:
                price_f = mv_f / shares_f
                self.price_corrected.append(ticker_text)
        if asset_class == DERIVATIVE and notional_f is not None:
            self.derivative_notional[ticker_text] = notional_f

        is_adr, by_heuristic = _adr_flag(name_text, _clean(location), asset_class)
        if by_heuristic and ticker_text in NON_ADR_FOREIGN_ORDINARIES:
            is_adr, by_heuristic = False, False
            self.adr_overrides.append(ticker_text)
        if by_heuristic:
            self.adr_heuristic.append(ticker_text)
        if asset_class == EQUITY:
            self.equity_count += 1

        return Holding(
            etf=self.etf,
            as_of=self.as_of,
            ticker=ticker_text,
            name=name_text,
            asset_class=asset_class,
            shares=shares_f,
            price=price_f,
            market_value=mv_f,
            weight=weight,
            currency=_clean(currency) or "USD",
            sector=_clean(sector),
            is_adr=is_adr,
            company_id=None,
            source=self.source,
        )

    def meta(self, holdings: list[Holding]) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "row_count": len(holdings),
            "equity_count": self.equity_count,
            "weight_sum_pct": self.weight_sum_pct,
            "adr_heuristic": list(self.adr_heuristic),
            "adr_overrides": list(self.adr_overrides),
            "derivative_notional": dict(self.derivative_notional),
            "price_corrected": list(self.price_corrected),
        }
        if self.synthesized_tickers:
            meta["synthesized_tickers"] = list(self.synthesized_tickers)
        if self.warnings:
            meta["parse_warnings"] = list(self.warnings)
        return meta


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def _fields(line: str) -> list[str]:
    """CSV-split one physical line (quotes honoured)."""
    try:
        return next(csv.reader([line]))
    except (csv.Error, StopIteration):
        return []


def _is_header_line(line: str) -> bool:
    head = line.lstrip().lower()
    if not (head.startswith("ticker,") or head.startswith('"ticker"')):
        return False
    fields = [f.strip().lower() for f in _fields(line)]
    return len(fields) >= 2 and fields[0] == "ticker" and fields[1] == "name"


def _until_blank(lines: Iterable[str]) -> Iterator[str]:
    """Yield lines up to (not including) the first blank one."""
    for line in lines:
        if not line.strip():
            return
        yield line


def _scan_csv(text: str) -> tuple[list[str], int | None]:
    """Return ``(lines, header_index)``; the header is the ``Ticker,Name`` line."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if index > _PREAMBLE_SCAN_LIMIT:
            break
        if _is_header_line(line):
            return lines, index
    return lines, None


def _parse_preamble(lines: list[str]) -> dict[str, Any]:
    """Fund name and the ``key,"value"`` lines above the holdings header."""
    out: dict[str, Any] = {"fund_name": None, "as_of_text": None, "inception_text": None, "shares_outstanding_text": None}
    for line in lines:
        if not line.strip():
            continue
        fields = _fields(line)
        if not fields:
            continue
        key = fields[0].strip().lower()
        value = fields[1].strip() if len(fields) > 1 else None
        if key == _ASOF_KEY:
            out["as_of_text"] = value
        elif key == _INCEPTION_KEY:
            out["inception_text"] = value
        elif key == _SHARES_OUT_KEY:
            out["shares_outstanding_text"] = value
        elif out["fund_name"] is None and out["as_of_text"] is None and value is None:
            out["fund_name"] = fields[0].strip()
        elif out["fund_name"] is None and out["as_of_text"] is None:
            out["fund_name"] = line.strip().strip('"')
    return out


def _table_layout(header_line: str) -> tuple[list[str], dict[str, int], str, int]:
    """``(columns, lower-cased name -> index, shares column, min_fields)``
    for the ``Ticker,Name,...`` header.  ``min_fields`` is the number of
    fields a line needs to reach every required column; a shorter line
    cannot be a holding.  Raises :class:`ParseError` when a required column
    is missing."""
    columns = [c.strip() for c in _fields(header_line)]
    col: dict[str, int] = {}
    for index, name in enumerate(columns):
        col.setdefault(name.lower(), index)
    missing = [name for name in _REQUIRED_COLUMNS if name.lower() not in col]
    if missing:
        raise ParseError(f"header lacks required column(s): {', '.join(missing)}")
    shares_column = next((name for name in _SHARES_COLUMNS if name.lower() in col), None)
    if shares_column is None:
        raise ParseError("header lacks a 'Quantity' or 'Shares' column")
    min_fields = max(col[name.lower()] for name in _REQUIRED_COLUMNS + (shares_column,)) + 1
    return columns, col, shares_column, min_fields


def _csv_rows(lines: Iterable[str], min_fields: int = 1) -> tuple[list[list[str]], str | None]:
    """Table rows up to the first blank line, as ``(rows, footer)``.

    A non-empty line with fewer than ``min_fields`` fields cannot reach the
    required columns, so it is not a holding but the start of a footer
    (disclaimer text printed without a separating blank line): parsing stops
    there and the line is returned, truncated, as ``footer``.
    """
    rows: list[list[str]] = []
    for row in csv.reader(_until_blank(lines)):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < min_fields:
            return rows, ",".join(row)[:80]
        if len(rows) >= MAX_ROWS:
            raise ParseError(f"more than {MAX_ROWS} holding rows")
        rows.append(row)
    return rows, None


def detect_empty_template(text: bytes | str) -> bool:
    """True when the CSV is iShares' *empty template*: the holdings header is
    followed by no rows (the ``Fund Holdings as of`` value is then ``-``), or
    the header is missing but the preamble says ``Fund Holdings as of,"-"``.

    Mirrors :func:`parse_csv`: a file with rows but no date is **not** an
    empty template (it is unparseable).  Bodies without the preamble or
    header (HTML pages, garbage) are not empty templates either.
    """
    try:
        content = _to_text(text)
    except (ParseError, TypeError):
        return False
    lines, header_index = _scan_csv(content)
    if header_index is None:
        as_of_text = _parse_preamble(lines[:_PREAMBLE_SCAN_LIMIT])["as_of_text"]
        return as_of_text is not None and as_of_text.strip().lower() in _NULL_STRINGS
    try:
        _, _, _, min_fields = _table_layout(lines[header_index])
        rows, _ = _csv_rows(lines[header_index + 1 :], min_fields)
    except ParseError:
        return False
    return not rows


def parse_csv(text_or_bytes: bytes | str, etf: str, source: str = SOURCE_CSV) -> Snapshot:
    """Parse a BlackRock fund-document holdings CSV into a :class:`Snapshot`.

    Raises :class:`EmptyTemplateError` for the empty template (no holdings
    for that date) and :class:`ParseError` for anything that is not the
    expected format.  ``etf`` is stored on the snapshot as given
    (upper-cased); it is not restricted to :data:`ETFS` so other iShares
    funds can be parsed with the same code.
    """
    etf_key = str(etf).strip().upper()
    if not etf_key:
        raise ValueError("etf must not be empty")
    content = _to_text(text_or_bytes)
    lines, header_index = _scan_csv(content)
    if header_index is None:
        raise ParseError("no 'Ticker,Name,...' header line found (not a holdings CSV)")
    preamble = _parse_preamble(lines[:header_index])
    columns, col, shares_column, min_fields = _table_layout(lines[header_index])

    rows, footer = _csv_rows(lines[header_index + 1 :], min_fields)
    as_of = _parse_fund_date(preamble["as_of_text"])
    if not rows:
        raise EmptyTemplateError(
            "empty template: no holding rows after the header"
            + (" and as-of date is '-'" if as_of is None else "")
        )
    if as_of is None:
        # Rows without a date cannot be stored under a date; this is not the
        # empty template, so the caller may try the JSON twin instead.
        raise ParseError(
            f"{len(rows)} holding rows but no 'Fund Holdings as of' date ({preamble['as_of_text']!r})"
        )

    ctx = _Context(etf_key, as_of, source)
    if footer is not None:
        ctx.warnings.append(f"table ended at footer text after row {len(rows)}: {footer!r}")

    def cell(row: list[str], name: str) -> str | None:
        index = col.get(name)
        if index is None or index >= len(row):
            return None
        return row[index]

    holdings: list[Holding] = []
    for position, row in enumerate(rows):
        if len(row) < len(columns):
            ctx.warnings.append(f"row {position + 1}: {len(row)} fields, header has {len(columns)}")
        holdings.append(
            ctx.holding(
                position,
                ticker=cell(row, "ticker"),
                name=cell(row, "name"),
                asset_class_raw=cell(row, "asset class"),
                shares=cell(row, shares_column.lower()),
                price=cell(row, "price"),
                market_value=cell(row, "market value"),
                weight_pct=cell(row, "weight (%)"),
                currency=cell(row, "currency"),
                sector=cell(row, "sector"),
                location=cell(row, "location"),
                notional=cell(row, "notional value"),
            )
        )

    try:
        shares_outstanding = _number(preamble["shares_outstanding_text"])
    except ValueError:
        shares_outstanding = None
        ctx.warnings.append(f"unreadable shares outstanding {preamble['shares_outstanding_text']!r}")
    try:
        inception = _parse_fund_date(preamble["inception_text"])
    except ParseError:
        inception = None
        ctx.warnings.append(f"unreadable inception date {preamble['inception_text']!r}")

    meta: dict[str, Any] = {
        "fund_name": preamble["fund_name"],
        "shares_outstanding": shares_outstanding,
        "inception_date": inception.isoformat() if inception else None,
        "raw_header_columns": columns,
        "shares_column": shares_column,
        "format": "csv",
    }
    meta.update(ctx.meta(holdings))
    return Snapshot(etf=etf_key, as_of=as_of, source=source, holdings=holdings, meta=meta)


# --------------------------------------------------------------------------
# JSON twin
# --------------------------------------------------------------------------


def _dig(obj: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def parse_product_data_json(data: bytes | str | dict[str, Any], etf: str, source: str = SOURCE_JSON) -> Snapshot:
    """Parse the ``get-product-data`` (``component=holdings.all``) JSON.

    Raises :class:`EmptyTemplateError` when the holdings arrays are absent
    or empty (unpublished / non-trading date) and :class:`ParseError` when
    the structure is not the expected one.
    """
    etf_key = str(etf).strip().upper()
    if not etf_key:
        raise ValueError("etf must not be empty")
    if isinstance(data, dict):
        obj: Any = data
    else:
        text = _to_text(data)
        try:
            obj = json.loads(text)
        except ValueError as exc:
            raise ParseError(f"body is not JSON: {exc}") from None
        except RecursionError:  # pathologically nested input; json.loads recurses per level
            raise ParseError("body is not JSON: nesting too deep") from None
    if not isinstance(obj, dict):
        raise ParseError("JSON root is not an object")

    points = _dig(obj, "componentsByNameMap", "holdings", "containersByNameMap", "all", "dataPointsByNameMap")
    if not isinstance(points, dict):
        raise ParseError("componentsByNameMap.holdings.containersByNameMap.all.dataPointsByNameMap missing")

    def scalar(name: str) -> Any:
        point = points.get(name)
        return point.get("value") if isinstance(point, dict) else None

    def array(name: str) -> list[Any] | None:
        value = scalar(name)
        return list(value) if isinstance(value, list) else None

    tickers = array("ticker")
    if not tickers:
        raise EmptyTemplateError("empty template: no ticker array in holdings.all (date not published)")
    n = len(tickers)
    if n > MAX_ROWS:
        raise ParseError(f"more than {MAX_ROWS} holding rows")
    as_of = _yyyymmdd(scalar("asOfDate"))
    if as_of is None:
        raise ParseError(f"asOfDate {scalar('asOfDate')!r} is not YYYYMMDD")

    ctx = _Context(etf_key, as_of, source)

    def column(name: str) -> list[Any]:
        values = array(name)
        if values is None:
            return [None] * n
        if len(values) != n:
            ctx.warnings.append(f"{name}: {len(values)} values for {n} tickers")
            values = (values + [None] * n)[:n]
        return values

    names = column("issueName")
    classes = column("assetClass")
    units = column("unitsHeld")
    prices = column("unitPrice")
    values = column("marketValue")
    percents = column("holdingPercent")
    notionals = column("notionalValue")
    sectors = column("sectorName")
    countries = column("countryOfRisk")
    currencies = column("currencyCode")
    isins, cusips, sedols = column("isin"), column("cusip"), column("sedol")

    holdings: list[Holding] = []
    identifiers: dict[str, dict[str, str]] = {}
    for i in range(n):
        holding = ctx.holding(
            i,
            ticker=tickers[i],
            name=names[i],
            asset_class_raw=classes[i],
            shares=units[i],
            price=prices[i],
            market_value=values[i],
            weight_pct=percents[i],
            currency=currencies[i],
            sector=sectors[i],
            location=countries[i],
            notional=notionals[i],
        )
        holdings.append(holding)
        ids = {k: v for k, v in (("isin", _clean(isins[i])), ("cusip", _clean(cusips[i])), ("sedol", _clean(sedols[i]))) if v}
        if ids:
            identifiers.setdefault(holding.ticker, ids)

    available = [d.isoformat() for d in (_yyyymmdd(v) for v in (array("dateList") or [])) if d is not None]
    meta: dict[str, Any] = {
        "fund_name": _clean(obj.get("fundName")),
        "shares_outstanding": None,
        "inception_date": None,
        "raw_header_columns": [k for k in points.keys() if isinstance(k, str)],
        "format": "json",
        "portfolio_id": _clean(obj.get("productId")),
        "available_dates": available,
        "identifiers": identifiers,
    }
    meta.update(ctx.meta(holdings))
    return Snapshot(etf=etf_key, as_of=as_of, source=source, holdings=holdings, meta=meta)


# --------------------------------------------------------------------------
# Validation and fetch
# --------------------------------------------------------------------------


def validate_snapshot(
    snapshot: Snapshot,
    etf: str | None = None,
    requested_as_of: date | None = None,
    today: date | None = None,
) -> list[str]:
    """Problems that make a parsed snapshot unusable (empty list = accept).

    Checks the as-of date (that it matches ``requested_as_of`` when one was
    asked for, and that it lies between :data:`EARLIEST_AS_OF` and ``today``
    + :data:`MAX_FUTURE_DAYS`; ``today`` defaults to the current date), the
    minimum row count of :data:`MIN_ROWS` and that the published weights sum
    to a percentage inside :data:`WEIGHT_SUM_RANGE_PCT`.  As a side effect
    ``snapshot.validate()`` runs (filling derivable prices) and its warnings
    are stored under ``snapshot.meta['validation_warnings']``.
    """
    problems: list[str] = []
    key = (etf or snapshot.etf).strip().upper()
    if not isinstance(snapshot.as_of, date):
        problems.append("as_of date missing")
    else:
        as_of_iso = snapshot.as_of.isoformat()
        if requested_as_of is not None and snapshot.as_of != requested_as_of:
            problems.append(f"as-of {as_of_iso} differs from the requested {requested_as_of.isoformat()}")
        clock = date.today() if today is None else today
        latest_plausible = clock + timedelta(days=MAX_FUTURE_DAYS)
        if snapshot.as_of < EARLIEST_AS_OF:
            problems.append(f"as-of {as_of_iso} is before the fund's inception {EARLIEST_AS_OF.isoformat()}")
        elif snapshot.as_of > latest_plausible:
            problems.append(
                f"as-of {as_of_iso} is more than {MAX_FUTURE_DAYS} days after today {clock.isoformat()}"
            )
    minimum = MIN_ROWS.get(key, 1)
    if len(snapshot.holdings) < minimum:
        problems.append(f"only {len(snapshot.holdings)} holding rows, expected at least {minimum}")
    weights = [h.weight for h in snapshot.holdings if h.weight is not None]
    total_pct = sum(weights) * 100.0
    low, high = WEIGHT_SUM_RANGE_PCT
    if not weights or not (low <= total_pct <= high):
        problems.append(f"published weights sum to {total_pct:.3f}%, expected within {low}-{high}%")
    snapshot.meta["validation_warnings"] = snapshot.validate()
    return problems


@dataclass
class _Route:
    """Outcome of one endpoint attempt (``status=None`` means: try the next)."""

    status: str | None = None
    snapshot: Snapshot | None = None
    note: str = ""


def _attempt(
    label: str,
    url: str,
    suffix: str,
    parser: Callable[[bytes, str], Snapshot],
    http_get: Callable[..., _http.Response],
    etf: str,
    requested: date | None,
    raw_files: dict[str, bytes],
) -> _Route:
    try:
        response = http_get(url)
    except Exception as exc:  # a transport that raises instead of returning a Response
        log.warning("%s %s: request raised %s: %s", etf, label, type(exc).__name__, exc)
        return _Route(note=f"{label}: request raised {type(exc).__name__}: {exc}")
    if response.error:
        return _Route(note=f"{label}: transport error {response.error}")
    if not response.ok:
        return _Route(note=f"{label}: HTTP {response.status}")
    if not response.body:
        return _Route(note=f"{label}: empty body")
    if response.looks_like_html():
        return _Route(note=f"{label}: HTML page served instead of the file (HTTP {response.status})")
    try:
        snapshot = parser(response.body, etf)
    except EmptyTemplateError as exc:
        raw_files[suffix] = bytes(response.body)
        return _Route(status=STATUS_NO_DATA, note=f"{label}: {exc}")
    except (ParseError, ValueError, TypeError) as exc:
        return _Route(note=f"{label}: parse failed: {type(exc).__name__}: {exc}")
    raw_files[suffix] = bytes(response.body)
    problems = validate_snapshot(snapshot, etf, requested)
    if problems:
        return _Route(status=STATUS_ERROR, snapshot=None, note=f"{label}: rejected: " + "; ".join(problems))
    snapshot.meta.update(
        {
            "url": url,
            "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "http_status": response.status,
            "content_type": response.content_type,
            "portfolio_id": PORTFOLIO_IDS[etf],
            "route": label.lower(),
            "requested_as_of": requested.isoformat() if requested else None,
        }
    )
    return _Route(status=STATUS_OK, snapshot=snapshot, note=f"{label}: ok")


def fetch(
    etf: str,
    as_of: date | str | None = None,
    *,
    http_get: Callable[..., _http.Response] | None = None,
) -> FetchResult:
    """Download the holdings of ``etf`` (``'SOXX'`` or ``'IGV'``).

    The CSV endpoint is tried first; when it fails at the transport level,
    serves an HTML page or cannot be parsed, the JSON twin is tried.  An
    empty template from either route is ``no_data`` (non-trading day or not
    yet published).  A snapshot that parses but fails
    :func:`validate_snapshot` is an ``error`` without further fallback,
    since both routes serve the same data.  ``raw_files`` holds the bodies
    exactly as received (``'.csv'`` and/or ``'.json'``) whenever the body
    was the expected kind of file, so the caller can archive them.
    """
    get = http_get or _http.get
    key = str(etf).strip().upper()
    requested = _coerce_as_of(as_of)
    requested_iso = requested.isoformat() if requested else None
    if key not in PORTFOLIO_IDS:
        return FetchResult(
            etf=key,
            status=STATUS_UNSUPPORTED,
            message=f"{key} is not served by the iShares source (supported: {', '.join(ETFS)})",
            source=SOURCE_CSV,
            requested_as_of=requested_iso,
        )

    raw_files: dict[str, bytes] = {}
    notes: list[str] = []
    routes = (
        ("CSV", build_csv_url(key, requested), ".csv", parse_csv, SOURCE_CSV),
        ("JSON", build_json_url(key, requested), ".json", parse_product_data_json, SOURCE_JSON),
    )
    for label, url, suffix, parser, source in routes:
        route = _attempt(label, url, suffix, parser, get, key, requested, raw_files)
        notes.append(route.note)
        if route.status == STATUS_OK and route.snapshot is not None:
            snap = route.snapshot
            message = (
                f"{key} holdings as of {snap.as_of.isoformat()} via {label} "
                f"({len(snap.holdings)} rows, {snap.meta.get('equity_count', 0)} equities)"
            )
            if len(notes) > 1:
                message += "; fallback after " + "; ".join(notes[:-1])
                log.warning("%s: %s", key, message)
            else:
                log.info("%s: %s", key, message)
            return FetchResult(key, STATUS_OK, snap, raw_files, message, source, requested_iso)
        if route.status == STATUS_NO_DATA:
            target = requested_iso or "latest"
            message = f"{key}: no holdings published for {target} ({'; '.join(notes)})"
            log.info("%s: %s", key, message)
            return FetchResult(key, STATUS_NO_DATA, None, raw_files, message, source, requested_iso)
        if route.status == STATUS_ERROR:
            message = f"{key}: {'; '.join(notes)}"
            log.error("%s: %s", key, message)
            return FetchResult(key, STATUS_ERROR, None, raw_files, message, source, requested_iso)
        log.warning("%s: %s", key, route.note)

    message = f"{key}: all routes failed: " + "; ".join(notes)
    log.error("%s: %s", key, message)
    return FetchResult(key, STATUS_ERROR, None, raw_files, message, SOURCE_CSV, requested_iso)
