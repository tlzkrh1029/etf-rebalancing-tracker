"""Tests for etf_tracker.sources.ishares (SOXX / IGV holdings downloads).

Offline tests use the real files captured on 2026-10-09 under
``tests/fixtures`` plus small synthetic variants.  The live test at the end
only runs with ``ETF_TRACKER_NETWORK_TESTS=1``.
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

import etf_tracker.http
from etf_tracker.bridge import apply_caps_to_snapshot, constituents_from_snapshot
from etf_tracker.holdings import CASH, DERIVATIVE, EQUITY, load_snapshot, save_snapshot
from etf_tracker.http import Response
from etf_tracker.sources import STATUS_ERROR, STATUS_NO_DATA, STATUS_OK, STATUS_UNSUPPORTED, FetchResult
from etf_tracker.sources import ishares
from etf_tracker.sources.ishares import (
    EARLIEST_AS_OF,
    ETFS,
    MAX_FUTURE_DAYS,
    MIN_ROWS,
    PORTFOLIO_IDS,
    SOURCE_CSV,
    NON_ADR_FOREIGN_ORDINARIES,
    SOURCE_JSON,
    EmptyTemplateError,
    ParseError,
    build_csv_url,
    build_json_url,
    detect_empty_template,
    fetch,
    parse_csv,
    parse_product_data_json,
    validate_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOXX_CSV = FIXTURES / "SOXX_holdings_2026-10-08.csv"
IGV_CSV = FIXTURES / "IGV_holdings_2026-10-08.csv"
SOXX_JSON = FIXTURES / "SOXX_product-data_2026-10-07.json"
EMPTY_CSV = FIXTURES / "ishares_SOXX_empty-template.csv"
EMPTY_JSON = FIXTURES / "ishares_SOXX_product-data_empty.json"

HTML_BODY = b"<!DOCTYPE html>\n<html><head><title>iShares Semiconductor ETF | SOXX</title></head><body></body></html>"

#: ADR rows of SOXX recognised from their names.
SOXX_ADR_BY_NAME = {"SKHY", "ASML", "ASX", "UMC", "ARM", "STM"}
#: Non-US equities of SOXX without 'ADR' in the name (location heuristic).
SOXX_ADR_BY_LOCATION = ["TSM"]
#: Non-US equities the heuristic would flag but that are ordinary shares
#: (``NON_ADR_FOREIGN_ORDINARIES``): Tower Semiconductor's NASDAQ line.
SOXX_ADR_OVERRIDDEN = ["TSEM"]


@pytest.fixture(scope="module")
def soxx_csv_bytes() -> bytes:
    return SOXX_CSV.read_bytes()


@pytest.fixture(scope="module")
def soxx_csv_text() -> str:
    return SOXX_CSV.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def igv_csv_bytes() -> bytes:
    return IGV_CSV.read_bytes()


@pytest.fixture(scope="module")
def soxx_json_bytes() -> bytes:
    return SOXX_JSON.read_bytes()


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


def _csv_response(url: str, body: bytes, status: int = 200) -> Response:
    return Response(url, status, {"content-type": "text/csv"}, body)


def _json_response(url: str, body: bytes, status: int = 200) -> Response:
    return Response(url, status, {"content-type": "application/json"}, body)


class FakeHTTP:
    """``http_get`` stand-in: answers by endpoint (``csv`` / ``json``).

    Each answer is a ``Response``, a callable ``url -> Response``, an
    exception instance to raise, or ``None`` (``AssertionError`` when hit:
    the test did not expect that endpoint to be called).
    """

    def __init__(self, csv: object = None, json: object = None) -> None:
        self.answers = {"csv": csv, "json": json}
        self.calls: list[str] = []

    def __call__(self, url: str, **kwargs: object) -> Response:
        self.calls.append(url)
        if "get-fund-document" in url:
            kind = "csv"
        elif "get-product-data" in url:
            kind = "json"
        else:  # pragma: no cover
            raise AssertionError(f"unexpected url {url}")
        answer = self.answers[kind]
        if answer is None:
            raise AssertionError(f"{kind} endpoint was not expected to be called: {url}")
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(url)
        assert isinstance(answer, Response)
        return Response(url, answer.status, answer.headers, answer.body, answer.error)

    @property
    def kinds(self) -> list[str]:
        return ["csv" if "get-fund-document" in u else "json" for u in self.calls]


def _csv_ok(body: bytes) -> Callable[[str], Response]:
    return lambda url: _csv_response(url, body)


def _json_ok(body: bytes) -> Callable[[str], Response]:
    return lambda url: _json_response(url, body)


# ---------------------------------------------------------------------------
# Module constants and URLs
# ---------------------------------------------------------------------------


def test_module_contract() -> None:
    assert ETFS == ("SOXX", "IGV")
    assert PORTFOLIO_IDS == {"SOXX": "239705", "IGV": "239771"}
    assert MIN_ROWS == {"SOXX": 20, "IGV": 60}
    assert callable(fetch)


def test_build_csv_url_latest_and_dated() -> None:
    latest = build_csv_url("SOXX")
    assert latest.startswith("https://www.blackrock.com/varnish-api/")
    assert "portfolioId=239705" in latest
    assert "component=holdings" in latest
    assert "asOfDate" not in latest
    dated = build_csv_url("igv", date(2026, 10, 8))
    assert "portfolioId=239771" in dated
    assert dated.endswith("&asOfDate=20261008")
    assert build_csv_url("IGV", "2026-10-08") == dated
    assert build_csv_url("IGV", "20261008") == dated


def test_build_json_url() -> None:
    url = build_json_url("IGV")
    assert url.startswith("https://www.ishares.com/varnish-api/")
    assert "portfolioId=239771" in url
    assert "component=holdings.all" in url
    assert "asOfDate" not in url
    assert build_json_url("SOXX", date(2025, 12, 31)).endswith("&asOfDate=20251231")


def test_build_url_rejects_unknown_etf_and_bad_dates() -> None:
    with pytest.raises(ValueError):
        build_csv_url("QQQ")
    with pytest.raises(ValueError):
        build_json_url("SPY")
    with pytest.raises(ValueError):
        build_csv_url("SOXX", "2026/10/08")
    with pytest.raises(TypeError):
        build_csv_url("SOXX", 20261008)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_csv: the real SOXX file
# ---------------------------------------------------------------------------


def test_parse_soxx_csv_header_and_meta(soxx_csv_bytes: bytes) -> None:
    snap = parse_csv(soxx_csv_bytes, "SOXX")
    assert snap.etf == "SOXX"
    assert snap.as_of == date(2026, 10, 8)
    assert snap.source == SOURCE_CSV
    assert len(snap.holdings) == 34
    assert len(snap.equities()) == 30
    meta = snap.meta
    assert meta["fund_name"] == "iShares Semiconductor ETF"
    assert meta["shares_outstanding"] == pytest.approx(82_700_000.0)
    assert meta["inception_date"] == "2001-07-10"
    assert meta["raw_header_columns"][:2] == ["Ticker", "Name"]
    assert "Quantity" in meta["raw_header_columns"]
    assert meta["shares_column"] == "Quantity"
    assert meta["row_count"] == 34
    assert meta["equity_count"] == 30
    assert meta["weight_sum_pct"] == pytest.approx(100.0, abs=0.5)
    assert meta["format"] == "csv"
    assert "parse_warnings" not in meta


def test_parse_soxx_csv_first_row(soxx_csv_bytes: bytes) -> None:
    snap = parse_csv(soxx_csv_bytes, "SOXX")
    amd = snap.holdings[0]
    assert amd.ticker == "AMD"
    assert amd.name == "ADVANCED MICRO DEVICES"
    assert amd.asset_class == EQUITY
    assert amd.weight == pytest.approx(0.0950)
    assert amd.shares == pytest.approx(7_067_090.0)
    assert amd.price == pytest.approx(620.68)
    assert amd.market_value == pytest.approx(4_386_401_421.20)
    assert amd.sector == "Information Technology"
    assert amd.currency == "USD"
    assert amd.is_adr is False
    assert amd.company_id is None
    assert amd.source == SOURCE_CSV
    assert amd.etf == "SOXX" and amd.as_of == date(2026, 10, 8)


def test_parse_soxx_csv_adr_flags(soxx_csv_bytes: bytes) -> None:
    snap = parse_csv(soxx_csv_bytes, "SOXX")
    flagged = {h.ticker for h in snap.holdings if h.is_adr}
    assert flagged == SOXX_ADR_BY_NAME | set(SOXX_ADR_BY_LOCATION)
    assert snap.meta["adr_heuristic"] == SOXX_ADR_BY_LOCATION
    assert snap.meta["adr_overrides"] == SOXX_ADR_OVERRIDDEN
    by = snap.by_ticker()
    assert by["TSEM"].is_adr is False  # Israeli ordinary shares on NASDAQ, not an ADR
    for ticker in ("UMC", "ARM", "STM"):
        assert by[ticker].is_adr is True
    for ticker in ("AMD", "INTC", "NVDA", "CBRS"):
        assert by[ticker].is_adr is False
    # non-equity lines are never ADRs
    assert all(h.is_adr is False for h in snap.non_equities())


def test_parse_soxx_csv_cash_and_derivative_rows(soxx_csv_bytes: bytes) -> None:
    snap = parse_csv(soxx_csv_bytes, "SOXX")
    by = snap.by_ticker()
    assert {h.ticker: h.asset_class for h in snap.non_equities()} == {
        "XTSLA": CASH,
        "USD": CASH,
        "WFFUT": CASH,
        "IXTZ6": DERIVATIVE,
    }
    # money market fund: price 1.00 is consistent, left alone
    assert by["XTSLA"].price == pytest.approx(1.0)
    assert by["XTSLA"].market_value == pytest.approx(55_500_154.41)
    # USD CASH is published at a nominal 100.00; market value is authoritative
    usd = by["USD"]
    assert usd.shares == pytest.approx(8_860_136.0)
    assert usd.market_value == pytest.approx(8_860_135.63)
    assert usd.price == pytest.approx(8_860_135.63 / 8_860_136.0)
    assert usd.weight == pytest.approx(0.0002)
    assert snap.meta["price_corrected"] == ["USD", "WFFUT"]
    # futures: market value 0, notional kept in meta
    fut = by["IXTZ6"]
    assert fut.market_value == 0.0
    assert fut.weight == 0.0
    assert fut.shares == pytest.approx(190.0)
    assert fut.price == pytest.approx(4_015.20)
    assert fut.sector == "Cash and/or Derivatives"
    assert snap.meta["derivative_notional"] == {"IXTZ6": pytest.approx(76_288_800.0)}


def test_parse_soxx_csv_is_consistent_for_holdings_validate(soxx_csv_bytes: bytes) -> None:
    snap = parse_csv(soxx_csv_bytes, "SOXX")
    assert snap.validate() == []
    assert validate_snapshot(snap, "SOXX") == []
    assert snap.meta["validation_warnings"] == []
    weights = snap.normalized_weights(include_cash=False)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["AMD"] == pytest.approx(0.0950, abs=0.001)


def test_parse_soxx_csv_feeds_the_rules_bridge(soxx_csv_bytes: bytes) -> None:
    snap = parse_csv(soxx_csv_bytes, "SOXX")
    constituents = constituents_from_snapshot(snap, "SOXX")
    assert len(constituents) == 30
    assert sum(c.weight for c in constituents) == pytest.approx(1.0)
    adrs = {c.ticker for c in constituents if c.is_adr}
    assert {"UMC", "ARM", "STM", "TSM"} <= adrs
    result = apply_caps_to_snapshot(snap, "SOXX")
    assert result.metrics["adr_sum"] > 0.0
    assert set(result.target_weights) == {c.ticker for c in constituents}


def test_parse_soxx_csv_round_trips_through_snapshot_json(soxx_csv_bytes: bytes, tmp_path: Path) -> None:
    snap = parse_csv(soxx_csv_bytes, "SOXX")
    path = save_snapshot(snap, tmp_path / "SOXX" / "2026-10-08.json")
    loaded = load_snapshot(path)
    assert len(loaded.holdings) == 34
    assert loaded.meta["derivative_notional"] == {"IXTZ6": 76_288_800.0}
    assert loaded.meta["adr_heuristic"] == SOXX_ADR_BY_LOCATION
    assert loaded.by_ticker()["UMC"].is_adr is True


# ---------------------------------------------------------------------------
# parse_csv: the real IGV file and format variants
# ---------------------------------------------------------------------------


def test_parse_igv_csv(igv_csv_bytes: bytes) -> None:
    snap = parse_csv(igv_csv_bytes, "IGV")
    assert snap.etf == "IGV"
    assert snap.as_of == date(2026, 10, 8)
    assert len(snap.holdings) == 111
    assert snap.meta["row_count"] == 111
    assert snap.meta["equity_count"] == 106
    assert len(snap.equities()) == 106
    assert snap.meta["fund_name"] == "iShares Expanded Tech-Software Sector ETF"
    assert snap.meta["shares_outstanding"] == pytest.approx(133_600_000.0)
    assert snap.holdings[0].ticker == "PANW"
    assert snap.holdings[0].weight == pytest.approx(0.0941)
    assert set(snap.meta["derivative_notional"]) == {"RTYZ6", "IXTZ6"}
    # Canadian ordinary shares are flagged by the location heuristic only
    assert snap.meta["adr_heuristic"] == ["DSGX", "OTEX", "BB", "LSPD"]
    assert validate_snapshot(snap, "IGV") == []
    assert len(snap.holdings) >= MIN_ROWS["IGV"]


def test_parse_csv_accepts_shares_column_variant(soxx_csv_text: str) -> None:
    variant = soxx_csv_text.replace("Quantity", "Shares", 1)
    assert "Quantity" not in variant
    snap = parse_csv(variant, "SOXX")
    assert snap.meta["shares_column"] == "Shares"
    assert snap.holdings[0].shares == pytest.approx(7_067_090.0)
    assert len(snap.holdings) == 34


def test_parse_csv_strips_bom_and_accepts_crlf(soxx_csv_text: str) -> None:
    with_bom = b"\xef\xbb\xbf" + soxx_csv_text.encode("utf-8")
    snap = parse_csv(with_bom, "SOXX")
    assert snap.meta["fund_name"] == "iShares Semiconductor ETF"
    assert len(snap.holdings) == 34
    crlf = soxx_csv_text.replace("\n", "\r\n").encode("utf-8")
    snap2 = parse_csv(crlf, "SOXX")
    assert len(snap2.holdings) == 34
    assert snap2.holdings[-1].ticker == "IXTZ6"
    # a str with the BOM character is fine too
    snap3 = parse_csv("﻿" + soxx_csv_text, "SOXX")
    assert snap3.as_of == date(2026, 10, 8)


def test_parse_csv_stops_at_footer(soxx_csv_text: str) -> None:
    footer = (
        soxx_csv_text.rstrip("\n")
        + "\n\n"
        + '"The content contained herein is owned or licensed by BlackRock, Inc."\n'
        + "Holdings subject to change, see, the prospectus\n"
        + '"FOOT","NOT A HOLDING","Information Technology","Equity","1.00","99.00","1.00","1.00","1.00","United States","NYSE","USD","1.00","USD","-"\n'
    )
    snap = parse_csv(footer, "SOXX")
    assert len(snap.holdings) == 34
    assert "FOOT" not in snap.by_ticker()
    assert snap.meta["weight_sum_pct"] == pytest.approx(100.0, abs=0.5)


def test_parse_csv_quoted_numbers_and_dashes(soxx_csv_text: str) -> None:
    header_end = soxx_csv_text.index("Ticker,Name")
    preamble = soxx_csv_text[:header_end]
    header = soxx_csv_text[header_end:].splitlines()[0]
    rows = [
        '"AAA","ALPHA","Information Technology","Equity","1,000.50","50.00","1,000.50","(10.00)","-","United States","NYSE","USD","1.00","USD","-"',
        '"","NO TICKER CORP","Information Technology","Equity","1,000.50","50.00","1,000.50","10.00","100.05","Canada","TSX","CAD","1.00","CAD","-"',
        '"BBB","BETA","-","Other Stuff","-","-","-","abc","-","-","-","-","-","-","-"',
    ]
    text = preamble + header + "\n" + "\n".join(rows) + "\n"
    snap = parse_csv(text, "SOXX")
    aaa, synthetic, bbb = snap.holdings
    assert aaa.shares == pytest.approx(-10.0)
    assert aaa.price is None
    assert aaa.market_value == pytest.approx(1000.5)
    assert aaa.weight == pytest.approx(0.5)
    assert synthetic.ticker == "_NO_TICKER_CORP"
    assert synthetic.currency == "CAD"
    assert synthetic.is_adr is True  # location heuristic
    assert snap.meta["synthesized_tickers"] == ["_NO_TICKER_CORP"]
    assert snap.meta["adr_heuristic"] == ["_NO_TICKER_CORP"]
    assert bbb.asset_class == "other"
    assert bbb.sector is None and bbb.weight is None and bbb.market_value is None
    assert bbb.shares is None
    assert any("BBB" in w and "shares" in w for w in snap.meta["parse_warnings"])


def test_parse_csv_rejects_non_holdings_bodies() -> None:
    with pytest.raises(ParseError):
        parse_csv(b"hello,world\n1,2\n", "SOXX")
    with pytest.raises(ParseError):
        parse_csv(HTML_BODY, "SOXX")
    with pytest.raises(ParseError):
        parse_csv(b"", "SOXX")
    with pytest.raises(ParseError):
        parse_csv(b"\xef\xbb\xbf" + b"x" * (ishares.MAX_BODY_BYTES + 1), "SOXX")
    with pytest.raises(TypeError):
        parse_csv(123, "SOXX")  # type: ignore[arg-type]


def test_parse_csv_requires_known_columns(soxx_csv_text: str) -> None:
    no_shares = soxx_csv_text.replace("Quantity", "Units", 1)
    with pytest.raises(ParseError, match="Quantity"):
        parse_csv(no_shares, "SOXX")
    no_weight = soxx_csv_text.replace("Weight (%)", "Wt", 1)
    with pytest.raises(ParseError, match="Weight"):
        parse_csv(no_weight, "SOXX")


# ---------------------------------------------------------------------------
# Empty template
# ---------------------------------------------------------------------------


def test_detect_empty_template_on_real_template() -> None:
    body = EMPTY_CSV.read_bytes()
    assert b'Fund Holdings as of,"-"' in body
    assert detect_empty_template(body) is True
    assert detect_empty_template(body.decode()) is True
    with pytest.raises(EmptyTemplateError):
        parse_csv(body, "SOXX")


def test_detect_empty_template_on_zero_rows(soxx_csv_text: str) -> None:
    header_end = soxx_csv_text.index("Ticker,Name")
    header_line = soxx_csv_text[header_end:].splitlines()[0]
    zero_rows = soxx_csv_text[:header_end] + header_line + "\n\n"
    assert detect_empty_template(zero_rows) is True
    with pytest.raises(EmptyTemplateError):
        parse_csv(zero_rows, "SOXX")


def test_detect_empty_template_is_false_for_real_files_and_garbage(soxx_csv_bytes: bytes, igv_csv_bytes: bytes) -> None:
    assert detect_empty_template(soxx_csv_bytes) is False
    assert detect_empty_template(igv_csv_bytes) is False
    assert detect_empty_template(HTML_BODY) is False
    assert detect_empty_template(b"") is False
    assert detect_empty_template(b"random,text\n") is False


# ---------------------------------------------------------------------------
# parse_product_data_json
# ---------------------------------------------------------------------------


def test_parse_product_data_json_fixture(soxx_json_bytes: bytes) -> None:
    snap = parse_product_data_json(soxx_json_bytes, "SOXX")
    assert snap.etf == "SOXX"
    assert snap.as_of == date(2026, 10, 7)
    assert snap.source == SOURCE_JSON
    assert len(snap.holdings) == 34
    assert len(snap.equities()) == 30
    amd = snap.holdings[0]
    assert amd.ticker == "AMD"
    assert amd.name == "ADVANCED MICRO DEVICES"
    assert amd.weight == pytest.approx(0.0954726)
    assert amd.shares == pytest.approx(7_127_416.0)
    assert amd.price == pytest.approx(645.86)
    assert amd.market_value == pytest.approx(4_603_312_897.76)
    assert amd.sector == "Information Technology"
    assert amd.is_adr is False
    meta = snap.meta
    assert meta["fund_name"] == "iShares Semiconductor ETF"
    assert meta["portfolio_id"] == "239705"
    assert meta["shares_outstanding"] is None
    assert meta["inception_date"] is None
    assert meta["format"] == "json"
    assert meta["row_count"] == 34 and meta["equity_count"] == 30
    assert meta["weight_sum_pct"] == pytest.approx(100.0, abs=0.01)
    assert "ticker" in meta["raw_header_columns"] and "holdingPercent" in meta["raw_header_columns"]
    assert meta["available_dates"] == ["2026-10-08", "2026-09-30", "2025-12-31"]
    assert meta["identifiers"]["AMD"] == {"isin": "US0079031078", "cusip": "007903107", "sedol": "2007849"}
    assert "USD" not in meta["identifiers"]
    assert validate_snapshot(snap, "SOXX") == []


def test_parse_product_data_json_matches_csv_semantics(soxx_json_bytes: bytes, soxx_csv_bytes: bytes) -> None:
    from_json = parse_product_data_json(soxx_json_bytes, "SOXX")
    from_csv = parse_csv(soxx_csv_bytes, "SOXX")
    # one day apart, so the (weight-sorted) order differs; compare per ticker
    assert set(from_json.tickers()) == set(from_csv.tickers())
    json_by, csv_by = from_json.by_ticker(), from_csv.by_ticker()
    assert {t: h.asset_class for t, h in json_by.items()} == {t: h.asset_class for t, h in csv_by.items()}
    assert {t: h.is_adr for t, h in json_by.items()} == {t: h.is_adr for t, h in csv_by.items()}
    assert {t: h.name for t, h in json_by.items()} == {t: h.name for t, h in csv_by.items()}
    assert set(from_json.meta["adr_heuristic"]) == set(from_csv.meta["adr_heuristic"])
    assert from_json.meta["price_corrected"] == from_csv.meta["price_corrected"]
    assert set(from_json.meta["derivative_notional"]) == set(from_csv.meta["derivative_notional"])
    assert from_json.meta["derivative_notional"]["IXTZ6"] == pytest.approx(77_725_200.0)
    usd = from_json.by_ticker()["USD"]
    assert usd.asset_class == CASH
    assert usd.price == pytest.approx(11_872_480.84 / 11_872_481.0)
    fut = from_json.by_ticker()["IXTZ6"]
    assert fut.asset_class == DERIVATIVE and fut.market_value == 0.0
    assert from_json.validate() == []


def test_parse_product_data_json_accepts_dict_and_string_as_of(soxx_json_bytes: bytes) -> None:
    data = json.loads(soxx_json_bytes)
    points = data["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"]["dataPointsByNameMap"]
    points["asOfDate"]["value"] = "20261007"
    snap = parse_product_data_json(data, "soxx")
    assert snap.as_of == date(2026, 10, 7)
    assert snap.etf == "SOXX"
    snap2 = parse_product_data_json(json.dumps(data), "SOXX")
    assert len(snap2.holdings) == 34


def test_parse_product_data_json_empty_template() -> None:
    body = EMPTY_JSON.read_bytes()
    with pytest.raises(EmptyTemplateError):
        parse_product_data_json(body, "SOXX")
    data = json.loads(body)
    points = data["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"]["dataPointsByNameMap"]
    assert points["ticker"]["value"] is None  # what the live endpoint returns for an unpublished date
    points["ticker"]["value"] = []
    with pytest.raises(EmptyTemplateError):
        parse_product_data_json(data, "SOXX")


def test_parse_product_data_json_rejects_bad_structures(soxx_json_bytes: bytes) -> None:
    with pytest.raises(ParseError):
        parse_product_data_json(b"not json", "SOXX")
    with pytest.raises(ParseError):
        parse_product_data_json(b"[1, 2, 3]", "SOXX")
    with pytest.raises(ParseError):
        parse_product_data_json(b'{"componentsByNameMap": {}}', "SOXX")
    data = json.loads(soxx_json_bytes)
    points = data["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"]["dataPointsByNameMap"]
    points["asOfDate"]["value"] = "yesterday"
    with pytest.raises(ParseError, match="asOfDate"):
        parse_product_data_json(data, "SOXX")


def test_parse_product_data_json_pads_short_columns(soxx_json_bytes: bytes) -> None:
    data = json.loads(soxx_json_bytes)
    points = data["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"]["dataPointsByNameMap"]
    points["unitPrice"]["value"] = points["unitPrice"]["value"][:5]
    del points["sectorName"]
    snap = parse_product_data_json(data, "SOXX")
    assert len(snap.holdings) == 34
    assert snap.holdings[0].price == pytest.approx(645.86)
    assert snap.holdings[10].price is None
    assert snap.holdings[0].sector is None
    assert any("unitPrice" in w for w in snap.meta["parse_warnings"])


# ---------------------------------------------------------------------------
# validate_snapshot
# ---------------------------------------------------------------------------


def test_validate_snapshot_problems(soxx_csv_bytes: bytes, soxx_csv_text: str) -> None:
    snap = parse_csv(soxx_csv_bytes, "SOXX")
    assert validate_snapshot(snap, "SOXX", date(2026, 10, 8)) == []
    mismatch = validate_snapshot(snap, "SOXX", date(2026, 10, 7))
    assert len(mismatch) == 1 and "2026-10-07" in mismatch[0]
    few = validate_snapshot(snap, "IGV")  # IGV needs 60 rows
    assert any("34 holding rows" in p for p in few)
    heavy = parse_csv(soxx_csv_text.replace('"9.50"', '"50.50"', 1), "SOXX")
    problems = validate_snapshot(heavy, "SOXX")
    assert len(problems) == 1 and "weights sum" in problems[0]


# ---------------------------------------------------------------------------
# fetch with an injected transport
# ---------------------------------------------------------------------------


def test_fetch_csv_success(soxx_csv_bytes: bytes) -> None:
    http = FakeHTTP(csv=_csv_ok(soxx_csv_bytes))
    result = fetch("SOXX", http_get=http)
    assert isinstance(result, FetchResult)
    assert result.ok and result.status == STATUS_OK
    assert result.etf == "SOXX"
    assert result.source == SOURCE_CSV
    assert result.requested_as_of is None
    assert result.raw_files == {".csv": soxx_csv_bytes}
    assert http.kinds == ["csv"]
    assert http.calls[0] == build_csv_url("SOXX")
    assert "via CSV" in result.message and "34 rows" in result.message and "30 equities" in result.message
    snap = result.snapshot
    assert snap is not None and snap.as_of == date(2026, 10, 8)
    assert snap.meta["url"] == build_csv_url("SOXX")
    assert snap.meta["route"] == "csv"
    assert snap.meta["http_status"] == 200
    assert snap.meta["portfolio_id"] == "239705"
    assert snap.meta["requested_as_of"] is None
    assert snap.meta["downloaded_at"].endswith("+00:00")
    assert snap.meta["validation_warnings"] == []


def test_fetch_lowercase_etf_and_dated_request(soxx_csv_bytes: bytes) -> None:
    http = FakeHTTP(csv=_csv_ok(soxx_csv_bytes))
    result = fetch("soxx", "2026-10-08", http_get=http)
    assert result.ok and result.etf == "SOXX"
    assert result.requested_as_of == "2026-10-08"
    assert http.calls == [build_csv_url("SOXX", date(2026, 10, 8))]
    assert http.calls[0].endswith("asOfDate=20261008")
    assert result.snapshot is not None and result.snapshot.meta["requested_as_of"] == "2026-10-08"


def test_fetch_igv(igv_csv_bytes: bytes) -> None:
    http = FakeHTTP(csv=_csv_ok(igv_csv_bytes))
    result = fetch("IGV", date(2026, 10, 8), http_get=http)
    assert result.ok
    assert result.snapshot is not None and len(result.snapshot.holdings) == 111
    assert "portfolioId=239771" in http.calls[0]


def test_fetch_unsupported_etf() -> None:
    http = FakeHTTP()
    result = fetch("QQQ", http_get=http)
    assert result.status == STATUS_UNSUPPORTED and not result.ok
    assert result.snapshot is None and result.raw_files == {}
    assert "QQQ" in result.message
    assert http.calls == []


def test_fetch_falls_back_to_json_on_http_error(soxx_json_bytes: bytes) -> None:
    http = FakeHTTP(csv=lambda url: _csv_response(url, b"Forbidden", 403), json=_json_ok(soxx_json_bytes))
    result = fetch("SOXX", http_get=http)
    assert result.ok and result.source == SOURCE_JSON
    assert http.kinds == ["csv", "json"]
    assert http.calls[1] == build_json_url("SOXX")
    assert result.raw_files == {".json": soxx_json_bytes}
    assert "HTTP 403" in result.message and "via JSON" in result.message
    assert result.snapshot is not None and result.snapshot.as_of == date(2026, 10, 7)
    assert result.snapshot.meta["route"] == "json"


def test_fetch_falls_back_to_json_on_html_body(soxx_json_bytes: bytes) -> None:
    http = FakeHTTP(csv=_csv_ok(HTML_BODY), json=_json_ok(soxx_json_bytes))
    result = fetch("SOXX", http_get=http)
    assert result.ok and result.source == SOURCE_JSON
    assert http.kinds == ["csv", "json"]
    assert ".csv" not in result.raw_files  # the HTML page is not archived as a CSV
    assert "HTML" in result.message


def test_fetch_falls_back_to_json_on_unparseable_csv(soxx_json_bytes: bytes) -> None:
    http = FakeHTTP(csv=_csv_ok(b"garbage,without,header\n1,2,3\n"), json=_json_ok(soxx_json_bytes))
    result = fetch("SOXX", http_get=http)
    assert result.ok and result.source == SOURCE_JSON
    assert result.raw_files == {".json": soxx_json_bytes}
    assert "parse failed" in result.message


def test_fetch_falls_back_on_transport_error_and_exception(soxx_json_bytes: bytes) -> None:
    http = FakeHTTP(
        csv=lambda url: Response(url, 0, {}, b"", "URLError: connection refused"),
        json=_json_ok(soxx_json_bytes),
    )
    result = fetch("SOXX", http_get=http)
    assert result.ok and "transport error" in result.message
    http2 = FakeHTTP(csv=RuntimeError("boom"), json=_json_ok(soxx_json_bytes))
    result2 = fetch("SOXX", http_get=http2)
    assert result2.ok and "RuntimeError" in result2.message


def test_fetch_empty_csv_template_is_no_data_without_json_call() -> None:
    body = EMPTY_CSV.read_bytes()
    http = FakeHTTP(csv=_csv_ok(body))
    result = fetch("SOXX", date(2026, 10, 4), http_get=http)
    assert result.status == STATUS_NO_DATA and not result.ok
    assert result.snapshot is None
    assert result.raw_files == {".csv": body}
    assert result.requested_as_of == "2026-10-04"
    assert "2026-10-04" in result.message
    assert http.kinds == ["csv"]


def test_fetch_empty_json_template_is_no_data() -> None:
    body = EMPTY_JSON.read_bytes()
    http = FakeHTTP(csv=lambda url: _csv_response(url, b"", 503), json=_json_ok(body))
    result = fetch("SOXX", date(2026, 10, 9), http_get=http)
    assert result.status == STATUS_NO_DATA
    assert result.raw_files == {".json": body}
    assert "HTTP 503" in result.message and "empty template" in result.message
    assert http.kinds == ["csv", "json"]


def test_fetch_both_routes_failing_is_error() -> None:
    http = FakeHTTP(
        csv=lambda url: _csv_response(url, b"", 403),
        json=lambda url: _json_response(url, b"<html><body>blocked</body></html>", 200),
    )
    result = fetch("IGV", http_get=http)
    assert result.status == STATUS_ERROR and not result.ok
    assert result.raw_files == {}
    assert "CSV: HTTP 403" in result.message and "JSON: HTML" in result.message
    assert "all routes failed" in result.message
    http2 = FakeHTTP(csv=lambda url: _csv_response(url, b"", 404), json=lambda url: _json_response(url, b"{}", 200))
    result2 = fetch("IGV", http_get=http2)
    assert result2.status == STATUS_ERROR and "parse failed" in result2.message


def test_fetch_rejects_too_few_rows_without_fallback(soxx_csv_text: str) -> None:
    lines = soxx_csv_text.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("Ticker,Name"))
    short = "\n".join(lines[: header + 6]) + "\n"
    http = FakeHTTP(csv=_csv_ok(short.encode()))
    result = fetch("SOXX", http_get=http)
    assert result.status == STATUS_ERROR
    assert "only 5 holding rows" in result.message
    assert http.kinds == ["csv"]
    assert result.raw_files == {".csv": short.encode()}


def test_fetch_rejects_weight_sum_out_of_range(soxx_csv_text: str) -> None:
    heavy = soxx_csv_text.replace('"9.50"', '"50.50"', 1).encode()
    http = FakeHTTP(csv=_csv_ok(heavy))
    result = fetch("SOXX", http_get=http)
    assert result.status == STATUS_ERROR and "weights sum" in result.message


def test_fetch_rejects_as_of_mismatch(soxx_csv_bytes: bytes, soxx_json_bytes: bytes) -> None:
    http = FakeHTTP(csv=_csv_ok(soxx_csv_bytes))
    result = fetch("SOXX", date(2026, 10, 7), http_get=http)
    assert result.status == STATUS_ERROR
    assert "differs from the requested 2026-10-07" in result.message
    assert http.calls[0].endswith("asOfDate=20261007")
    # the JSON twin answers an unpublished date with the latest as-of and no rows -> no_data,
    # but if it ever answered with rows of another date that is an error too
    http2 = FakeHTTP(csv=lambda url: _csv_response(url, b"", 500), json=_json_ok(soxx_json_bytes))
    result2 = fetch("SOXX", date(2026, 10, 6), http_get=http2)
    assert result2.status == STATUS_ERROR and "2026-10-07" in result2.message


def test_fetch_uses_project_http_get_by_default(monkeypatch: pytest.MonkeyPatch, soxx_csv_bytes: bytes) -> None:
    http = FakeHTTP(csv=_csv_ok(soxx_csv_bytes))
    monkeypatch.setattr(etf_tracker.http, "get", http)
    result = fetch("SOXX")
    assert result.ok and http.kinds == ["csv"]


# ---------------------------------------------------------------------------
# Adversarial review (2026-10-09): every real raw file, and hostile variants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
#: SOXX ADRs before and after the 2026-09-18 annual reconstitution (SKHY
#: entered; NVMI, RMBS and SWKS left).  NVMI and TSEM are Israeli ordinary
#: shares on NASDAQ and must never count against the 10% ADR cap.
SOXX_ADR_BEFORE_RECON = frozenset({"ARM", "ASML", "ASX", "STM", "TSM", "UMC"})
SOXX_ADR_AFTER_RECON = SOXX_ADR_BEFORE_RECON | {"SKHY"}
SOXX_RECON_TRADE_DATE = date(2026, 9, 18)
#: The following annual reconstitution trades on the third Friday of
#: September 2027; the pinned ADR set is only asserted before it.
SOXX_NEXT_RECON_TRADE_DATE = date(2027, 9, 17)
TODAY = date(2026, 10, 9)  # capture date of the fixtures and raw files

FOOTER_NO_BLANK = (
    '"The content contained herein is owned or licensed by BlackRock, Inc. and/or its '
    'third-party information providers and is protected by applicable copyrights."\n'
    "Holdings subject to change\n"
)


def _raw_csv_files() -> list[Path]:
    if not RAW_DIR.is_dir():
        return []
    return sorted(p for etf in ETFS for p in (RAW_DIR / etf).glob("????-??-??.csv"))


def _table_rows(text: str) -> list[list[str]]:
    """Independent reading of the table: header line to the first blank line."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Ticker,"))
    rows: list[list[str]] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        rows.append(next(csv.reader([line])))
    return rows


