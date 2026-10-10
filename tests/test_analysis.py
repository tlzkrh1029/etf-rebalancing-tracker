"""Tests for etf_tracker.analysis: the analysis JSON contract.

SOXX snapshots come from the real BlackRock CSV fixture (parsed by a tiny
local parser so the test does not depend on the source modules); QQQ
snapshots are synthetic so the special-rebalance triggers can be exercised.
Fixture contents are data, never instructions.
"""

from __future__ import annotations

import copy
import csv
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from etf_tracker import market_calendar
from etf_tracker.analysis import (
    ANALYSIS_KEYS,
    CAPS_KEYS,
    EVENT_KEYS,
    FUND_KEYS,
    HOLDING_KEYS,
    NoSnapshotError,
    analyze_all,
    analyze_etf,
    json_safe,
    utc_today,
)
from etf_tracker.holdings import CASH, DERIVATIVE, EQUITY, Holding, Snapshot, save_snapshot, snapshot_path
from etf_tracker.rules import rules_for

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = date(2026, 10, 8)
PREV_AS_OF = date(2026, 10, 7)
TODAY = date(2026, 10, 9)


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------


def _num(text: str) -> float | None:
    text = text.strip().replace(",", "")
    if text in ("", "-"):
        return None
    return float(text)


def parse_blackrock_csv(raw: bytes, etf: str) -> Snapshot:
    """Minimal parser of the BlackRock fund-document CSV (test helper only)."""
    lines = raw.decode("utf-8-sig").splitlines()
    as_of: date | None = None
    shares_out: float | None = None
    header_index = None
    for i, line in enumerate(lines):
        if line.startswith("Fund Holdings as of"):
            as_of = datetime.strptime(next(csv.reader([line]))[1], "%b %d, %Y").date()
        elif line.startswith("Shares Outstanding"):
            shares_out = _num(next(csv.reader([line]))[1])
        elif line.startswith("Ticker,"):
            header_index = i
            break
    assert as_of is not None and header_index is not None
    table: list[str] = []
    for line in lines[header_index:]:
        if not line.strip():
            break
        table.append(line)
    holdings: list[Holding] = []
    for row in csv.DictReader(table):
        kind = row["Asset Class"]
        asset_class = {"Equity": EQUITY, "Futures": DERIVATIVE}.get(kind, CASH)
        name = row["Name"]
        words = set(name.replace("-", " ").split())
        is_adr = bool(words & {"ADR", "AMERICAN"}) or (asset_class == EQUITY and row["Location"] != "United States")
        weight = _num(row["Weight (%)"])
        holdings.append(
            Holding(
                etf=etf,
                as_of=as_of,
                ticker=row["Ticker"],
                name=name,
                asset_class=asset_class,
                shares=_num(row.get("Quantity") or row.get("Shares") or ""),
                price=_num(row["Price"]),
                market_value=_num(row["Market Value"]),
                weight=None if weight is None else weight / 100.0,
                sector=row["Sector"],
                is_adr=is_adr if asset_class == EQUITY else None,
            )
        )
    return Snapshot(etf=etf, as_of=as_of, source="fixture-csv", holdings=holdings, meta={"shares_outstanding": shares_out})


def soxx_snapshot() -> Snapshot:
    return parse_blackrock_csv((FIXTURES / "SOXX_holdings_2026-10-08.csv").read_bytes(), "SOXX")


def shifted_copy(snapshot: Snapshot, as_of: date, price_factor: float = 0.98, trade: dict[str, float] | None = None) -> Snapshot:
    """A copy on another date with prices scaled and optional share changes."""
    prev = copy.deepcopy(snapshot)
    prev.as_of = as_of
    for h in prev.holdings:
        h.as_of = as_of
        if h.asset_class == EQUITY and h.price is not None and h.shares is not None:
            h.price = h.price / price_factor
            if trade and h.ticker in trade:
                h.shares = h.shares * trade[h.ticker]
            h.market_value = h.shares * h.price
    prev.meta = dict(prev.meta)
    return prev


_QQQ_FILLERS = ("COST", "NFLX", "AMD", "PEP", "ADBE", "CSCO", "TMUS", "INTU", "QCOM", "TXN")
_QQQ_CALM_FILLERS = _QQQ_FILLERS + ("AMGN", "ISRG", "HON", "BKNG", "AMAT", "CMCSA")


