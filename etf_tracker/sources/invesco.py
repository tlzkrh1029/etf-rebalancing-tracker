"""Invesco QQQ holdings source (``dng-api.invesco.com`` JSON).

Invesco publishes the *current* holdings of each ETF through a small JSON
API (no history).  Two documents are needed to build a full
:class:`~etf_tracker.holdings.Snapshot`:

* ``.../shareclasses/QQQ/holdings/fund`` -- one row per holding with ticker,
  issuer name, units (shares) and weight in percent of total net assets.
  There is **no** per-holding price or market value.
* ``.../shareclasses/QQQ?variationType=fundDetails`` -- fund level figures
  (NAV, shares outstanding, total net assets).  Per-holding market values are
  derived as ``weight * total_net_assets`` and prices as ``market_value /
  units`` (equity rows with positive units only); ``Snapshot.meta[
  'price_derivation']`` records this.

Edge behaviour verified on 2026-10-09 (see ``probe/summary.txt``):

* The Fastly edge answers a synthetic **HTTP 406 with an empty body** to any
  User-Agent starting with ``Mozilla/5.0`` and to an empty UA.  The neutral
  project UA and urllib's default UA get 200.  :func:`fetch` tries the project
  UA first and retries once with no UA header on 406.  A browser UA is never
  sent.
* ``content-type`` is ``text/plain`` although the body is JSON.
* The ``cb`` query parameter is a cache-buster the API ignores; it is sent
  anyway so intermediate caches do not serve a stale copy.
* Only the current snapshot is served: a request for a past ``as_of`` yields
  :data:`~etf_tracker.sources.STATUS_UNSUPPORTED`; a request for a date after
  the served one yields :data:`~etf_tracker.sources.STATUS_NO_DATA` (not yet
  published).
* The document carries two dates.  ``effectiveBusinessDate`` is the close
  the positions and the fundDetails total net assets are priced at (T-1 on
  trading day T); ``effectiveDate`` is the day the list is effective for.
  Observed 2026-10-10 11:08 UTC: ``effectiveDate`` 2026-10-10 with
  ``effectiveBusinessDate`` 2026-10-09 (``shareclassTotalNetAssetsEffectiveDate``
  2026-10-09 too), while the file seen on 2026-10-09 13:05 UTC carried
  2026-10-08 for both.  ``Snapshot.as_of`` is therefore
  ``effectiveBusinessDate`` whenever it is present, well-formed and not after
  ``effectiveDate``; otherwise ``effectiveDate`` with a warning.  ``meta``
  keeps both raw dates and ``as_of_basis`` names the field used.

Security-type mapping (``securityTypeName`` / ``securityTypeCode``):
``Common Stock``/``COM`` -> equity; ``American Depository Receipt``
(``ADR``, ``DRNY``) -> equity with ``is_adr=True``; ``Index Future``/``IFUT``
-> derivative; ``Currency``, ``Currency Collateral``, ``Synthetic Cash``
(``CURR``, ``CURRCOL``, ``SYN``) -> cash; anything else -> other.  A
non-equity row with a ``null`` weight (e.g. ``USDPDV`` pending dividends) is
immaterial bookkeeping and gets weight 0 and asset class ``other``; an
*equity* row without a weight is a data defect, so it stays an equity with
``weight=None`` and a warning in ``meta['warnings']`` (never silently
dropped from the constituents).  Rows with an empty ticker (cash collateral,
synthetic cash) get a deterministic synthetic ticker derived from the issuer
name.

Downloaded bytes are untrusted: bodies are size-bounded, parsed with
``json.loads`` only, and dates, row counts and weight sums are validated.

Standard library only.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
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
    "SOURCE",
    "PRICE_DERIVATION",
    "HOLDINGS_URL",
    "FUND_DETAILS_URL",
    "USER_AGENTS",
    "UA_FALLBACK_STATUSES",
    "MAX_BODY_BYTES",
    "MAX_ROWS",
    "MIN_EQUITY_ROWS",
    "WEIGHT_SUM_MIN_PCT",
    "WEIGHT_SUM_MAX_PCT",
    "holdings_url",
    "fund_details_url",
    "classify_security",
    "synthesize_ticker",
    "parse_fund_details_json",
    "parse_holdings_json",
    "validate_snapshot",
    "fetch",
]

log = logging.getLogger(__name__)

#: ETFs this source serves.
ETFS: tuple[str, ...] = ("QQQ",)
#: Value of ``Snapshot.source`` / ``Holding.source`` / ``FetchResult.source``.
SOURCE = "invesco-dng-api"
#: ``Snapshot.meta['price_derivation']`` when prices were derived.
PRICE_DERIVATION = "weight*TNA/units"

_API_ROOT = "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses"
#: Holdings endpoint without the cache-buster (``{etf}`` is substituted).
HOLDINGS_URL = _API_ROOT + "/{etf}/holdings/fund?idType=ticker&productType=ETF"
#: Fund-details endpoint without the cache-buster (``{etf}`` is substituted).
FUND_DETAILS_URL = _API_ROOT + "/{etf}?idType=ticker&variationType=fundDetails&productType=ETF"

#: User-Agent values tried in order; ``None`` sends no UA header (urllib's
#: default).  Browser UAs (``Mozilla/5.0 ...``) are rejected by the edge and
#: must never appear here.
USER_AGENTS: tuple[str | None, ...] = (_http.DEFAULT_USER_AGENT, None)
#: HTTP statuses that trigger a retry with the next entry of :data:`USER_AGENTS`.
UA_FALLBACK_STATUSES: tuple[int, ...] = (406,)

#: Bodies larger than this are rejected before JSON decoding (the real
#: holdings document is ~27 KB, fundDetails ~0.5 KB).
MAX_BODY_BYTES = 8 * 1024 * 1024
#: More holdings rows than this is treated as a malformed document.
MAX_ROWS = 10_000
#: ``fetch`` reports an error when fewer equity lines are parsed (Nasdaq-100
#: holds about 100 securities).
MIN_EQUITY_ROWS = 90
#: ``fetch`` reports an error when published weights sum outside this range.
WEIGHT_SUM_MIN_PCT = 98.0
WEIGHT_SUM_MAX_PCT = 102.0
#: An ``effectiveDate`` more than this many days in the future is rejected.
MAX_FUTURE_DAYS = 7
#: QQQ inception; an ``effectiveDate`` before it is rejected.
EARLIEST_AS_OF = date(1999, 3, 10)

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")
_NULL_STRINGS = frozenset({"", "null", "none", "nan", "-", "--", "n/a", "na"})

_ADR_CODES = frozenset({"ADR", "DRNY", "GDR", "DR"})
_EQUITY_CODES = frozenset({"COM", "CS", "ORD", "PFD", "REIT"})
_DERIVATIVE_CODES = frozenset({"IFUT", "FUT", "FUTURE", "OPT", "SWAP", "FWD"})
_CASH_CODES = frozenset({"CURR", "CURRCOL", "SYN", "CASH", "MM", "MMF", "TBILL"})

HttpGet = Callable[..., _http.Response]


# --------------------------------------------------------------------------
# URL builders
# --------------------------------------------------------------------------


def _cache_buster(now: float | None) -> int:
    ts = time.time() if now is None else float(now)
    if not math.isfinite(ts) or ts < 0:
        raise ValueError(f"now must be a non-negative epoch timestamp, got {now!r}")
    return int(ts)


def holdings_url(etf: str = "QQQ", now: float | None = None) -> str:
    """Holdings endpoint for ``etf`` with the ``cb`` cache-buster.

    ``now`` is a Unix timestamp in seconds (defaults to the current time);
    tests pass a fixed value for reproducible URLs.
    """
    return HOLDINGS_URL.format(etf=etf.strip().upper()) + f"&cb={_cache_buster(now)}"


def fund_details_url(etf: str = "QQQ", now: float | None = None) -> str:
    """Fund-details endpoint for ``etf`` with the ``cb`` cache-buster."""
    return FUND_DETAILS_URL.format(etf=etf.strip().upper()) + f"&cb={_cache_buster(now)}"


# --------------------------------------------------------------------------
# Field helpers
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """Coerce a JSON scalar to a finite float; anything else becomes ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").rstrip("%").strip()
        if text.lower() in _NULL_STRINGS:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _int(value: Any) -> int | None:
    number = _num(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _text(value: Any) -> str:
    """HTML-unescaped, whitespace-stripped string (``None`` -> ``''``)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return html.unescape(value).strip()


def _parse_date(value: Any) -> date | None:
    """``'YYYY-MM-DD'`` (optionally followed by a time) -> ``date``; else ``None``."""
    if not isinstance(value, str):
        return None
    match = _DATE_RE.match(value.strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _coerce_requested_date(value: Any) -> date | None:
    """Normalise the ``as_of`` argument of :func:`fetch`."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        parsed = _parse_date(value)
        if parsed is None:
            raise ValueError(f"as_of must be a date or 'YYYY-MM-DD', got {value!r}")
        return parsed
    raise TypeError(f"as_of must be a date, ISO string or None, got {type(value).__name__}")


def _load_json(body: bytes, what: str) -> Mapping[str, Any]:
    """Decode a size-bounded JSON object (BOM tolerated).  ``ValueError`` on failure."""
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError(f"{what}: expected bytes, got {type(body).__name__}")
    if len(body) > MAX_BODY_BYTES:
        raise ValueError(f"{what}: body of {len(body)} bytes exceeds {MAX_BODY_BYTES}")
    if not body.strip():
        raise ValueError(f"{what}: empty body")
    try:
        data = json.loads(bytes(body).decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{what}: not valid JSON ({e.msg} at position {e.pos})") from None
    except RecursionError:  # pathologically nested input; json.loads recurses per level
        raise ValueError(f"{what}: not valid JSON (nesting too deep)") from None
    if not isinstance(data, Mapping):
        raise ValueError(f"{what}: top-level JSON value is {type(data).__name__}, expected an object")
    return data


# --------------------------------------------------------------------------
# Row classification
# --------------------------------------------------------------------------


def classify_security(type_name: Any, type_code: Any = None) -> tuple[str, bool]:
    """Map Invesco's ``securityTypeName`` / ``securityTypeCode`` to
    ``(asset_class, is_adr)``.

    The name is matched first (case-insensitive substrings), the code is a
    fallback.  Unknown types become ``'other'``.
    """
    name = _text(type_name).lower()
    code = _text(type_code).upper()

    if "depositary" in name or "depository" in name or name == "adr" or code in _ADR_CODES:
        return EQUITY, True
    if (
        "common stock" in name
        or "ordinary" in name
        or "preferred" in name
        or name in {"stock", "equity", "common", "reit"}
        or code in _EQUITY_CODES
    ):
        return EQUITY, False
    if "future" in name or "option" in name or "swap" in name or "forward" in name or code in _DERIVATIVE_CODES:
        return DERIVATIVE, False
    if "currency" in name or "cash" in name or "money market" in name or "treasury bill" in name or code in _CASH_CODES:
        return CASH, False
    return OTHER, False


def synthesize_ticker(issuer_name: Any, fallback: str = "UNNAMED") -> str:
    """Deterministic ticker for a row without one.

    Upper-cases the issuer name, replaces every non-alphanumeric character
    with ``'_'`` and prefixes ``'_'`` so the result can never collide with a
    real exchange ticker: ``'CASH COLLATERAL' -> '_CASH_COLLATERAL'``.
    """
    base = _text(issuer_name).upper()
    if not base:
        base = fallback.upper()
    return "_" + _NON_ALNUM_RE.sub("_", base)


def _unique(ticker: str, seen: set[str]) -> str:
    if ticker not in seen:
        return ticker
    n = 2
    while f"{ticker}_{n}" in seen:
        n += 1
    return f"{ticker}_{n}"


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def parse_fund_details_json(data: bytes) -> dict[str, Any]:
    """Parse the ``fundDetails`` document into plain fund-level figures.

    Returns a dict with ``effective_date`` (``date`` or ``None``), ``nav``,
    ``shares_outstanding``, ``total_net_assets``,
    ``total_net_assets_effective_date``, ``market_value``,
    ``total_no_of_holdings`` and ``identifier`` (the top-level ``cusip`` /
    ``ticker`` field).  Raises ``ValueError`` when the body is not a JSON
    object.
    """
    obj = _load_json(data, "fundDetails")
    return {
        "identifier": _text(obj.get("ticker") or obj.get("cusip")) or None,
        "effective_date": _parse_date(obj.get("effectiveDate")),
        "nav": _num(obj.get("nav")),
        "shares_outstanding": _num(obj.get("sharesOutstanding")),
        "total_net_assets": _num(obj.get("shareclassTotalNetAssets")),
        "total_net_assets_effective_date": _parse_date(obj.get("shareclassTotalNetAssetsEffectiveDate")),
        "market_value": _num(obj.get("marketValue")),
        "total_no_of_holdings": _int(obj.get("totalNoOfHoldings")),
    }


def _resolve_total_net_assets(
    details: Mapping[str, Any] | None, warnings: list[str]
) -> tuple[float | None, str | None]:
    """Pick the fund total used to derive market values: ``shareclassTotalNetAssets``,
    else ``nav * sharesOutstanding``.  Returns ``(total, basis)``."""
    if details is None:
        return None, None
    tna = details.get("total_net_assets")
    if tna is not None and tna > 0:
        return tna, "shareclassTotalNetAssets"
    nav = details.get("nav")
    shares_out = details.get("shares_outstanding")
    if nav is not None and shares_out is not None and nav > 0 and shares_out > 0:
        warnings.append(
            "fundDetails has no usable shareclassTotalNetAssets; "
            "market values derived from nav * sharesOutstanding"
        )
        return nav * shares_out, "nav*sharesOutstanding"
    warnings.append(
        "fundDetails has neither shareclassTotalNetAssets nor nav and sharesOutstanding; "
        "market_value and price left empty"
    )
    return None, None


def _resolve_as_of(effective_date: date, business_raw: Any) -> tuple[date, str, list[str]]:
    """Pick the snapshot date: ``effectiveBusinessDate`` when usable, else ``effectiveDate``.

    Returns ``(as_of, basis, warnings)`` where ``basis`` is the name of the
    field used.  A missing or empty ``effectiveBusinessDate`` falls back
    silently; a malformed one, or one after ``effectiveDate``, falls back
    with a warning (the pricing date can never be later than the day the
    list is effective for).
    """
    raw = _text(business_raw)
    if not raw:
        return effective_date, "effectiveDate", []
    business = _parse_date(raw)
    if business is None:
        return effective_date, "effectiveDate", [
            f"holdings: effectiveBusinessDate {raw!r} is not 'YYYY-MM-DD'; as_of falls back to "
            f"effectiveDate {effective_date.isoformat()}"
        ]
    if business > effective_date:
        return effective_date, "effectiveDate", [
            f"holdings: effectiveBusinessDate {business.isoformat()} is after effectiveDate "
            f"{effective_date.isoformat()}; as_of falls back to effectiveDate"
        ]
    return business, "effectiveBusinessDate", []


def _check_as_of_range(as_of: date, today: date | None = None) -> None:
    today = date.today() if today is None else today
    if as_of < EARLIEST_AS_OF:
        raise ValueError(f"effectiveDate {as_of.isoformat()} is before QQQ inception {EARLIEST_AS_OF.isoformat()}")
    if as_of > today + timedelta(days=MAX_FUTURE_DAYS):
        raise ValueError(f"effectiveDate {as_of.isoformat()} is more than {MAX_FUTURE_DAYS} days in the future")


def parse_holdings_json(
    holdings_bytes: bytes,
    fund_details_bytes: bytes | None = None,
    etf: str = "QQQ",
    *,
    source_urls: Mapping[str, str] | None = None,
    today: date | None = None,
) -> Snapshot:
    """Build a :class:`~etf_tracker.holdings.Snapshot` from the two Invesco
    documents.

    Args:
        holdings_bytes: body of the holdings endpoint.
        fund_details_bytes: body of the fundDetails endpoint, or ``None``.
            Without it ``market_value`` and ``price`` stay ``None`` and a
            warning is recorded in ``meta['warnings']``.
        etf: ticker the snapshot is for (upper-cased).
        source_urls: recorded in ``meta['source_urls']``; defaults to the
            endpoint templates without cache-buster.
        today: reference date for the future-date sanity check (tests).

    Weights are fractions (``percentageOfTotalNetAssets / 100``); a ``null``
    weight becomes 0.0 with asset class ``'other'`` on a non-equity line and
    ``None`` (plus a warning) on an equity line.  ``shares`` is
    ``units`` as published (dollar amounts for cash lines).  ``company_id``
    is left ``None``: share-class grouping (GOOGL/GOOG) is applied by
    :mod:`etf_tracker.bridge` through ``rules.SHARE_CLASS_GROUPS``.

    ``Snapshot.as_of`` is ``effectiveBusinessDate`` (the pricing close) when
    the document carries a usable one, else ``effectiveDate``; see the module
    docstring.  ``meta['as_of_basis']`` names the field used.

    Raises ``ValueError`` when the body is not the expected document (bad
    JSON, missing/invalid ``effectiveDate``, no ``holdings`` list, empty or
    oversized list).
    """
    etf = etf.strip().upper()
    data = _load_json(holdings_bytes, "holdings")

    effective_date = _parse_date(data.get("effectiveDate"))
    if effective_date is None:
        raise ValueError("holdings: effectiveDate missing or not 'YYYY-MM-DD'")
    _check_as_of_range(effective_date, today)
    as_of, as_of_basis, date_warnings = _resolve_as_of(effective_date, data.get("effectiveBusinessDate"))
    _check_as_of_range(as_of, today)

    rows = data.get("holdings")
    if not isinstance(rows, list):
        raise ValueError("holdings: 'holdings' is missing or not a list")
    if not rows:
        raise ValueError("holdings: 'holdings' list is empty")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"holdings: {len(rows)} rows exceed the {MAX_ROWS} row bound")

    warnings: list[str] = list(date_warnings)
    identifier = _text(data.get("ticker") or data.get("cusip")) or None
    if identifier and identifier.upper() != etf:
        warnings.append(f"holdings document identifies itself as {identifier!r}, expected {etf}")

    details: dict[str, Any] | None = None
    if fund_details_bytes is None:
        warnings.append("fundDetails not available: market_value and price left empty")
    else:
        try:
            details = parse_fund_details_json(fund_details_bytes)
        except ValueError as e:
            warnings.append(f"fundDetails unparseable ({e}): market_value and price left empty")
    tna, tna_basis = _resolve_total_net_assets(details, warnings)

    details_date: date | None = None
    if details is not None:
        details_date = details.get("total_net_assets_effective_date") or details.get("effective_date")
        if details_date is not None and details_date != as_of:
            warnings.append(
                f"fundDetails total net assets are as of {details_date.isoformat()} while holdings are "
                f"as of {as_of.isoformat()}; derived market values use the fundDetails total"
            )
        if details.get("identifier") and details["identifier"].upper() != etf:
            warnings.append(f"fundDetails document identifies itself as {details['identifier']!r}, expected {etf}")

    holdings: list[Holding] = []
    seen: set[str] = set()
    synthesized: list[str] = []
    adr_tickers: list[str] = []
    null_weight: list[str] = []
    type_counts: dict[str, int] = {}
    weight_sum_pct = 0.0

    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            warnings.append(f"row {index}: not a JSON object, skipped")
            continue
        name = _text(row.get("issuerName"))
        ticker = _text(row.get("ticker"))
        if not ticker:
            ticker = _unique(synthesize_ticker(name or _text(row.get("cusip")) or f"ROW_{index}"), seen)
            synthesized.append(ticker)
        elif ticker in seen:
            warnings.append(f"duplicate ticker {ticker!r} at row {index}")
        seen.add(ticker)

        type_name = _text(row.get("securityTypeName"))
        type_counts[type_name or "?"] = type_counts.get(type_name or "?", 0) + 1
        asset_class, is_adr = classify_security(type_name, row.get("securityTypeCode"))

        pct = _num(row.get("percentageOfTotalNetAssets"))
        weight: float | None
        if pct is None:
            null_weight.append(ticker)
            if asset_class == EQUITY:
                # Keep it visible as an equity: a missing weight on a stock is
                # a defect to report, not a reason to drop the constituent.
                weight = None
                warnings.append(
                    f"equity row {ticker!r} ({name or '?'}) has no percentageOfTotalNetAssets; weight left empty"
                )
            else:
                weight = 0.0
                asset_class = OTHER
        else:
            weight = pct / 100.0
            weight_sum_pct += pct

        units = _num(row.get("units"))
        market_value: float | None = None
        price: float | None = None
        if tna is not None and pct is not None:
            market_value = weight * tna
            if asset_class == EQUITY and units is not None and units > 0:
                price = market_value / units

        if asset_class == EQUITY and is_adr:
            adr_tickers.append(ticker)

        currency = _text(row.get("currency")).upper() or "USD"
        holdings.append(
            Holding(
                etf=etf,
                as_of=as_of,
                ticker=ticker,
                name=name,
                asset_class=asset_class,
                shares=units,
                price=price,
                market_value=market_value,
                weight=weight,
                currency=currency,
                sector=None,
                is_adr=is_adr if asset_class == EQUITY else None,
                company_id=None,
                source=SOURCE,
            )
        )

    if not holdings:
        raise ValueError("holdings: no row is a JSON object")

    declared = _int(data.get("totalNumberOfHoldings"))
    if declared is not None and declared != len(holdings):
        warnings.append(f"totalNumberOfHoldings={declared} but {len(holdings)} rows parsed")

    equity_count = sum(1 for h in holdings if h.asset_class == EQUITY)
    urls = dict(source_urls) if source_urls else {
        "holdings": HOLDINGS_URL.format(etf=etf),
        "fund_details": FUND_DETAILS_URL.format(etf=etf),
    }
    meta: dict[str, Any] = {
        "source": SOURCE,
        "etf": etf,
        "effective_date": effective_date.isoformat(),
        "effective_business_date": _text(data.get("effectiveBusinessDate")) or None,
        "as_of_basis": as_of_basis,
        "fund_identifier": identifier,
        "total_number_of_holdings": declared,
        "row_count": len(holdings),
        "equity_count": equity_count,
        "adr_tickers": adr_tickers,
        "weight_sum_pct": weight_sum_pct,
        "security_type_counts": type_counts,
        "nav": details.get("nav") if details else None,
        "shares_outstanding": details.get("shares_outstanding") if details else None,
        "total_net_assets": tna,
        "total_net_assets_basis": tna_basis,
        "fund_details_effective_date": details_date.isoformat() if details_date else None,
        "price_derivation": PRICE_DERIVATION if tna is not None else None,
        "synthesized_tickers": synthesized,
        "null_weight_tickers": null_weight,
        "url": urls.get("holdings"),
        "source_urls": urls,
        "warnings": warnings,
    }
    return Snapshot(etf=etf, as_of=as_of, source=SOURCE, holdings=holdings, meta=meta)


def validate_snapshot(snapshot: Snapshot) -> list[str]:
    """Plausibility checks :func:`fetch` applies before reporting ``ok``.

    Returns a list of problems (empty when the snapshot passes): fewer than
    :data:`MIN_EQUITY_ROWS` equity lines, or published weights summing
    outside ``[WEIGHT_SUM_MIN_PCT, WEIGHT_SUM_MAX_PCT]`` percent.
    """
    problems: list[str] = []
    equity_count = len(snapshot.equities())
    if equity_count < MIN_EQUITY_ROWS:
        problems.append(f"only {equity_count} equity rows parsed, expected at least {MIN_EQUITY_ROWS}")
    total_pct = 100.0 * sum(h.weight for h in snapshot.holdings if h.weight is not None)
    if not (WEIGHT_SUM_MIN_PCT <= total_pct <= WEIGHT_SUM_MAX_PCT):
        problems.append(
            f"weights sum to {total_pct:.4f}%, expected between {WEIGHT_SUM_MIN_PCT:g}% and {WEIGHT_SUM_MAX_PCT:g}%"
        )
    return problems


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def _check_user_agent(user_agent: str | None) -> None:
    if user_agent is not None and user_agent.lstrip().lower().startswith("mozilla/"):
        raise ValueError("a browser User-Agent must never be sent to dng-api.invesco.com")


def _get_with_ua_fallback(
    http_get: HttpGet, url: str, user_agents: tuple[str | None, ...]
) -> tuple[_http.Response, str | None]:
    """GET ``url`` trying each UA in turn while the edge answers a status in
    :data:`UA_FALLBACK_STATUSES`.  Returns ``(response, user_agent_used)``."""
    response: _http.Response | None = None
    used: str | None = None
    for user_agent in user_agents:
        _check_user_agent(user_agent)
        try:
            response = http_get(url, user_agent=user_agent)
        except Exception as exc:  # noqa: BLE001 - a transport that raises instead of returning a Response
            log.warning("invesco: request for %s raised %s: %s", url, type(exc).__name__, exc)
            response = _http.Response(url=url, status=0, error=f"{type(exc).__name__}: {exc}")
        used = user_agent
        if response.status not in UA_FALLBACK_STATUSES:
            break
        log.warning("invesco: HTTP %s for %s with User-Agent %r; trying the next UA", response.status, url, user_agent)
    assert response is not None
    return response, used


def _describe_failure(response: _http.Response) -> str:
    if response.error:
        return f"transport error: {response.error}"
    if not response.ok:
        return f"HTTP {response.status}" + (f" ({response.content_type})" if response.content_type else "")
    if not response.body.strip():
        return "HTTP 200 with an empty body"
    if response.looks_like_html():
        return "HTTP 200 but the body is HTML, not JSON"
    if len(response.body) > MAX_BODY_BYTES:
        return f"HTTP 200 but the body of {len(response.body)} bytes exceeds {MAX_BODY_BYTES}"
    return ""


def _ua_label(user_agent: str | None) -> str:
    return "urllib-default" if user_agent is None else ("project" if user_agent == _http.DEFAULT_USER_AGENT else user_agent)


def fetch(
    etf: str,
    as_of: date | str | None = None,
    *,
    http_get: HttpGet | None = None,
    now: float | None = None,
) -> FetchResult:
    """Download the current QQQ holdings and return a :class:`FetchResult`.

    Args:
        etf: ``'QQQ'`` (anything else -> ``unsupported``).
        as_of: requested holdings date.  ``None`` accepts whatever date the
            API serves.  A date earlier than the served ``effectiveDate`` ->
            ``unsupported`` (Invesco serves only the current snapshot); a
            later date -> ``no_data`` (not yet published); the same date ->
            ``ok``.
        http_get: transport with the signature of :func:`etf_tracker.http.get`
            (tests inject a fake).  Called as ``http_get(url, user_agent=...)``.
        now: Unix timestamp used for the ``cb`` cache-buster and
            ``meta['downloaded_at']`` (defaults to the current time).

    ``raw_files`` holds ``'.json'`` (holdings) and, when it was retrieved,
    ``'.fund.json'`` (fundDetails).  A fundDetails failure is non-fatal: the
    snapshot is returned without prices and ``meta['warnings']`` says why.
    Status ``error`` covers transport/HTTP failures, non-JSON or malformed
    bodies, fewer than :data:`MIN_EQUITY_ROWS` equity rows and weight sums
    outside 98-102%.  Raw bytes are kept on ``error`` results for debugging.
    """
    etf_upper = str(etf).strip().upper()
    requested = _coerce_requested_date(as_of)
    requested_iso = requested.isoformat() if requested else None
    if etf_upper not in ETFS:
        return FetchResult(
            etf=etf_upper,
            status=STATUS_UNSUPPORTED,
            message=f"Invesco source serves {', '.join(ETFS)} only, not {etf_upper}",
            source=SOURCE,
            requested_as_of=requested_iso,
        )

    get = _http.get if http_get is None else http_get
    ts = time.time() if now is None else float(now)
    urls = {"holdings": holdings_url(etf_upper, ts), "fund_details": fund_details_url(etf_upper, ts)}

    holdings_resp, ua_used = _get_with_ua_fallback(get, urls["holdings"], USER_AGENTS)
    failure = _describe_failure(holdings_resp)
    raw_files: dict[str, bytes] = {}
    if holdings_resp.body and len(holdings_resp.body) <= MAX_BODY_BYTES:
        raw_files[".json"] = bytes(holdings_resp.body)
    if failure:
        log.error("invesco: holdings download failed: %s (%s)", failure, urls["holdings"])
        return FetchResult(
            etf=etf_upper,
            status=STATUS_ERROR,
            raw_files=raw_files,
            message=f"holdings download failed: {failure}",
            source=SOURCE,
            requested_as_of=requested_iso,
        )

    # Start with the UA that worked for the holdings call, keep the fallback.
    ua_order = (ua_used,) + tuple(u for u in USER_AGENTS if u != ua_used)
    details_resp, _ = _get_with_ua_fallback(get, urls["fund_details"], ua_order)
    details_failure = _describe_failure(details_resp)
    fund_details_bytes: bytes | None = None
    extra_warnings: list[str] = []
    if details_failure:
        extra_warnings.append(f"fundDetails download failed: {details_failure}")
        log.warning("invesco: fundDetails download failed: %s (%s)", details_failure, urls["fund_details"])
    else:
        fund_details_bytes = bytes(details_resp.body)
        raw_files[".fund.json"] = fund_details_bytes

    try:
        snapshot = parse_holdings_json(holdings_resp.body, fund_details_bytes, etf_upper, source_urls=urls)
    except ValueError as e:
        log.error("invesco: holdings document rejected: %s", e)
        return FetchResult(
            etf=etf_upper,
            status=STATUS_ERROR,
            raw_files=raw_files,
            message=f"holdings document rejected: {e}",
            source=SOURCE,
            requested_as_of=requested_iso,
        )

    snapshot.meta["warnings"] = list(extra_warnings) + list(snapshot.meta.get("warnings", []))
    snapshot.meta["user_agent"] = _ua_label(ua_used)
    snapshot.meta["downloaded_at"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()

    served = snapshot.as_of
    if requested is not None and requested != served:
        if requested < served:
            status, reason = STATUS_UNSUPPORTED, (
                f"Invesco serves only the current snapshot (as of {served.isoformat()}); "
                f"historical holdings for {requested.isoformat()} are not available"
            )
        else:
            status, reason = STATUS_NO_DATA, (
                f"holdings for {requested.isoformat()} not yet published; "
                f"Invesco currently serves {served.isoformat()}"
            )
        log.info("invesco: %s", reason)
        return FetchResult(
            etf=etf_upper,
            status=status,
            raw_files=raw_files,
            message=reason,
            source=SOURCE,
            requested_as_of=requested_iso,
        )

    problems = validate_snapshot(snapshot)
    if problems:
        message = "; ".join(problems)
        log.error("invesco: snapshot as of %s rejected: %s", served.isoformat(), message)
        return FetchResult(
            etf=etf_upper,
            status=STATUS_ERROR,
            raw_files=raw_files,
            message=f"snapshot as of {served.isoformat()} rejected: {message}",
            source=SOURCE,
            requested_as_of=requested_iso,
        )

    message = (
        f"{etf_upper} holdings as of {served.isoformat()}: {len(snapshot.holdings)} rows, "
        f"{snapshot.meta['equity_count']} equities, weights sum {snapshot.meta['weight_sum_pct']:.4f}%"
        + (", no fundDetails (prices missing)" if fund_details_bytes is None else "")
    )
    log.info("invesco: %s (UA %s)", message, snapshot.meta["user_agent"])
    return FetchResult(
        etf=etf_upper,
        status=STATUS_OK,
        snapshot=snapshot,
        raw_files=raw_files,
        message=message,
        source=SOURCE,
        requested_as_of=requested_iso,
    )