def _raw_id(path: Path | None) -> str:
    return f"{path.parent.name}/{path.stem}" if path else "no-raw-data"


@pytest.mark.parametrize("path", _raw_csv_files() or [None], ids=_raw_id)
def test_every_raw_csv_parses_consistently(path: Path | None) -> None:
    if path is None:
        pytest.skip("data/raw is not present in this checkout")
    etf = path.parent.name
    body = path.read_bytes()
    snap = parse_csv(body, etf)
    assert snap.etf == etf
    assert snap.as_of == date.fromisoformat(path.stem), "as-of must match the file name"
    rows = _table_rows(body.decode("utf-8-sig"))
    assert len(snap.holdings) == len(rows) >= MIN_ROWS[etf]
    assert snap.tickers() == [r[0] for r in rows], "no row dropped, renamed or reordered"
    assert len(set(snap.tickers())) == len(snap.holdings), "duplicate tickers"
    assert "synthesized_tickers" not in snap.meta
    assert "parse_warnings" not in snap.meta
    assert {h.asset_class for h in snap.holdings} <= {EQUITY, CASH, DERIVATIVE}
    assert {r[3] for r in rows} <= {"Equity", "Money Market", "Cash", "Cash Collateral and Margins", "Futures"}
    assert snap.meta["weight_sum_pct"] == pytest.approx(100.0, abs=0.3)
    assert snap.meta["shares_outstanding"] > 1_000_000
    for h in snap.equities():
        assert h.shares and h.price and h.market_value and h.weight, h.ticker
        assert h.shares > 0 and h.price > 0 and h.weight > 0, h.ticker
        assert h.market_value == pytest.approx(h.shares * h.price, rel=0.005), h.ticker
        assert h.currency == "USD"
    for h in snap.non_equities():
        assert h.is_adr is False
        assert h.weight is not None  # USD CASH is regularly overdrawn (negative) in both funds
    for fut in (h for h in snap.holdings if h.asset_class == DERIVATIVE):
        assert fut.market_value == 0.0 and fut.weight == 0.0
        assert snap.meta["derivative_notional"][fut.ticker] > 0
    for cash in (h for h in snap.holdings if h.asset_class == CASH):
        # nominal 100.00 corrected to ~1.00 from the authoritative market value, overdrafts included
        assert cash.price == pytest.approx(cash.market_value / cash.shares)
        assert 0.99 < cash.price < 1.01, cash.ticker
    # the file's own date is the clock: the committed history keeps growing past TODAY
    assert validate_snapshot(snap, etf, snap.as_of, today=snap.as_of) == []
    assert snap.meta["validation_warnings"] == []