def _qqq_weights(aapl: float, fillers: tuple[str, ...]) -> dict[str, float]:
    """Equity weights summing to 0.995 (ASML 0.001 and cash 0.004 complete the fund)."""
    big = {
        "AAPL": aapl,
        "MSFT": 0.09,
        "NVDA": 0.08,
        "AMZN": 0.06,
        "GOOGL": 0.03,
        "GOOG": 0.03,
        "META": 0.045,
        "AVGO": 0.04,
        "TSLA": 0.035,
    }
    rest = 0.995 - sum(big.values())
    weights = dict(big)
    for t in fillers:
        weights[t] = rest / len(fillers)
    return weights


#: AAPL at 26%: above the 24% company trigger; the >4.5% cohort sums above 48%.
QQQ_WEIGHTS = _qqq_weights(0.26, _QQQ_FILLERS)
#: AAPL at 9%: no trigger, no cap binding (fillers 3.09% each, cohort 42.7%, top-5 36.7%).
QQQ_CALM_WEIGHTS = _qqq_weights(0.09, _QQQ_CALM_FILLERS)


def qqq_snapshot(as_of: date = AS_OF, weights: dict[str, float] | None = None) -> Snapshot:
    weights = weights or QQQ_WEIGHTS
    tna = 1_000_000.0
    holdings = [
        Holding("QQQ", as_of, t, f"{t} Inc", EQUITY, shares=1000.0, price=w * tna / 1000.0, market_value=w * tna, weight=w)
        for t, w in weights.items()
    ]
    holdings.append(Holding("QQQ", as_of, "ASML", "ASML Holding ADR", EQUITY, 100.0, 10.0, 1000.0, 0.001, is_adr=True))
    holdings.append(Holding("QQQ", as_of, "USD", "US Dollar", CASH, None, None, 4000.0, 0.004))
    assert math.isclose(sum(h.weight for h in holdings), 1.0, abs_tol=1e-9)
    meta = {"nav": 500.0, "shares_outstanding": 2000.0, "total_net_assets": tna + 5000.0}
    return Snapshot(etf="QQQ", as_of=as_of, source="synthetic", holdings=holdings, meta=meta)


def store(root: Path, *snapshots: Snapshot) -> None:
    for s in snapshots:
        save_snapshot(s, snapshot_path(root, s.etf, s.as_of))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    curr = soxx_snapshot()
    prev = shifted_copy(curr, PREV_AS_OF, trade={"AMD": 0.9})  # AMD was bought (shares up 11%) into the current snapshot
    store(tmp_path, prev, curr, qqq_snapshot(PREV_AS_OF), qqq_snapshot(AS_OF))
    return tmp_path


# ---------------------------------------------------------------------------
# Contract: keys, types, serialisability
# ---------------------------------------------------------------------------


def _assert_number_or_none(value) -> None:
    assert value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))


def test_analyze_etf_soxx_has_exactly_the_contract_keys(root: Path):
    a = analyze_etf(root, "soxx", TODAY)
    assert tuple(a) == ANALYSIS_KEYS
    assert a["etf"] == "SOXX" and a["index_id"] == "SOXX"
    assert a["as_of"] == "2026-10-08" and a["prev_as_of"] == "2026-10-07"
    assert a["source"] == "fixture-csv"
    assert tuple(a["fund"]) == FUND_KEYS
    assert tuple(a["caps"]) == CAPS_KEYS
    for row in a["holdings"]:
        assert tuple(row) == HOLDING_KEYS
    for ev in a["next_events"]:
        assert tuple(ev) == EVENT_KEYS
    assert isinstance(a["warnings"], list) and all(isinstance(w, str) for w in a["warnings"])
    json.dumps(a, allow_nan=False)


def test_analyze_etf_fund_block(root: Path):
    a = analyze_etf(root, "SOXX", TODAY)
    fund = a["fund"]
    assert fund["shares_outstanding"] == 82_700_000.0
    assert fund["prev_shares_outstanding"] == 82_700_000.0
    assert fund["nav"] is None
    # iShares does not publish TNA: derived from the holdings' market values
    assert fund["total_net_assets"] == pytest.approx(sum(h.market_value for h in soxx_snapshot().holdings if h.market_value))
    for v in fund.values():
        _assert_number_or_none(v)


