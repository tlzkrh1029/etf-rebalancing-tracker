"""Tests for etf_tracker.sources.invesco (QQQ holdings via dng-api).

Offline tests use the real documents captured on 2026-10-09
(``tests/fixtures/QQQ_holdings_2026-10-08.json`` and
``QQQ_fundDetails_2026-10-08.json``) and a fake ``http_get``.  The live test
runs only with ``ETF_TRACKER_NETWORK_TESTS=1``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest

from etf_tracker import http as http_mod
from etf_tracker.bridge import apply_caps_to_snapshot, constituents_from_snapshot
from etf_tracker.holdings import CASH, DERIVATIVE, EQUITY, OTHER, load_snapshot, save_snapshot, snapshot_path
from etf_tracker.sources import STATUS_ERROR, STATUS_NO_DATA, STATUS_OK, STATUS_UNSUPPORTED
from etf_tracker.sources import invesco, ishares

FIXTURES = Path(__file__).parent / "fixtures"
HOLDINGS_BYTES = (FIXTURES / "QQQ_holdings_2026-10-08.json").read_bytes()
FUND_DETAILS_BYTES = (FIXTURES / "QQQ_fundDetails_2026-10-08.json").read_bytes()

AS_OF = date(2026, 10, 8)
TODAY = date(2026, 10, 9)  # capture date of the fixtures; keeps the future-date check stable
TNA = 500_688_014_927.97
NVDA_UNITS = 181_071_668
NVDA_PCT = 8.334285
NOW = 1_791_000_000.0  # fixed epoch for the cache-buster


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class FakeHttp:
    """Records calls and answers per URL substring and User-Agent.

    ``responder(url, user_agent) -> Response`` decides the answer.
    """

    def __init__(self, responder: Callable[[str, str | None], http_mod.Response]):
        self.responder = responder
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, url: str, *, user_agent: str | None = http_mod.DEFAULT_USER_AGENT, **kwargs) -> http_mod.Response:
        self.calls.append((url, user_agent))
        return self.responder(url, user_agent)


def _resp(url: str, status: int = 200, body: bytes = b"", content_type: str = "text/plain;charset=UTF-8", error: str | None = None) -> http_mod.Response:
    return http_mod.Response(url=url, status=status, headers={"content-type": content_type}, body=body, error=error)


def _is_holdings(url: str) -> bool:
    return "/holdings/fund?" in url


def _is_fund_details(url: str) -> bool:
    return "variationType=fundDetails" in url


def happy_responder(url: str, user_agent: str | None) -> http_mod.Response:
    if _is_holdings(url):
        return _resp(url, 200, HOLDINGS_BYTES)
    if _is_fund_details(url):
        return _resp(url, 200, FUND_DETAILS_BYTES)
    return _resp(url, 404, b"")


def parse_fixture(**kwargs):
    return invesco.parse_holdings_json(HOLDINGS_BYTES, FUND_DETAILS_BYTES, "QQQ", today=TODAY, **kwargs)


# ---------------------------------------------------------------------------
# Module contract and URL builders
# ---------------------------------------------------------------------------


def test_module_contract():
    assert invesco.ETFS == ("QQQ",)
    assert callable(invesco.fetch)
    assert invesco.SOURCE
    # UA policy: project UA first, then urllib default; never a browser UA.
    assert invesco.USER_AGENTS[0] == http_mod.DEFAULT_USER_AGENT
    assert invesco.USER_AGENTS[-1] is None
    assert all(ua is None or not ua.startswith("Mozilla/") for ua in invesco.USER_AGENTS)
    assert 406 in invesco.UA_FALLBACK_STATUSES


def test_url_builders_include_cache_buster():
    h = invesco.holdings_url("QQQ", now=NOW)
    d = invesco.fund_details_url("qqq", now=NOW)
    assert h.startswith("https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund?")
    assert "idType=ticker" in h and "productType=ETF" in h and h.endswith(f"&cb={int(NOW)}")
    assert d.startswith("https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ?")
    assert "variationType=fundDetails" in d and d.endswith(f"&cb={int(NOW)}")
    # default: current time, still an integer cache-buster
    assert invesco.holdings_url().rsplit("cb=", 1)[1].isdigit()
    with pytest.raises(ValueError):
        invesco.holdings_url("QQQ", now=-1)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, code, expected",
    [
        ("Common Stock", "COM", (EQUITY, False)),
        ("American Depository Receipt", "ADR", (EQUITY, True)),
        ("American Depository Receipt - NY", "DRNY", (EQUITY, True)),
        ("ADR", None, (EQUITY, True)),
        ("Index Future", "IFUT", (DERIVATIVE, False)),
        ("Currency", "CURR", (CASH, False)),
        ("Currency Collateral", "CURRCOL", (CASH, False)),
        ("Synthetic Cash", "SYN", (CASH, False)),
        (None, "COM", (EQUITY, False)),  # name missing, code decides
        ("Something New", "ZZZ", (OTHER, False)),
        ("", "", (OTHER, False)),
    ],
)
def test_classify_security(name, code, expected):
    assert invesco.classify_security(name, code) == expected


def test_synthesize_ticker_is_deterministic_and_prefixed():
    assert invesco.synthesize_ticker("CASH COLLATERAL") == "_CASH_COLLATERAL"
    assert invesco.synthesize_ticker("CONTRA FUTURE FUTURE DEC 18 26") == "_CONTRA_FUTURE_FUTURE_DEC_18_26"
    assert invesco.synthesize_ticker("  cash &amp; equivalents ") == "_CASH___EQUIVALENTS"
    assert invesco.synthesize_ticker("") == "_UNNAMED"
    assert invesco.synthesize_ticker(None, fallback="row_7") == "_ROW_7"
    assert invesco.synthesize_ticker("ABC") == invesco.synthesize_ticker("ABC")


# ---------------------------------------------------------------------------
# parse_holdings_json on the real fixture
# ---------------------------------------------------------------------------


def test_fixture_parses_to_expected_shape():
    snap = parse_fixture()
    assert snap.etf == "QQQ"
    assert snap.as_of == AS_OF
    assert snap.source == invesco.SOURCE
    assert len(snap.holdings) == 105
    assert len(snap.equities()) == 100
    counts = {}
    for h in snap.holdings:
        counts[h.asset_class] = counts.get(h.asset_class, 0) + 1
    assert counts == {EQUITY: 100, CASH: 3, DERIVATIVE: 1, OTHER: 1}
    assert all(h.etf == "QQQ" and h.as_of == AS_OF and h.source == invesco.SOURCE for h in snap.holdings)
    assert all(h.company_id is None for h in snap.holdings)
    assert all(h.ticker and h.ticker.strip() for h in snap.holdings)
    assert len(set(snap.tickers())) == 105


def test_fixture_adr_flags():
    snap = parse_fixture()
    adrs = sorted(h.ticker for h in snap.equities() if h.is_adr)
    assert adrs == ["ARM", "ASML", "PDD"]
    commons = [h for h in snap.equities() if not h.is_adr]
    assert len(commons) == 97
    assert all(h.is_adr is False for h in commons)
    assert all(h.is_adr is None for h in snap.non_equities())
    assert snap.meta["adr_tickers"] == ["ASML", "ARM", "PDD"]  # file order


def test_fixture_nvda_values_derived_from_fund_details():
    snap = parse_fixture()
    nvda = snap.by_ticker()["NVDA"]
    assert nvda.name == "NVIDIA Corp"
    assert nvda.asset_class == EQUITY
    assert nvda.weight == pytest.approx(NVDA_PCT / 100)
    assert nvda.weight == pytest.approx(0.08334285)
    assert nvda.shares == NVDA_UNITS
    expected_mv = 0.08334285 * TNA
    assert nvda.market_value == pytest.approx(expected_mv, rel=1e-4)
    assert nvda.price == pytest.approx(expected_mv / NVDA_UNITS, rel=1e-4)
    assert nvda.currency == "USD"
    assert nvda.sector is None


def test_fixture_non_equity_rows():
    snap = parse_fixture()
    by = snap.by_ticker()
    usd = by["USD"]
    assert usd.asset_class == CASH
    assert usd.name == "CASH & EQUIVALENTS"  # HTML entity unescaped
    assert usd.shares == pytest.approx(2_364_275_681.36)
    assert usd.weight == pytest.approx(0.00472153)
    assert usd.market_value == pytest.approx(0.00472153 * TNA, rel=1e-6)
    assert usd.price is None  # prices derived for equities only

    fut = by["NQZ6"]
    assert fut.asset_class == DERIVATIVE
    assert fut.shares == 1125
    assert fut.weight == pytest.approx(0.00139156)
    assert fut.price is None

    pending = by["USDPDV"]
    assert pending.asset_class == OTHER
    assert pending.weight == 0.0
    assert pending.shares == 0
    assert pending.market_value is None
    assert pending.price is None
    assert snap.meta["null_weight_tickers"] == ["USDPDV"]


def test_fixture_synthesized_tickers_for_empty_ticker_rows():
    snap = parse_fixture()
    assert snap.meta["synthesized_tickers"] == ["_CASH_COLLATERAL", "_CONTRA_FUTURE_FUTURE_DEC_18_26"]
    by = snap.by_ticker()
    collateral = by["_CASH_COLLATERAL"]
    assert collateral.asset_class == CASH
    assert collateral.name == "CASH COLLATERAL"
    assert collateral.shares == pytest.approx(7_647_129.21)
    contra = by["_CONTRA_FUTURE_FUTURE_DEC_18_26"]
    assert contra.asset_class == CASH
    assert contra.shares == -1125
    assert contra.weight == pytest.approx(-0.00139156)
    assert contra.market_value == pytest.approx(-0.00139156 * TNA, rel=1e-6)
    # Parsing twice gives identical tickers.
    assert parse_fixture().meta["synthesized_tickers"] == snap.meta["synthesized_tickers"]


def test_fixture_meta():
    snap = parse_fixture()
    meta = snap.meta
    assert meta["total_number_of_holdings"] == 105
    assert meta["row_count"] == 105
    assert meta["equity_count"] == 100
    assert meta["weight_sum_pct"] == pytest.approx(99.999998, abs=1e-6)
    assert meta["nav"] == pytest.approx(747.407098)
    assert meta["shares_outstanding"] == 669_900_000
    assert meta["total_net_assets"] == pytest.approx(TNA)
    assert meta["total_net_assets_basis"] == "shareclassTotalNetAssets"
    assert meta["fund_details_effective_date"] == "2026-10-08"
    assert meta["price_derivation"] == "weight*TNA/units"
    assert meta["effective_date"] == "2026-10-08"
    assert meta["effective_business_date"] == "2026-10-08"
    assert meta["warnings"] == []
    assert meta["security_type_counts"]["Common Stock"] == 97
    assert set(meta["source_urls"]) == {"holdings", "fund_details"}
    assert meta["url"] == meta["source_urls"]["holdings"]
    assert "dng-api.invesco.com" in meta["url"]


def test_fixture_weights_are_fractions_and_consistent():
    snap = parse_fixture()
    total = sum(h.weight for h in snap.holdings)
    assert total == pytest.approx(1.0, abs=1e-6)
    # Core validation: the only notes are the derived prices of non-equity rows.
    warnings = snap.validate()
    assert all("derived price" in w for w in warnings), warnings
    assert len(warnings) == 4
    # Market-value based normalisation agrees with the published weights.
    norm = snap.normalized_weights(include_cash=False)
    assert norm["NVDA"] == pytest.approx(0.08334285 / sum(h.weight for h in snap.equities()), rel=1e-6)


def test_fixture_round_trips_through_json(tmp_path):
    snap = parse_fixture()
    path = snapshot_path(tmp_path, "QQQ", snap.as_of)
    save_snapshot(snap, path)
    loaded = load_snapshot(path)
    assert loaded.as_of == AS_OF
    assert len(loaded.holdings) == 105
    assert loaded.meta["synthesized_tickers"] == snap.meta["synthesized_tickers"]
    assert loaded.by_ticker()["ASML"].is_adr is True
    assert loaded.by_ticker()["NVDA"].price == pytest.approx(snap.by_ticker()["NVDA"].price)


def test_fixture_feeds_the_capping_rules():
    snap = parse_fixture()
    constituents = constituents_from_snapshot(snap, "QQQ")
    assert len(constituents) == 100
    assert sum(c.weight for c in constituents) == pytest.approx(1.0)
    by = {c.ticker: c for c in constituents}
    assert by["GOOGL"].company_id == "ALPHABET" and by["GOOG"].company_id == "ALPHABET"
    assert by["ASML"].is_adr and by["ARM"].is_adr and by["PDD"].is_adr
    assert not by["NVDA"].is_adr
    result = apply_caps_to_snapshot(snap, "QQQ")
    assert result.feasible


# ---------------------------------------------------------------------------
# parse_holdings_json: missing / degraded fundDetails
# ---------------------------------------------------------------------------


def test_parse_without_fund_details_leaves_prices_empty_with_warning():
    snap = invesco.parse_holdings_json(HOLDINGS_BYTES, None, "QQQ", today=TODAY)
    assert len(snap.holdings) == 105
    assert all(h.market_value is None and h.price is None for h in snap.holdings)
    assert snap.by_ticker()["NVDA"].weight == pytest.approx(0.08334285)
    assert snap.by_ticker()["NVDA"].shares == NVDA_UNITS
    assert snap.meta["price_derivation"] is None
    assert snap.meta["nav"] is None and snap.meta["total_net_assets"] is None
    assert any("fundDetails not available" in w for w in snap.meta["warnings"])
    # Weight-based normalisation still works for the rules.
    assert snap.weight_basis(include_cash=False) == "weight"
    assert len(constituents_from_snapshot(snap, "QQQ")) == 100


def test_parse_with_unparseable_fund_details_is_non_fatal():
    snap = invesco.parse_holdings_json(HOLDINGS_BYTES, b"<html>blocked</html>", "QQQ", today=TODAY)
    assert all(h.price is None for h in snap.holdings)
    assert any("fundDetails unparseable" in w for w in snap.meta["warnings"])


def test_parse_falls_back_to_nav_times_shares_outstanding():
    details = json.loads(FUND_DETAILS_BYTES)
    del details["shareclassTotalNetAssets"]
    snap = invesco.parse_holdings_json(HOLDINGS_BYTES, json.dumps(details).encode(), "QQQ", today=TODAY)
    expected_tna = 747.407098 * 669_900_000
    assert snap.meta["total_net_assets"] == pytest.approx(expected_tna)
    assert snap.meta["total_net_assets_basis"] == "nav*sharesOutstanding"
    assert snap.meta["price_derivation"] == "weight*TNA/units"
    assert snap.by_ticker()["NVDA"].market_value == pytest.approx(0.08334285 * expected_tna, rel=1e-9)
    assert any("nav * sharesOutstanding" in w for w in snap.meta["warnings"])


def test_parse_without_any_fund_total_leaves_prices_empty():
    details = {"effectiveDate": "2026-10-08", "nav": None, "sharesOutstanding": "n/a"}
    snap = invesco.parse_holdings_json(HOLDINGS_BYTES, json.dumps(details).encode(), "QQQ", today=TODAY)
    assert all(h.price is None and h.market_value is None for h in snap.holdings)
    assert snap.meta["price_derivation"] is None
    assert any("neither shareclassTotalNetAssets" in w for w in snap.meta["warnings"])


def test_parse_warns_when_fund_details_date_differs():
    details = json.loads(FUND_DETAILS_BYTES)
    details["shareclassTotalNetAssetsEffectiveDate"] = "2026-10-07"
    snap = invesco.parse_holdings_json(HOLDINGS_BYTES, json.dumps(details).encode(), "QQQ", today=TODAY)
    assert snap.meta["fund_details_effective_date"] == "2026-10-07"
    assert any("2026-10-07" in w and "2026-10-08" in w for w in snap.meta["warnings"])
    # Prices are still derived (the total is the best available).
    assert snap.by_ticker()["NVDA"].price is not None


def test_as_of_is_the_effective_business_date_when_the_file_is_dated_ahead_of_it():
    # effectiveDate is a calendar date (a Saturday here) while the positions and
    # the fund total are priced at the last session (observed Saturday
    # 2026-10-10 11:08 UTC: effectiveDate 2026-10-10, effectiveBusinessDate
    # 2026-10-09, shareclassTotalNetAssetsEffectiveDate 2026-10-09).
    details = json.loads(FUND_DETAILS_BYTES)
    details["effectiveDate"] = "2026-10-10"
    details["shareclassTotalNetAssetsEffectiveDate"] = "2026-10-09"
    body = _holdings_doc(effectiveDate="2026-10-10", effectiveBusinessDate="2026-10-09")
    snap = invesco.parse_holdings_json(body, json.dumps(details).encode(), "QQQ", today=date(2026, 10, 10))
    assert snap.as_of == date(2026, 10, 9)
    assert snap.meta["effective_date"] == "2026-10-10"
    assert snap.meta["effective_business_date"] == "2026-10-09"
    assert snap.meta["as_of_basis"] == "effectiveBusinessDate"
    assert snap.meta["fund_details_effective_date"] == "2026-10-09"
    # the pricing dates agree, so no date-mismatch warning
    assert not any("fundDetails total net assets are as of" in w for w in snap.meta["warnings"])
    assert snap.by_ticker()["NVDA"].price is not None


def test_as_of_is_the_effective_date_when_both_dates_agree():
    snap = parse_fixture()
    assert snap.as_of == date(2026, 10, 8)
    assert snap.meta["effective_date"] == snap.meta["effective_business_date"] == "2026-10-08"
    assert snap.meta["as_of_basis"] == "effectiveBusinessDate"


def test_as_of_without_an_effective_business_date_is_the_effective_date():
    doc = json.loads(HOLDINGS_BYTES)
    doc.pop("effectiveBusinessDate", None)
    snap = invesco.parse_holdings_json(json.dumps(doc).encode(), FUND_DETAILS_BYTES, "QQQ", today=TODAY)
    assert snap.as_of == date(2026, 10, 8)
    assert snap.meta["effective_business_date"] is None and snap.meta["as_of_basis"] == "effectiveDate"
    assert not any("effectiveBusinessDate" in w for w in snap.meta["warnings"])


def test_a_stale_effective_business_date_is_used_but_flagged():
    body = _holdings_doc(effectiveDate="2026-10-12", effectiveBusinessDate="2026-10-01")  # 11 days: no holiday weekend is that long
    snap = invesco.parse_holdings_json(body, FUND_DETAILS_BYTES, "QQQ", today=date(2026, 10, 12))
    assert snap.as_of == date(2026, 10, 1) and snap.meta["as_of_basis"] == "effectiveBusinessDate"
    assert any("11 days before effectiveDate" in w and "may be stale" in w for w in snap.meta["warnings"])
    # a holiday weekend (Friday close published on Tuesday) is within the bound
    body = _holdings_doc(effectiveDate="2026-10-13", effectiveBusinessDate="2026-10-09")
    snap = invesco.parse_holdings_json(body, FUND_DETAILS_BYTES, "QQQ", today=date(2026, 10, 13))
    assert not any("may be stale" in w for w in snap.meta["warnings"])


def test_fund_details_business_date_backs_up_the_total_net_assets_date():
    # shareclassTotalNetAssetsEffectiveDate missing: the fundDetails business date,
    # not its calendar effectiveDate, is compared with the holdings date
    details = json.loads(FUND_DETAILS_BYTES)
    details["effectiveDate"] = "2026-10-10"
    details["effectiveBusinessDate"] = "2026-10-09"
    details.pop("shareclassTotalNetAssetsEffectiveDate", None)
    body = _holdings_doc(effectiveDate="2026-10-10", effectiveBusinessDate="2026-10-09")
    snap = invesco.parse_holdings_json(body, json.dumps(details).encode(), "QQQ", today=date(2026, 10, 10))
    assert snap.meta["fund_details_effective_date"] == "2026-10-09"
    assert not any("fundDetails total net assets are as of" in w for w in snap.meta["warnings"])
    assert invesco.parse_fund_details_json(json.dumps(details).encode())["effective_business_date"] == date(2026, 10, 9)


@pytest.mark.parametrize(
    "business, fragment",
    [
        ("2026-10-11", "is after effectiveDate"),
        ("10/09/2026", "is not 'YYYY-MM-DD'"),
    ],
)
def test_as_of_falls_back_to_the_effective_date_with_a_warning(business, fragment):
    body = _holdings_doc(effectiveDate="2026-10-10", effectiveBusinessDate=business)
    snap = invesco.parse_holdings_json(body, FUND_DETAILS_BYTES, "QQQ", today=date(2026, 10, 10))
    assert snap.as_of == date(2026, 10, 10)
    assert snap.meta["as_of_basis"] == "effectiveDate"
    assert snap.meta["effective_business_date"] == business
    assert any(fragment in w and "falls back to effectiveDate" in w for w in snap.meta["warnings"])


# ---------------------------------------------------------------------------
# parse_holdings_json: defensive handling of bad input
# ---------------------------------------------------------------------------


def _holdings_doc(**overrides) -> bytes:
    doc = json.loads(HOLDINGS_BYTES)
    doc.update(overrides)
    return json.dumps(doc).encode()


@pytest.mark.parametrize(
    "body, fragment",
    [
        (b"", "empty body"),
        (b"   \n", "empty body"),
        (b"<html><body>Access denied</body></html>", "not valid JSON"),
        (b"[1, 2, 3]", "expected an object"),
        (b'{"holdings": []}', "effectiveDate"),
        (_holdings_doc(effectiveDate="08/10/2026"), "effectiveDate"),
        (_holdings_doc(effectiveDate="1998-01-01"), "before QQQ inception"),
        (_holdings_doc(effectiveDate="2026-12-31"), "in the future"),
        (_holdings_doc(holdings=None), "not a list"),
        (_holdings_doc(holdings={"ticker": "NVDA"}), "not a list"),
        (_holdings_doc(holdings=[]), "empty"),
        (_holdings_doc(holdings=[1, 2, "x"]), "no row is a JSON object"),
    ],
)
def test_parse_rejects_malformed_documents(body, fragment):
    with pytest.raises(ValueError, match=fragment):
        invesco.parse_holdings_json(body, FUND_DETAILS_BYTES, "QQQ", today=TODAY)


def test_parse_rejects_oversized_bodies_before_decoding():
    body = b"{" + b" " * (invesco.MAX_BODY_BYTES + 1) + b"}"
    with pytest.raises(ValueError, match="exceeds"):
        invesco.parse_holdings_json(body, None, "QQQ", today=TODAY)
    too_many = _holdings_doc(holdings=[{"ticker": "X", "percentageOfTotalNetAssets": 0}] * (invesco.MAX_ROWS + 1))
    with pytest.raises(ValueError, match="row bound"):
        invesco.parse_holdings_json(too_many, None, "QQQ", today=TODAY)


def test_parse_tolerates_bom_and_time_suffix_and_string_numbers():
    doc = json.loads(HOLDINGS_BYTES)
    doc["effectiveDate"] = "2026-10-08T00:00:00"
    doc["holdings"][0]["units"] = "181,071,668"
    doc["holdings"][0]["percentageOfTotalNetAssets"] = "8.334285"
    body = b"\xef\xbb\xbf" + json.dumps(doc).encode()
    snap = invesco.parse_holdings_json(body, FUND_DETAILS_BYTES, "QQQ", today=TODAY)
    assert snap.as_of == AS_OF
    nvda = snap.by_ticker()["NVDA"]
    assert nvda.shares == NVDA_UNITS and nvda.weight == pytest.approx(0.08334285)


def test_parse_skips_non_object_rows_and_dedups_synthetic_tickers():
    doc = json.loads(HOLDINGS_BYTES)
    doc["holdings"].insert(3, "garbage")
    doc["holdings"].append({"ticker": None, "issuerName": "CASH COLLATERAL", "units": 1, "percentageOfTotalNetAssets": 0.0, "securityTypeName": "Currency Collateral"})
    doc["holdings"].append({"ticker": "  ", "issuerName": "", "units": None, "percentageOfTotalNetAssets": None, "securityTypeName": None})
    doc["holdings"].append({"ticker": "NVDA", "issuerName": "dup", "units": 1, "percentageOfTotalNetAssets": 0.0, "securityTypeName": "Common Stock"})
    snap = invesco.parse_holdings_json(json.dumps(doc).encode(), FUND_DETAILS_BYTES, "QQQ", today=TODAY)
    assert len(snap.holdings) == 108
    assert snap.meta["synthesized_tickers"][:2] == ["_CASH_COLLATERAL", "_CONTRA_FUTURE_FUTURE_DEC_18_26"]
    assert "_CASH_COLLATERAL_2" in snap.meta["synthesized_tickers"]
    assert any(t.startswith("_ROW_") for t in snap.meta["synthesized_tickers"])
    warnings = snap.meta["warnings"]
    assert any("row 3" in w and "skipped" in w for w in warnings)
    assert any("duplicate ticker 'NVDA'" in w for w in warnings)
    assert any("totalNumberOfHoldings=105" in w for w in warnings)


def test_parse_warns_on_fund_identifier_mismatch():
    snap = invesco.parse_holdings_json(_holdings_doc(cusip="SPY"), FUND_DETAILS_BYTES, "QQQ", today=TODAY)
    assert any("'SPY'" in w and "expected QQQ" in w for w in snap.meta["warnings"])


def test_parse_fund_details_json_fields():
    details = invesco.parse_fund_details_json(FUND_DETAILS_BYTES)
    assert details["effective_date"] == AS_OF
    assert details["nav"] == pytest.approx(747.407098)
    assert details["shares_outstanding"] == 669_900_000
    assert details["total_net_assets"] == pytest.approx(TNA)
    assert details["total_net_assets_effective_date"] == AS_OF
    assert details["total_no_of_holdings"] == 101
    assert details["identifier"] == "QQQ"
    with pytest.raises(ValueError):
        invesco.parse_fund_details_json(b"not json")


# ---------------------------------------------------------------------------
# validate_snapshot
# ---------------------------------------------------------------------------


def test_validate_snapshot_thresholds():
    snap = parse_fixture()
    assert invesco.validate_snapshot(snap) == []
    few = parse_fixture()
    few.holdings = few.holdings[:50]
    problems = invesco.validate_snapshot(few)
    assert any("equity rows" in p for p in problems)
    skewed = parse_fixture()
    for h in skewed.holdings:
        h.weight = (h.weight or 0.0) * 0.5
    problems = invesco.validate_snapshot(skewed)
    assert any("weights sum to 50.0000%" in p for p in problems)


# ---------------------------------------------------------------------------
# fetch with a fake transport
# ---------------------------------------------------------------------------


def test_fetch_happy_path_uses_project_ua_and_returns_ok():
    fake = FakeHttp(happy_responder)
    result = invesco.fetch("QQQ", http_get=fake, now=NOW)
    assert result.status == STATUS_OK and result.ok
    assert result.etf == "QQQ"
    assert result.source == invesco.SOURCE
    assert result.requested_as_of is None
    assert result.raw_files == {".json": HOLDINGS_BYTES, ".fund.json": FUND_DETAILS_BYTES}
    assert "2026-10-08" in result.message and "105 rows" in result.message
    snap = result.snapshot
    assert snap.as_of == AS_OF and len(snap.holdings) == 105
    assert snap.by_ticker()["NVDA"].price == pytest.approx(0.08334285 * TNA / NVDA_UNITS, rel=1e-4)
    assert snap.meta["user_agent"] == "project"
    assert snap.meta["downloaded_at"].startswith("2026-")
    assert snap.meta["source_urls"]["holdings"].endswith(f"&cb={int(NOW)}")
    assert snap.meta["warnings"] == []
    # Exactly two requests, both with the project UA, in holdings-then-details order.
    assert [ua for _, ua in fake.calls] == [http_mod.DEFAULT_USER_AGENT, http_mod.DEFAULT_USER_AGENT]
    assert _is_holdings(fake.calls[0][0]) and _is_fund_details(fake.calls[1][0])


def test_fetch_retries_without_user_agent_on_406():
    def responder(url: str, user_agent: str | None) -> http_mod.Response:
        if user_agent is not None:
            return _resp(url, 406, b"", content_type="")
        return happy_responder(url, None)

    fake = FakeHttp(responder)
    result = invesco.fetch("QQQ", http_get=fake, now=NOW)
    assert result.ok, result.message
    assert result.snapshot.meta["user_agent"] == "urllib-default"
    uas = [ua for _, ua in fake.calls]
    # holdings: project UA -> 406 -> no UA; details: starts with the UA that worked.
    assert uas == [http_mod.DEFAULT_USER_AGENT, None, None]
    assert all(ua is None or not ua.startswith("Mozilla") for ua in uas)
    assert result.raw_files[".fund.json"] == FUND_DETAILS_BYTES


def test_fetch_gives_up_after_one_ua_fallback():
    fake = FakeHttp(lambda url, ua: _resp(url, 406, b"", content_type=""))
    result = invesco.fetch("QQQ", http_get=fake, now=NOW)
    assert result.status == STATUS_ERROR
    assert "HTTP 406" in result.message
    assert result.snapshot is None
    assert [ua for _, ua in fake.calls] == [http_mod.DEFAULT_USER_AGENT, None]


def test_fetch_never_sends_a_browser_user_agent(monkeypatch):
    monkeypatch.setattr(invesco, "USER_AGENTS", ("Mozilla/5.0 (X11; Linux) Gecko", None))
    fake = FakeHttp(happy_responder)
    with pytest.raises(ValueError, match="browser User-Agent"):
        invesco.fetch("QQQ", http_get=fake, now=NOW)
    assert fake.calls == []


def test_fetch_historical_as_of_is_unsupported():
    fake = FakeHttp(happy_responder)
    result = invesco.fetch("QQQ", as_of=date(2026, 10, 1), http_get=fake, now=NOW)
    assert result.status == STATUS_UNSUPPORTED
    assert not result.ok and result.snapshot is None
    assert result.requested_as_of == "2026-10-01"
    assert "only the current snapshot" in result.message and "2026-10-08" in result.message
    # Raw bytes are still handed back for the caller's records.
    assert ".json" in result.raw_files


def test_fetch_as_of_equal_to_served_date_is_ok():
    result = invesco.fetch("QQQ", as_of=AS_OF, http_get=FakeHttp(happy_responder), now=NOW)
    assert result.ok
    assert result.requested_as_of == "2026-10-08"
    assert result.snapshot.as_of == AS_OF
    # ISO strings are accepted too.
    assert invesco.fetch("qqq", as_of="2026-10-08", http_get=FakeHttp(happy_responder), now=NOW).ok


def test_fetch_future_as_of_is_no_data():
    result = invesco.fetch("QQQ", as_of=date(2026, 10, 9), http_get=FakeHttp(happy_responder), now=NOW)
    assert result.status == STATUS_NO_DATA
    assert result.snapshot is None
    assert "not yet published" in result.message


def test_fetch_missing_fund_details_is_non_fatal():
    def responder(url: str, user_agent: str | None) -> http_mod.Response:
        if _is_fund_details(url):
            return _resp(url, 500, b"", content_type="")
        return happy_responder(url, user_agent)

    fake = FakeHttp(responder)
    result = invesco.fetch("QQQ", http_get=fake, now=NOW)
    assert result.ok
    assert result.raw_files == {".json": HOLDINGS_BYTES}
    snap = result.snapshot
    assert all(h.price is None and h.market_value is None for h in snap.holdings)
    assert snap.by_ticker()["NVDA"].weight == pytest.approx(0.08334285)
    assert any("fundDetails download failed: HTTP 500" in w for w in snap.meta["warnings"])
    assert any("fundDetails not available" in w for w in snap.meta["warnings"])
    assert "prices missing" in result.message


def test_fetch_fund_details_html_body_is_treated_as_missing():
    def responder(url: str, user_agent: str | None) -> http_mod.Response:
        if _is_fund_details(url):
            return _resp(url, 200, b"<!DOCTYPE html><html>blocked</html>", content_type="text/html")
        return happy_responder(url, user_agent)

    result = invesco.fetch("QQQ", http_get=FakeHttp(responder), now=NOW)
    assert result.ok
    assert ".fund.json" not in result.raw_files
    assert any("body is HTML" in w for w in result.snapshot.meta["warnings"])


@pytest.mark.parametrize(
    "response, fragment",
    [
        (lambda url: _resp(url, 0, b"", content_type="", error="URLError: timed out"), "transport error"),
        (lambda url: _resp(url, 403, b"denied", content_type="text/html"), "HTTP 403"),
        (lambda url: _resp(url, 200, b"", content_type=""), "empty body"),
        (lambda url: _resp(url, 200, b"<html><body>blocked</body></html>", content_type="text/html"), "body is HTML"),
        (lambda url: _resp(url, 200, b"{not json"), "holdings document rejected"),
        (lambda url: _resp(url, 200, b'{"effectiveDate": "2026-10-08", "holdings": []}'), "holdings document rejected"),
    ],
)
def test_fetch_holdings_failures_are_errors(response, fragment):
    def responder(url: str, user_agent: str | None) -> http_mod.Response:
        if _is_holdings(url):
            return response(url)
        return happy_responder(url, user_agent)

    fake = FakeHttp(responder)
    result = invesco.fetch("QQQ", http_get=fake, now=NOW)
    assert result.status == STATUS_ERROR
    assert result.snapshot is None
    assert fragment in result.message
    assert result.source == invesco.SOURCE


def test_fetch_rejects_implausible_snapshots():
    doc = json.loads(HOLDINGS_BYTES)
    doc["holdings"] = doc["holdings"][:60]
    short = json.dumps(doc).encode()

    def responder(url: str, user_agent: str | None) -> http_mod.Response:
        if _is_holdings(url):
            return _resp(url, 200, short)
        return happy_responder(url, user_agent)

    result = invesco.fetch("QQQ", http_get=FakeHttp(responder), now=NOW)
    assert result.status == STATUS_ERROR
    assert "equity rows" in result.message
    assert result.raw_files[".json"] == short


def test_fetch_unknown_etf_is_unsupported():
    fake = FakeHttp(happy_responder)
    result = invesco.fetch("SOXX", http_get=fake)
    assert result.status == STATUS_UNSUPPORTED
    assert "QQQ" in result.message
    assert fake.calls == []


def test_fetch_rejects_bad_as_of_argument():
    with pytest.raises(ValueError):
        invesco.fetch("QQQ", as_of="yesterday", http_get=FakeHttp(happy_responder))
    with pytest.raises(TypeError):
        invesco.fetch("QQQ", as_of=20261008, http_get=FakeHttp(happy_responder))  # type: ignore[arg-type]


def test_fetch_default_transport_is_project_http_get(monkeypatch):
    seen: list[tuple[str, str | None]] = []

    def fake_get(url: str, *, user_agent=http_mod.DEFAULT_USER_AGENT, **kwargs) -> http_mod.Response:
        seen.append((url, user_agent))
        return happy_responder(url, user_agent)

    monkeypatch.setattr(http_mod, "get", fake_get)
    result = invesco.fetch("QQQ", now=NOW)
    assert result.ok
    assert len(seen) == 2 and all(ua == http_mod.DEFAULT_USER_AGENT for _, ua in seen)


# ---------------------------------------------------------------------------
# Adversarial review (2026-10-09): real files, cross-source checks, hostile input
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_QQQ_DIR = REPO_ROOT / "data" / "raw" / "QQQ"


def test_derived_prices_agree_with_ishares_prices_of_the_same_day():
    """QQQ has no prices; ``weight * TNA / units`` must reproduce the closing
    prices BlackRock prints for the same tickers on the same date."""
    soxx = ishares.parse_csv((FIXTURES / "SOXX_holdings_2026-10-08.csv").read_bytes(), "SOXX")
    igv = ishares.parse_csv((FIXTURES / "IGV_holdings_2026-10-08.csv").read_bytes(), "IGV")
    qqq = parse_fixture().by_ticker()
    assert soxx.as_of == igv.as_of == AS_OF
    compared = 0
    for snap in (soxx, igv):
        for h in snap.equities():
            q = qqq.get(h.ticker)
            if q is None:
                continue
            assert q.asset_class == EQUITY and q.price is not None, h.ticker
            assert q.price == pytest.approx(h.price, rel=0.005), f"{h.ticker}: QQQ {q.price:.2f} vs {snap.etf} {h.price:.2f}"
            compared += 1
    assert compared >= 30  # NVDA, AVGO, AMD, MSFT, ADBE, PANW, ...


def test_raw_qqq_files_parse_like_the_fixture():
    files = sorted(RAW_QQQ_DIR.glob("????-??-??.json")) if RAW_QQQ_DIR.is_dir() else []
    if not files:
        pytest.skip("data/raw/QQQ is not present in this checkout")
    for path in files:
        fund = path.with_name(f"{path.stem}.fund.json")
        snap = invesco.parse_holdings_json(path.read_bytes(), fund.read_bytes() if fund.is_file() else None, "QQQ", today=date.fromisoformat(path.stem))
        assert snap.as_of == date.fromisoformat(path.stem), path.name
        assert invesco.validate_snapshot(snap) == [], path.name
        assert [w for w in snap.meta["warnings"] if "fundDetails" not in w] == [], path.name
        assert len(set(snap.tickers())) == len(snap.holdings), path.name
        assert len(snap.equities()) >= invesco.MIN_EQUITY_ROWS
        by = snap.by_ticker()
        assert "GOOG" in by and "GOOGL" in by  # both Alphabet share classes, distinct tickers
        assert all(t.startswith("_") for t in snap.meta["synthesized_tickers"])
        assert all(by[t].asset_class != EQUITY for t in snap.meta["synthesized_tickers"])
        adrs = {h.ticker for h in snap.equities() if h.is_adr}
        # the flag follows the published security type, so derive the expectation from the document
        doc_rows = json.loads(path.read_bytes())["holdings"]
        depositary = {r["ticker"] for r in doc_rows if r.get("ticker") and "depositary" in str(r.get("securityTypeName", "")).lower().replace("depository", "depositary")}
        assert adrs == depositary, path.name
        assert {"ASML", "ARM", "PDD"} & set(by) <= adrs, path.name  # the known ADRs are among them
        if fund.is_file():
            assert snap.meta["price_derivation"] == invesco.PRICE_DERIVATION
            for h in snap.equities():
                assert h.market_value == pytest.approx(h.shares * h.price, rel=1e-9), h.ticker


def test_synthesized_tickers_do_not_depend_on_row_order():
    doc = json.loads(HOLDINGS_BYTES)
    doc["holdings"].reverse()
    snap = invesco.parse_holdings_json(json.dumps(doc).encode(), FUND_DETAILS_BYTES, "QQQ", today=TODAY)
    assert set(snap.meta["synthesized_tickers"]) == {"_CASH_COLLATERAL", "_CONTRA_FUTURE_FUTURE_DEC_18_26"}
    assert snap.by_ticker()["_CASH_COLLATERAL"].shares == pytest.approx(7_647_129.21)
    assert snap.by_ticker()["_CONTRA_FUTURE_FUTURE_DEC_18_26"].weight == pytest.approx(-0.00139156)
    assert snap.meta["warnings"] == []


def test_equity_row_with_null_weight_stays_an_equity_and_warns():
    doc = json.loads(HOLDINGS_BYTES)
    assert doc["holdings"][0]["ticker"] == "NVDA"
    doc["holdings"][0]["percentageOfTotalNetAssets"] = None
    snap = invesco.parse_holdings_json(json.dumps(doc).encode(), FUND_DETAILS_BYTES, "QQQ", today=TODAY)
    nvda = snap.by_ticker()["NVDA"]
    assert nvda.asset_class == EQUITY and nvda.is_adr is False
    assert nvda.weight is None and nvda.market_value is None and nvda.price is None
    assert nvda.shares == NVDA_UNITS
    assert len(snap.equities()) == 100
    assert snap.meta["null_weight_tickers"] == ["NVDA", "USDPDV"]
    assert any("NVDA" in w and "weight" in w for w in snap.meta["warnings"])
    # the pending-dividend cash line keeps its documented treatment
    pending = snap.by_ticker()["USDPDV"]
    assert pending.asset_class == OTHER and pending.weight == 0.0
    assert not any("USDPDV" in w for w in snap.meta["warnings"])
    # the missing 8.3% shows up in the weight sum, so fetch reports it
    assert any("weights sum to 91." in p for p in invesco.validate_snapshot(snap))


def test_parse_accepts_a_percent_sign_in_the_weight():
    doc = json.loads(HOLDINGS_BYTES)
    doc["holdings"][0]["percentageOfTotalNetAssets"] = "8.334285 %"
    snap = invesco.parse_holdings_json(json.dumps(doc).encode(), FUND_DETAILS_BYTES, "QQQ", today=TODAY)
    nvda = snap.by_ticker()["NVDA"]
    assert nvda.asset_class == EQUITY and nvda.weight == pytest.approx(0.08334285)
    assert snap.meta["null_weight_tickers"] == ["USDPDV"]


def test_fetch_transport_exception_is_an_error_result():
    def boom(url: str, **kwargs) -> http_mod.Response:
        raise RuntimeError("socket exploded")

    result = invesco.fetch("QQQ", http_get=boom, now=NOW)
    assert result.status == STATUS_ERROR and result.snapshot is None
    assert "RuntimeError" in result.message and "socket exploded" in result.message
    assert result.raw_files == {}

    def boom_details(url: str, *, user_agent: str | None = http_mod.DEFAULT_USER_AGENT, **kwargs) -> http_mod.Response:
        if _is_fund_details(url):
            raise RuntimeError("details exploded")
        return happy_responder(url, user_agent)

    result = invesco.fetch("QQQ", http_get=boom_details, now=NOW)
    assert result.ok and ".fund.json" not in result.raw_files
    assert any("RuntimeError" in w for w in result.snapshot.meta["warnings"])


def test_deeply_nested_json_is_rejected_not_a_crash():
    body = b"[" * 200_000
    with pytest.raises(ValueError, match="not valid JSON"):
        invesco.parse_holdings_json(body, None, "QQQ", today=TODAY)
    with pytest.raises(ValueError, match="not valid JSON"):
        invesco.parse_fund_details_json(body)
    fake = FakeHttp(lambda url, ua: _resp(url, 200, body))
    result = invesco.fetch("QQQ", http_get=fake, now=NOW)
    assert result.status == STATUS_ERROR and "holdings document rejected" in result.message
    # nested fundDetails is non-fatal
    def responder(url: str, user_agent: str | None) -> http_mod.Response:
        return _resp(url, 200, body) if _is_fund_details(url) else happy_responder(url, user_agent)

    result = invesco.fetch("QQQ", http_get=FakeHttp(responder), now=NOW)
    assert result.ok and any("fundDetails unparseable" in w for w in result.snapshot.meta["warnings"])


# ---------------------------------------------------------------------------
# Live endpoint (opt-in)
# ---------------------------------------------------------------------------


NETWORK = os.environ.get("ETF_TRACKER_NETWORK_TESTS") == "1"


@pytest.mark.skipif(not NETWORK, reason="set ETF_TRACKER_NETWORK_TESTS=1 to call dng-api.invesco.com")
def test_live_fetch_qqq():
    result = invesco.fetch("QQQ")
    assert result.ok, result.message
    snap = result.snapshot
    assert len(snap.equities()) >= invesco.MIN_EQUITY_ROWS
    assert timedelta(0) <= date.today() - snap.as_of <= timedelta(days=7)
    assert ".json" in result.raw_files
    assert snap.meta["user_agent"] in {"project", "urllib-default"}
    if ".fund.json" in result.raw_files:
        assert snap.meta["price_derivation"] == "weight*TNA/units"
        assert snap.by_ticker()["NVDA"].price is not None
    assert invesco.validate_snapshot(snap) == []
    # Printed so a verbose run (-s) shows which date and UA the edge served.
    print(
        f"live QQQ: effectiveDate={snap.as_of.isoformat()} rows={len(snap.holdings)} "
        f"equities={snap.meta['equity_count']} ua={snap.meta['user_agent']} "
        f"fund_details={'yes' if '.fund.json' in result.raw_files else 'no'} warnings={snap.meta['warnings']}"
    )