def test_soxx_adr_set_is_stable_between_reconstitutions() -> None:
    files = [p for p in _raw_csv_files() if p.parent.name == "SOXX"]
    if not files:
        pytest.skip("data/raw is not present in this checkout")
    for path in files:
        snap = parse_csv(path.read_bytes(), "SOXX")
        flagged = {h.ticker for h in snap.equities() if h.is_adr}
        by = snap.by_ticker()
        if snap.as_of < SOXX_RECON_TRADE_DATE:
            assert flagged == SOXX_ADR_BEFORE_RECON, path.name
        elif snap.as_of < SOXX_NEXT_RECON_TRADE_DATE:
            assert flagged == SOXX_ADR_AFTER_RECON, path.name
        else:  # after the next reconstitution only the invariants are known
            assert flagged <= {h.ticker for h in snap.equities()}, path.name
        if "TSM" in by:  # the only ADR whose name carries no marker
            assert "TSM" in flagged and "TSM" in snap.meta["adr_heuristic"], path.name
        for ticker in ("TSEM", "NVMI"):
            if ticker in by:
                assert by[ticker].is_adr is False, f"{ticker} in {path.name}"
                assert ticker in snap.meta["adr_overrides"], f"{ticker} in {path.name}"


def test_israeli_ordinary_shares_are_never_adrs(soxx_csv_text: str) -> None:
    assert {"TSEM", "NVMI"} <= NON_ADR_FOREIGN_ORDINARIES
    header_end = soxx_csv_text.index("Ticker,Name")
    header = soxx_csv_text[header_end:].splitlines()[0]
    rows = [
        '"NVMI","NOVA","Information Technology","Equity","269,647,044.44","50.00","269,647,044.44","774,692.00","348.07","Israel","NASDAQ","USD","1.00","USD","-"',
        '"NICE","NICE LTD ADR","Information Technology","Equity","269,647,044.44","50.00","269,647,044.44","774,692.00","348.07","Israel","NASDAQ","USD","1.00","USD","-"',
    ]
    snap = parse_csv(soxx_csv_text[:header_end] + header + "\n" + "\n".join(rows) + "\n", "SOXX")
    nvmi, nice = snap.holdings
    assert nvmi.is_adr is False and nice.is_adr is True  # the name wins over the location
    assert snap.meta["adr_overrides"] == ["NVMI"]
    assert snap.meta["adr_heuristic"] == []