def test_analyze_etf_holdings_sorted_by_weight_desc_with_fractions(root: Path):
    a = analyze_etf(root, "SOXX", TODAY)
    weights = [row["weight"] for row in a["holdings"]]
    assert weights == sorted(weights, reverse=True)
    assert a["holdings"][0]["ticker"] == "AMD"
    assert 0.09 < a["holdings"][0]["weight"] < 0.10  # 9.50% as a fraction
    assert len(a["holdings"]) == len(soxx_snapshot().holdings)
    tsm = next(row for row in a["holdings"] if row["ticker"] == "TSM")
    assert tsm["is_adr"] is True
    assert all(isinstance(row["is_adr"], bool) for row in a["holdings"])
    for row in a["holdings"]:
        for key in ("weight", "shares", "price", "market_value"):
            _assert_number_or_none(row[key])


def test_analyze_etf_decomposition_present_with_two_snapshots(root: Path):
    a = analyze_etf(root, "SOXX", TODAY)
    d = a["decomposition"]
    assert d is not None
    assert d["etf"] == "SOXX" and d["prev_as_of"] == "2026-10-07" and d["curr_as_of"] == "2026-10-08"
    changes = {c["ticker"]: c for c in d["changes"]}
    assert changes["AMD"]["classification"] == "active_trade"
    assert changes["AMD"]["trade"] > 0
    assert changes["NVDA"]["classification"] == "flow_only"
    assert abs(sum(c["drift"] for c in d["changes"])) < 1e-9
    assert abs(sum(c["trade"] for c in d["changes"])) < 1e-9


def test_analyze_etf_caps_block_soxx(root: Path):
    a = analyze_etf(root, "SOXX", TODAY)
    caps = a["caps"]
    assert caps["event_type"] == rules_for("SOXX").event_type_for(TODAY) == "quarterly_rebalance"
    assert caps["feasible"] is True
    assert caps["special_rebalance"] is None
    assert any("8%" in b for b in caps["binding_constraints"])
    rules = {b["rule"] for b in caps["breaches"]}
    assert "soxx_single_cap_8" in rules
    for b in caps["breaches"]:
        assert tuple(b) == ("rule", "description", "tickers", "value", "limit")
        assert isinstance(b["tickers"], list)
    sellers = caps["forced_sellers"]
    buyers = caps["forced_buyers"]
    assert sellers and sellers[0][0] == "AMD" and sellers[0][1] < 0
    assert [d for _, d in sellers] == sorted(d for _, d in sellers)
    assert buyers and buyers[0][1] > 0
    assert [d for _, d in buyers] == sorted((d for _, d in buyers), reverse=True)
    assert abs(sum(d for _, d in sellers) + sum(d for _, d in buyers)) < 1e-9
    assert {"top5_sum", "adr_sum", "names_over_8", "names_over_4_outside_top5", "max_weight"} <= set(caps["metrics"])
    assert caps["target_metrics"]["max_weight"] <= 0.08 + 1e-9
    assert caps["metrics"]["adr_sum"] > 0  # TSM is flagged ADR by the fixture parser


def test_analyze_etf_next_events_countdowns(root: Path):
    a = analyze_etf(root, "SOXX", TODAY)
    events = a["next_events"]
    assert len(events) == 3
    first = events[0]
    assert first["reference_date"] == "2026-11-30"
    assert first["effective_trade_date"] == "2026-12-18"
    assert first["announcement_date"] == "2026-12-04"
    assert first["trading_days_to_reference"] == market_calendar.trading_days_between(TODAY, date(2026, 11, 30))
    assert first["trading_days_to_trade"] == market_calendar.trading_days_between(TODAY, date(2026, 12, 18))
    assert isinstance(first["trading_days_to_trade"], int) and not isinstance(first["trading_days_to_trade"], bool)
    assert [e["effective_trade_date"] for e in events] == sorted(e["effective_trade_date"] for e in events)


def test_analyze_etf_today_is_injectable_and_defaults_to_utc(root: Path):
    later = date(2026, 12, 19)  # the Saturday after the December trade date
    a = analyze_etf(root, "SOXX", later)
    assert a["next_events"][0]["effective_trade_date"] == "2027-03-19"
    assert a["next_events"][0]["trading_days_to_trade"] == market_calendar.trading_days_between(later, date(2027, 3, 19))
    assert any("older than the last completed session" in w for w in a["warnings"])
    assert utc_today(datetime(2026, 10, 9, 23, 30, tzinfo=timezone(timedelta(hours=-5)))) == date(2026, 10, 10)