def test_csv_and_json_routes_agree_for_the_same_date(soxx_json_bytes: bytes) -> None:
    raw = RAW_DIR / "SOXX" / "2026-10-07.csv"
    if not raw.is_file():
        pytest.skip("data/raw/SOXX/2026-10-07.csv is not present in this checkout")
    from_csv = parse_csv(raw.read_bytes(), "SOXX")
    from_json = parse_product_data_json(soxx_json_bytes, "SOXX")
    assert from_csv.as_of == from_json.as_of == date(2026, 10, 7)
    assert from_csv.tickers() == from_json.tickers()
    json_by = from_json.by_ticker()
    for ticker, c in from_csv.by_ticker().items():
        j = json_by[ticker]
        assert (c.name, c.asset_class, c.is_adr, c.sector, c.currency) == (j.name, j.asset_class, j.is_adr, j.sector, j.currency), ticker
        assert c.shares == pytest.approx(j.shares), ticker
        assert c.market_value == pytest.approx(j.market_value), ticker
        assert c.price == pytest.approx(j.price, rel=1e-6), ticker
        # the CSV rounds weights to two decimals of a percent
        assert c.weight == pytest.approx(j.weight, abs=0.000051), ticker
    assert from_csv.meta["derivative_notional"] == pytest.approx(from_json.meta["derivative_notional"])
    assert from_csv.meta["adr_heuristic"] == from_json.meta["adr_heuristic"]
    assert from_csv.meta["adr_overrides"] == from_json.meta["adr_overrides"]


def test_parse_csv_footer_without_blank_line_is_not_a_holding(soxx_csv_text: str) -> None:
    text = soxx_csv_text.rstrip("\n") + "\n" + FOOTER_NO_BLANK
    snap = parse_csv(text, "SOXX")
    assert len(snap.holdings) == 34
    assert snap.holdings[-1].ticker == "IXTZ6"
    assert not any("content contained" in t for t in snap.tickers())
    assert any("footer" in w for w in snap.meta["parse_warnings"])
    assert validate_snapshot(snap, "SOXX") == []
    # a header followed directly by the footer is the empty template
    header_end = soxx_csv_text.index("Ticker,Name")
    header_line = soxx_csv_text[header_end:].splitlines()[0]
    only_footer = soxx_csv_text[:header_end] + header_line + "\n" + FOOTER_NO_BLANK
    assert detect_empty_template(only_footer) is True
    with pytest.raises(EmptyTemplateError):
        parse_csv(only_footer, "SOXX")


def test_parse_csv_rows_without_as_of_date_is_a_parse_error(soxx_csv_text: str, soxx_json_bytes: bytes) -> None:
    text = soxx_csv_text.replace('Fund Holdings as of,"Oct 08, 2026"', 'Fund Holdings as of,"-"', 1)
    assert 'as of,"-"' in text
    with pytest.raises(ParseError) as info:
        parse_csv(text, "SOXX")
    assert not isinstance(info.value, EmptyTemplateError)
    assert detect_empty_template(text) is False
    # fetch must fall back to the JSON twin instead of reporting no_data
    http = FakeHTTP(csv=_csv_ok(text.encode()), json=_json_ok(soxx_json_bytes))
    result = fetch("SOXX", http_get=http)
    assert result.ok and result.source == SOURCE_JSON
    assert http.kinds == ["csv", "json"]