# ---------------------------------------------------------------------------
# QQQ: special rebalance only there
# ---------------------------------------------------------------------------


def test_analyze_etf_qqq_special_rebalance_and_company_caps(root: Path):
    a = analyze_etf(root, "qqq", TODAY)
    assert a["etf"] == "QQQ" and a["index_id"] == "QQQ"
    caps = a["caps"]
    assert caps["event_type"] == "annual_reconstitution"  # December event is next after 2026-10-09
    special = caps["special_rebalance"]
    assert special is not None
    assert tuple(special) == ("triggered", "reasons", "metrics")
    assert special["triggered"] is True
    assert any("AAPL" in r and "24%" in r for r in special["reasons"])
    assert special["metrics"]["max_company_weight"] == pytest.approx(0.26 / 0.996, rel=1e-6)
    assert caps["forced_sellers"][0][0] == "AAPL"
    assert any(b["rule"].startswith("qqq") for b in caps["breaches"])
    assert any("special rebalance" in w for w in a["warnings"])
    assert a["fund"] == {"shares_outstanding": 2000.0, "prev_shares_outstanding": 2000.0, "nav": 500.0, "total_net_assets": 1_005_000.0}
    assert {"max_company_weight", "sum_over_4_5", "top5_security_sum", "max_security_weight"} <= set(caps["metrics"])
    json.dumps(a, allow_nan=False)


def test_analyze_etf_qqq_not_triggered_when_calm(tmp_path: Path):
    calm = QQQ_CALM_WEIGHTS
    assert all(w < 0.045 for t, w in calm.items() if t in _QQQ_CALM_FILLERS)
    store(tmp_path, qqq_snapshot(AS_OF, calm))
    a = analyze_etf(tmp_path, "QQQ", TODAY)
    assert a["caps"]["special_rebalance"]["triggered"] is False
    assert a["caps"]["special_rebalance"]["reasons"] == []
    assert a["caps"]["breaches"] == []
    assert a["caps"]["forced_sellers"] == [] and a["caps"]["forced_buyers"] == []


# ---------------------------------------------------------------------------
# Single snapshot, missing snapshot, analyze_all
# ---------------------------------------------------------------------------


def test_analyze_etf_single_snapshot_has_null_decomposition(tmp_path: Path):
    store(tmp_path, soxx_snapshot())
    a = analyze_etf(tmp_path, "SOXX", TODAY)
    assert a["prev_as_of"] is None
    assert a["decomposition"] is None
    assert a["fund"]["prev_shares_outstanding"] is None
    assert any("no previous snapshot" in w for w in a["warnings"])
    assert a["caps"]["breaches"]  # caps still computed


def test_adr_provenance_notes_only_for_indices_with_an_adr_rule(tmp_path: Path):
    """SOXX has the 10% ADR cap, so the heuristic/override provenance is a
    warning there; IGV's index excludes ADRs and never reads the flag."""
    soxx = soxx_snapshot()
    soxx.meta["adr_heuristic"] = ["TSM"]
    soxx.meta["adr_overrides"] = ["TSEM"]
    store(tmp_path, soxx)
    a = analyze_etf(tmp_path, "SOXX", TODAY)
    assert any(w == "ADR flag inferred from listing location for: TSM" for w in a["warnings"])
    assert any(w == "ADR flag overridden to False by configuration for: TSEM" for w in a["warnings"])

    igv = copy.deepcopy(soxx)
    igv.etf = "IGV"
    for h in igv.holdings:
        h.etf = "IGV"
    igv.meta["adr_heuristic"] = ["DSGX", "OTEX"]
    store(tmp_path, igv)
    b = analyze_etf(tmp_path, "IGV", TODAY)
    assert not any(w.startswith("ADR flag") for w in b["warnings"])