def test_validate_snapshot_rejects_implausible_as_of(soxx_csv_text: str) -> None:
    assert EARLIEST_AS_OF == date(2001, 7, 10) and MAX_FUTURE_DAYS == 7
    future = parse_csv(soxx_csv_text.replace("Oct 08, 2026", "Oct 08, 2126", 1), "SOXX")
    assert future.as_of == date(2126, 10, 8)
    problems = validate_snapshot(future, "SOXX", today=TODAY)
    assert len(problems) == 1 and "2126-10-08" in problems[0]
    ancient = parse_csv(soxx_csv_text.replace("Oct 08, 2026", "Oct 08, 1999", 1), "SOXX")
    problems = validate_snapshot(ancient, "SOXX", today=TODAY)
    assert len(problems) == 1 and "1999-10-08" in problems[0] and "2001-07-10" in problems[0]
    # dated today, or a few days ahead of a lagging clock, is fine
    snap = parse_csv(soxx_csv_text, "SOXX")
    assert validate_snapshot(snap, "SOXX", today=date(2026, 10, 8)) == []
    assert validate_snapshot(snap, "SOXX", today=date(2026, 10, 1)) == []
    assert validate_snapshot(snap, "SOXX", today=date(2026, 9, 30)) != []
    # the default clock is today's date
    assert validate_snapshot(snap, "SOXX") == []


def test_fetch_rejects_future_dated_file(soxx_csv_text: str) -> None:
    http = FakeHTTP(csv=_csv_ok(soxx_csv_text.replace("Oct 08, 2026", "Oct 08, 2126", 1).encode()))
    result = fetch("SOXX", http_get=http)
    assert result.status == STATUS_ERROR and result.snapshot is None
    assert "2126-10-08" in result.message
    assert http.kinds == ["csv"]


def test_deeply_nested_json_is_a_parse_error_not_a_crash() -> None:
    body = b"[" * 200_000
    with pytest.raises(ParseError, match="not JSON"):
        parse_product_data_json(body, "SOXX")
    http = FakeHTTP(csv=lambda url: _csv_response(url, b"", 503), json=_json_ok(body))
    result = fetch("SOXX", http_get=http)
    assert result.status == STATUS_ERROR and "parse failed" in result.message


# ---------------------------------------------------------------------------
# Live (opt-in)
# ---------------------------------------------------------------------------

NETWORK = os.environ.get("ETF_TRACKER_NETWORK_TESTS") == "1"


@pytest.mark.skipif(not NETWORK, reason="set ETF_TRACKER_NETWORK_TESTS=1 to run live network tests")
def test_live_fetch_soxx_latest() -> None:
    result = fetch("SOXX")
    assert result.ok, result.message
    snap = result.snapshot
    assert snap is not None
    assert len(snap.equities()) >= 30, result.message
    assert snap.as_of <= date.today()
    assert ".csv" in result.raw_files or ".json" in result.raw_files
    assert validate_snapshot(snap, "SOXX") == []
    print(f"live SOXX holdings as of {snap.as_of.isoformat()} via {snap.meta.get('route')}: {result.message}")