def test_derivation_notes_kept_for_equities_but_not_for_cash_lines(tmp_path: Path):
    """Invesco publishes no price for cash/futures lines, so validate()'s
    "derived price" note for them is expected and must not reach the report;
    the same note for an equity line is a real data-quality signal."""

    def line(ticker, asset_class, shares, price, market_value, weight):
        return Holding(
            etf="SOXX", as_of=AS_OF, ticker=ticker, name=ticker, asset_class=asset_class,
            shares=shares, price=price, market_value=market_value, weight=weight,
        )

    holdings = [
        line("AAA", EQUITY, 100.0, 10.0, 1000.0, 0.25),
        line("BBB", EQUITY, 100.0, None, 1000.0, 0.25),  # equity without a price: keep the note
        line("CCC", EQUITY, 100.0, 10.0, 1000.0, 0.25),
        line("DDD", EQUITY, 100.0, 10.0, 980.0, 0.245),
        line("USD", CASH, 20.0, None, 20.0, 0.005),  # cash without a price: expected, drop the note
        line("NQZ6", DERIVATIVE, 1.0, None, 0.0, 0.0),
    ]
    store(tmp_path, Snapshot(etf="SOXX", as_of=AS_OF, holdings=holdings, source="test"))
    a = analyze_etf(tmp_path, "SOXX", TODAY)
    assert any(w.startswith("BBB: derived price") for w in a["warnings"])
    assert not any(w.startswith("USD: derived") for w in a["warnings"])
    assert not any(w.startswith("NQZ6: derived") for w in a["warnings"])


def _plain_snapshot(as_of: date, aaa_shares: float) -> Snapshot:
    holdings = [
        Holding("SOXX", as_of, "AAA", "AAA name", EQUITY, shares=aaa_shares, price=10.0, market_value=10.0 * aaa_shares, weight=0.5),
        Holding("SOXX", as_of, "BBB", "BBB name", EQUITY, shares=100.0, price=10.0, market_value=1000.0, weight=0.5),
    ]
    return Snapshot(etf="SOXX", as_of=as_of, holdings=holdings, source="test", meta={"shares_outstanding": 1000.0})


def test_decomposition_spanning_several_sessions_is_flagged(tmp_path: Path):
    # Monday 2026-10-05 -> Thursday 2026-10-08: three sessions (an issuer outage in between)
    store(tmp_path, _plain_snapshot(date(2026, 10, 5), 100.0))
    store(tmp_path, _plain_snapshot(date(2026, 10, 8), 100.0))
    a = analyze_etf(tmp_path, "SOXX", date(2026, 10, 9))
    assert a["decomposition"]["prev_as_of"] == "2026-10-05" and a["decomposition"]["curr_as_of"] == "2026-10-08"
    assert any(w.startswith("decomposition spans 3 sessions (2026-10-05 -> 2026-10-08)") for w in a["warnings"])
    # consecutive sessions: no such warning
    store(tmp_path, _plain_snapshot(date(2026, 10, 7), 100.0))
    a = analyze_etf(tmp_path, "SOXX", date(2026, 10, 9))
    assert a["decomposition"]["prev_as_of"] == "2026-10-07"
    assert not any(w.startswith("decomposition spans") for w in a["warnings"])


def test_analyze_etf_without_snapshots_raises(tmp_path: Path):
    with pytest.raises(NoSnapshotError):
        analyze_etf(tmp_path, "SOXX", TODAY)


def test_analyze_etf_unknown_etf_raises_key_error(root: Path):
    with pytest.raises(KeyError):
        analyze_etf(root, "SPY", TODAY)


def test_analyze_all_collects_failures(root: Path):
    now = datetime(2026, 10, 9, 14, 30, 5, tzinfo=timezone.utc)
    out = analyze_all(root, ["SOXX", "QQQ", "IGV", "SPY"], TODAY, now=now)
    assert tuple(out) == ("generated_utc", "today", "etfs", "errors")
    assert out["generated_utc"] == "2026-10-09T14:30:05Z"
    assert out["today"] == "2026-10-09"
    assert set(out["etfs"]) == {"SOXX", "QQQ"}
    assert "no snapshot" in out["errors"]["IGV"]
    assert "SPY" in out["errors"] and "KeyError" in out["errors"]["SPY"]
    json.dumps(out, allow_nan=False)


def test_analyze_all_today_defaults_from_now(root: Path):
    now = datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc)
    out = analyze_all(root, ["SOXX"], now=now)
    assert out["today"] == "2026-10-10"


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def test_json_safe_converts_everything_the_contract_forbids():
    class WithDict:
        def to_dict(self):
            return {"d": date(2026, 1, 2), "t": (1, 2)}

    value = json_safe(
        {
            "nan": float("nan"),
            "inf": float("inf"),
            "date": date(2026, 10, 8),
            "set": {1},
            "tuple": ("a", 1.5),
            "path": Path("x"),
            "obj": WithDict(),
            "ok": [True, None, 3],
        }
    )
    assert value == {
        "nan": None,
        "inf": None,
        "date": "2026-10-08",
        "set": [1],
        "tuple": ["a", 1.5],
        "path": "x",
        "obj": {"d": "2026-01-02", "t": [1, 2]},
        "ok": [True, None, 3],
    }
    json.dumps(value, allow_nan=False)


# ---------------------------------------------------------------------------
# The real data/ directory (tracked in git): the contract on what the daily
# job actually stores.  Skipped in a checkout without data.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _walk(obj, path="", *, on_value):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, f"{path}/{k}", on_value=on_value)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, f"{path}[{i}]", on_value=on_value)
    else:
        on_value(path, obj)


def test_analyze_all_on_the_real_data_directory_meets_the_contract():
    if not (REPO_ROOT / "data" / "normalized").is_dir():
        pytest.skip("no data/ directory in this checkout")
    out = analyze_all(REPO_ROOT, ["SOXX", "QQQ", "IGV"], TODAY)
    assert out["today"] == "2026-10-09"
    assert out["errors"] == {}, out["errors"]
    assert set(out["etfs"]) == {"SOXX", "QQQ", "IGV"}
    json.dumps(out, allow_nan=False)

    problems: list[str] = []

    def check(path: str, value) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{path}: non-finite {value}")
        leaf = path.rsplit("/", 1)[-1]
        if leaf in ("as_of", "prev_as_of", "today") or leaf.endswith("_date"):
            if value is not None and not (isinstance(value, str) and _ISO_DATE_RE.match(value)):
                problems.append(f"{path}: not an ISO date: {value!r}")
        if isinstance(value, bool) and leaf in ("trading_days_to_reference", "trading_days_to_trade"):
            problems.append(f"{path}: bool where int expected")

    _walk(out, on_value=check)
    assert problems == []

    for etf, a in out["etfs"].items():
        assert tuple(a) == ANALYSIS_KEYS
        assert tuple(a["caps"]) == CAPS_KEYS
        assert all(tuple(ev) == EVENT_KEYS for ev in a["next_events"])
        assert all(tuple(row) == HOLDING_KEYS for row in a["holdings"])
        weights = [row["weight"] for row in a["holdings"] if row["weight"] is not None]
        assert weights == sorted(weights, reverse=True)
        assert 0.97 < sum(weights) < 1.03, (etf, sum(weights))
        assert a["caps"]["feasible"] is True
        for ev in a["next_events"]:
            ref, trade = date.fromisoformat(ev["reference_date"]), date.fromisoformat(ev["effective_trade_date"])
            assert ev["trading_days_to_reference"] == market_calendar.trading_days_between(TODAY, ref)
            assert ev["trading_days_to_trade"] == market_calendar.trading_days_between(TODAY, trade)

    # the December events: every index trades on 2026-12-18
    soxx, qqq, igv = out["etfs"]["SOXX"], out["etfs"]["QQQ"], out["etfs"]["IGV"]
    assert soxx["caps"]["event_type"] == "quarterly_rebalance"
    assert soxx["next_events"][0]["reference_date"] == "2026-11-30"
    assert soxx["next_events"][0]["trading_days_to_reference"] == 35
    assert soxx["next_events"][0]["trading_days_to_trade"] == 49
    assert qqq["caps"]["event_type"] == "annual_reconstitution"  # security-level rules apply
    assert {"max_security_weight", "top5_security_sum"} <= set(qqq["caps"]["metrics"])
    assert qqq["caps"]["special_rebalance"] is not None and qqq["caps"]["special_rebalance"]["triggered"] is False
    assert igv["caps"]["event_type"] == "semiannual_reconstitution"
    assert {e["effective_trade_date"] for e in (soxx["next_events"][0], qqq["next_events"][0], igv["next_events"][0])} == {"2026-12-18"}
    # SOXX's ADR flags reach the caps (TSM is inferred from its listing location)
    assert soxx["caps"]["metrics"]["adr_sum"] > 0.05
    assert any(w.startswith("ADR flag inferred") for w in soxx["warnings"])
