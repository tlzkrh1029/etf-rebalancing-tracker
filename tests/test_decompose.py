"""Tests for etf_tracker.decompose (drift vs trade decomposition)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from etf_tracker import decompose as dc
from etf_tracker.decompose import (
    CLASS_ACTIVE_TRADE,
    CLASS_CORPORATE_ACTION,
    CLASS_ENTRY,
    CLASS_EXIT,
    CLASS_FLOW_ONLY,
    DecompositionResult,
    TickerChange,
    decompose,
    decompose_from_weights,
    decompose_latest,
    match_split_ratio,
)
from etf_tracker.holdings import CASH, EQUITY, Holding, Snapshot, save_snapshot, snapshot_path

D0 = date(2026, 10, 7)
D1 = date(2026, 10, 8)
TOL = 1e-12

# ticker -> (shares, price); market values 10k, 20k, 10k, 10k, 20k = 70k.
BASE: dict[str, tuple[float, float]] = {
    "AAA": (1000.0, 10.0),
    "BBB": (500.0, 40.0),
    "CCC": (2000.0, 5.0),
    "DDD": (100.0, 100.0),
    "EEE": (800.0, 25.0),
}


def snap(
    as_of: date,
    positions: dict[str, tuple[float, float]],
    cash: float | None = None,
    meta: dict | None = None,
    etf: str = "SOXX",
) -> Snapshot:
    holdings = [
        Holding(etf, as_of, t, f"{t} Inc", EQUITY, shares, price, None, None)
        for t, (shares, price) in positions.items()
    ]
    if cash is not None:
        holdings.append(Holding(etf, as_of, "USD", "US Dollar", CASH, None, None, cash, None))
    return Snapshot(etf, as_of, "test", holdings, meta or {})


def with_changes(base: dict[str, tuple[float, float]], **overrides: tuple[float, float]) -> dict[str, tuple[float, float]]:
    out = dict(base)
    out.update(overrides)
    return out


def assert_sums_to_zero(result: DecompositionResult) -> None:
    assert sum(c.w_prev for c in result.changes) == pytest.approx(1.0, abs=TOL)
    assert sum(c.w_cf for c in result.changes) == pytest.approx(1.0, abs=TOL)
    assert sum(c.w_curr for c in result.changes) == pytest.approx(1.0, abs=TOL)
    assert abs(sum(c.drift for c in result.changes)) < TOL
    assert abs(sum(c.trade for c in result.changes)) < TOL
    for c in result.changes:
        assert c.drift + c.trade == pytest.approx(c.w_curr - c.w_prev, abs=TOL)


# --------------------------------------------------------------------------
# (a) prices only
# --------------------------------------------------------------------------


def test_price_only_move_is_pure_drift() -> None:
    prev = snap(D0, BASE)
    curr = snap(D1, with_changes(BASE, AAA=(1000.0, 11.0), CCC=(2000.0, 4.0)))
    result = decompose(prev, curr)

    assert result.scale_factor == 1.0
    assert result.mode == dc.MODE_SHARES_AND_PRICES
    assert result.entries == [] and result.exits == []
    assert_sums_to_zero(result)

    # Hand computation: prev total 70,000; curr total 11,000+20,000+8,000+10,000+20,000 = 69,000.
    aaa = result.get("AAA")
    assert aaa is not None
    assert aaa.w_prev == pytest.approx(10_000 / 70_000, abs=TOL)
    assert aaa.w_cf == pytest.approx(11_000 / 69_000, abs=TOL)
    assert aaa.w_curr == pytest.approx(11_000 / 69_000, abs=TOL)
    assert aaa.drift == pytest.approx(11_000 / 69_000 - 10_000 / 70_000, abs=TOL)
    assert aaa.price_return == pytest.approx(0.10)
    ccc = result.get("CCC")
    assert ccc.drift == pytest.approx(8_000 / 69_000 - 10_000 / 70_000, abs=TOL)
    assert ccc.drift < 0 < aaa.drift
    # Unchanged-price names still drift (down) because the fund grew.
    assert result.get("BBB").drift == pytest.approx(20_000 / 69_000 - 20_000 / 70_000, abs=TOL)

    for c in result.changes:
        assert abs(c.trade) < TOL
        assert c.rel_share_change == pytest.approx(0.0, abs=TOL)
        assert c.classification == CLASS_FLOW_ONLY
    assert result.summary["gross_trade"] < TOL
    assert result.summary["largest_drift_up"]["ticker"] == "AAA"
    assert result.summary["largest_drift_down"]["ticker"] == "CCC"
    assert result.summary["portfolio_return"] == pytest.approx(69_000 / 70_000 - 1)


# --------------------------------------------------------------------------
# (b) uniform creation
# --------------------------------------------------------------------------


def test_uniform_creation_is_flow_only() -> None:
    prev = snap(D0, BASE, meta={"shares_outstanding": 1_000_000, "total_net_assets": 70_000.0})
    curr = snap(
        D1,
        {t: (s * 1.1, p) for t, (s, p) in BASE.items()},
        meta={"shares_outstanding": 1_100_000, "total_net_assets": 77_000.0, "nav": 0.07},
    )
    result = decompose(prev, curr)

    assert result.scale_factor == pytest.approx(1.1, abs=1e-12)
    assert_sums_to_zero(result)
    for c in result.changes:
        assert c.rel_share_change == pytest.approx(0.0, abs=TOL)
        assert abs(c.trade) < TOL
        assert abs(c.drift) < TOL
        assert c.classification == CLASS_FLOW_ONLY
        assert c.shares_prev_adjusted == pytest.approx(c.shares_curr)

    flow = result.summary["fund_flow"]
    assert flow["flow_pct_from_scale_factor"] == pytest.approx(0.1, abs=1e-12)
    assert flow["implied_flow_usd_from_scale_factor"] == pytest.approx(7_000.0)
    assert flow["flow_shares"] == 100_000
    assert flow["flow_pct_from_shares_outstanding"] == pytest.approx(0.1)
    assert flow["implied_flow_usd_from_shares_outstanding"] == pytest.approx(7_000.0)
    assert flow["implied_flow_usd_from_net_assets"] == pytest.approx(7_000.0)
    assert flow["flow_pct_from_net_assets"] == pytest.approx(0.1)


# --------------------------------------------------------------------------
# (c) one name sold
# --------------------------------------------------------------------------


def test_single_name_sale_is_active_trade() -> None:
    prev = snap(D0, BASE)
    curr = snap(D1, with_changes(BASE, CCC=(1600.0, 5.0)))
    result = decompose(prev, curr)

    assert result.scale_factor == 1.0
    assert_sums_to_zero(result)
    ccc = result.get("CCC")
    assert ccc.classification == CLASS_ACTIVE_TRADE
    assert ccc.rel_share_change == pytest.approx(-0.2)
    assert ccc.trade < 0
    assert abs(ccc.drift) < TOL  # prices did not move
    # Hand check: w_cf = 10,000/70,000, w_curr = 8,000/68,000.
    assert ccc.trade == pytest.approx(8_000 / 68_000 - 10_000 / 70_000, abs=TOL)
    for c in result.changes:
        if c.ticker != "CCC":
            assert c.classification == CLASS_FLOW_ONLY
            assert c.trade > 0  # everyone else gains weight
            assert c.rel_share_change == pytest.approx(0.0, abs=TOL)
    assert result.summary["active_trade_tickers"] == ["CCC"]
    assert result.summary["n_active_trades"] == 1
    assert result.summary["largest_trade_down"]["ticker"] == "CCC"
    assert result.summary["gross_trade"] == pytest.approx(abs(ccc.trade), abs=TOL)
    assert result.active_trades()[0].ticker == "CCC"


def test_threshold_is_respected() -> None:
    prev = snap(D0, BASE)
    curr = snap(D1, with_changes(BASE, CCC=(2000.0 * 1.004, 5.0)))
    assert decompose(prev, curr).get("CCC").classification == CLASS_FLOW_ONLY
    assert decompose(prev, curr, active_trade_threshold=0.001).get("CCC").classification == CLASS_ACTIVE_TRADE


# --------------------------------------------------------------------------
# (d) stock split
# --------------------------------------------------------------------------


def test_two_for_one_split_is_corporate_action_with_zero_trade() -> None:
    prev = snap(D0, BASE)
    curr = snap(D1, with_changes(BASE, DDD=(200.0, 50.0)))
    result = decompose(prev, curr)

    assert result.scale_factor == 1.0
    assert_sums_to_zero(result)
    ddd = result.get("DDD")
    assert ddd.classification == CLASS_CORPORATE_ACTION
    assert ddd.split_ratio == 2.0
    assert abs(ddd.trade) < TOL
    assert abs(ddd.drift) < TOL
    assert ddd.price_return == pytest.approx(0.0, abs=TOL)
    assert ddd.shares_prev == 100.0
    assert ddd.shares_prev_adjusted == pytest.approx(200.0)
    assert ddd.rel_share_change == pytest.approx(0.0, abs=TOL)
    assert ddd.price_prev == pytest.approx(50.0)  # restated
    assert any("corporate action" in w for w in result.warnings)
    for c in result.changes:
        assert abs(c.trade) < TOL
    assert result.summary["n_corporate_action_suspects"] == 1


def test_split_detected_alongside_flow_and_market_move() -> None:
    # 10% creation everywhere, DDD splits 2:1 and also rallies 2%.
    prev = snap(D0, BASE)
    curr = snap(D1, {**{t: (s * 1.1, p) for t, (s, p) in BASE.items()}, "DDD": (220.0, 51.0)})
    result = decompose(prev, curr)
    assert result.scale_factor == pytest.approx(1.1)
    ddd = result.get("DDD")
    assert ddd.classification == CLASS_CORPORATE_ACTION
    assert ddd.split_ratio == 2.0
    assert ddd.price_return == pytest.approx(0.02)
    assert ddd.rel_share_change == pytest.approx(0.0, abs=1e-9)
    assert abs(ddd.trade) < 1e-9
    assert ddd.drift > 0


def test_reverse_split_and_non_split_doubling() -> None:
    prev = snap(D0, BASE)
    reverse = decompose(prev, snap(D1, with_changes(BASE, AAA=(250.0, 40.0))))
    aaa = reverse.get("AAA")
    assert aaa.classification == CLASS_CORPORATE_ACTION
    assert aaa.split_ratio == pytest.approx(0.25)
    assert abs(aaa.trade) < TOL

    # Shares doubled but price unchanged: a genuine purchase, not a split.
    bought = decompose(prev, snap(D1, with_changes(BASE, AAA=(2000.0, 10.0))))
    assert bought.get("AAA").classification == CLASS_ACTIVE_TRADE
    assert bought.get("AAA").trade > 0

    # Detection can be switched off.
    off = decompose(prev, snap(D1, with_changes(BASE, DDD=(200.0, 50.0))), detect_corporate_actions=False)
    assert off.get("DDD").classification == CLASS_ACTIVE_TRADE
    assert off.get("DDD").split_ratio is None


def test_match_split_ratio() -> None:
    assert match_split_ratio(2.0) == 2.0
    assert match_split_ratio(2.005) == 2.0
    assert match_split_ratio(2.05) is None
    assert match_split_ratio(0.1) == pytest.approx(0.1)
    assert match_split_ratio(1.0) is None
    assert match_split_ratio(1.003) is None
    assert match_split_ratio(0.0) is None


# --------------------------------------------------------------------------
# (e) entries and exits
# --------------------------------------------------------------------------


def test_entry_and_exit() -> None:
    prev = snap(D0, BASE)
    curr_positions = {t: v for t, v in BASE.items() if t != "EEE"}
    curr_positions["FFF"] = (300.0, 50.0)
    curr = snap(D1, curr_positions)
    result = decompose(prev, curr)

    assert result.entries == ["FFF"]
    assert result.exits == ["EEE"]
    assert_sums_to_zero(result)

    fff = result.get("FFF")
    assert fff.classification == CLASS_ENTRY
    assert fff.w_prev == 0.0 and fff.w_cf == 0.0
    assert fff.drift == 0.0
    assert fff.trade == pytest.approx(fff.w_curr, abs=TOL)
    assert fff.w_curr == pytest.approx(15_000 / 65_000, abs=TOL)
    assert fff.rel_share_change is None and fff.price_return is None

    eee = result.get("EEE")
    assert eee.classification == CLASS_EXIT
    assert eee.w_curr == 0.0
    assert eee.trade == pytest.approx(-eee.w_cf, abs=TOL)
    assert eee.w_cf == pytest.approx(20_000 / 70_000, abs=TOL)
    assert dc.FLAG_EXIT_PRICE_ASSUMED in eee.flags
    assert eee.price_return == 0.0
    assert any("EEE" in w and "previous price" in w for w in result.warnings)
    assert result.summary["n_entries"] == 1 and result.summary["n_exits"] == 1


def test_exit_uses_fallback_price_when_given() -> None:
    prev = snap(D0, BASE)
    curr = snap(D1, {t: v for t, v in BASE.items() if t != "EEE"})
    result = decompose(prev, curr, fallback_prices={"EEE": 30.0})
    eee = result.get("EEE")
    assert eee.price_curr == 30.0
    assert eee.price_return == pytest.approx(0.2)
    assert dc.FLAG_EXIT_PRICE_ASSUMED not in eee.flags
    assert eee.w_cf == pytest.approx(24_000 / 74_000, abs=TOL)
    assert eee.drift > 0
    assert_sums_to_zero(result)


def test_zero_sized_line_counts_as_entry_or_exit() -> None:
    prev = snap(D0, with_changes(BASE, EEE=(0.0, 25.0)))
    curr = snap(D1, BASE)
    result = decompose(prev, curr)
    assert result.get("EEE").classification == CLASS_ENTRY
    assert result.entries == ["EEE"]


# --------------------------------------------------------------------------
# Cash lines, duplicates and messy universes
# --------------------------------------------------------------------------


def messy_pair() -> tuple[Snapshot, Snapshot]:
    """5% creation, prices move, CCC sold 10%, cash grows, EEE exits, FFF enters."""
    prev = snap(D0, BASE, cash=3_000.0)
    k = 1.05
    curr_positions = {
        "AAA": (1000.0 * k, 10.5),
        "BBB": (500.0 * k, 39.0),
        "CCC": (2000.0 * k * 0.9, 5.2),
        "DDD": (100.0 * k, 101.0),
        "FFF": (300.0, 50.0),
    }
    curr = snap(D1, curr_positions, cash=5_000.0)
    return prev, curr


def test_cash_included_and_sums_hold_in_messy_case() -> None:
    prev, curr = messy_pair()
    result = decompose(prev, curr)
    assert result.scale_factor == pytest.approx(1.05)
    assert_sums_to_zero(result)
    usd = result.get("USD")
    assert usd is not None
    assert usd.asset_class == CASH
    assert usd.price_prev == 1.0 and usd.price_curr == 1.0
    assert usd.shares_prev == 3_000.0 and usd.shares_curr == 5_000.0
    assert usd.w_prev == pytest.approx(3_000 / 73_000, abs=TOL)
    assert usd.trade > 0  # cash grew beyond the 5% flow
    assert result.get("CCC").classification == CLASS_ACTIVE_TRADE
    assert result.get("CCC").rel_share_change == pytest.approx(-0.1)
    assert result.get("AAA").classification == CLASS_FLOW_ONLY
    assert result.entries == ["FFF"] and result.exits == ["EEE"]
    # Equity-only summary statistics ignore the cash line.
    assert result.summary["active_trade_tickers"] == ["CCC"]


def test_duplicate_lines_are_merged() -> None:
    prev = snap(D0, BASE, cash=1_000.0)
    prev.holdings.append(Holding("SOXX", D0, "USD", "US Dollar", CASH, None, None, 500.0, None))
    curr = snap(D1, BASE, cash=1_500.0)
    result = decompose(prev, curr)
    usd = result.get("USD")
    assert usd.shares_prev == 1_500.0
    assert abs(usd.trade) < TOL
    assert any("duplicate lines for USD merged" in w for w in result.warnings)
    assert_sums_to_zero(result)


def test_equity_without_shares_falls_back_to_market_value() -> None:
    prev = snap(D0, BASE)
    curr = snap(D1, BASE)
    # Replace AAA by a line that only has a market value and price.
    prev.holdings[0] = Holding("SOXX", D0, "AAA", "AAA Inc", EQUITY, None, 10.0, 10_000.0, None)
    result = decompose(prev, curr)
    aaa = result.get("AAA")
    assert dc.FLAG_SHARES_MISSING in aaa.flags
    assert aaa.rel_share_change is None
    assert aaa.classification == CLASS_FLOW_ONLY
    assert aaa.w_prev == pytest.approx(10_000 / 70_000, abs=TOL)
    assert_sums_to_zero(result)


# --------------------------------------------------------------------------
# (f) Mode B reproduces mode A
# --------------------------------------------------------------------------


def test_mode_b_matches_mode_a() -> None:
    prev, curr = messy_pair()
    mode_a = decompose(prev, curr)

    prev_prices = {h.ticker: h.price for h in prev.equities()}
    curr_prices = {h.ticker: h.price for h in curr.equities()}
    returns = {t: curr_prices[t] / prev_prices[t] - 1.0 for t in prev_prices if t in curr_prices}
    returns["USD"] = 0.0
    mode_b = decompose_from_weights(
        prev.normalized_weights(),
        curr.normalized_weights(),
        returns,
        etf="SOXX",
        prev_as_of=D0,
        curr_as_of=D1,
        asset_classes={"USD": CASH},
    )

    assert mode_b.mode == dc.MODE_WEIGHTS_AND_RETURNS
    assert mode_b.scale_factor is None
    assert set(mode_b.by_ticker()) == set(mode_a.by_ticker())
    assert mode_b.entries == mode_a.entries and mode_b.exits == mode_a.exits
    assert_sums_to_zero(mode_b)
    for ticker, a in mode_a.by_ticker().items():
        b = mode_b.get(ticker)
        assert b is not None
        assert b.w_prev == pytest.approx(a.w_prev, abs=TOL)
        assert b.w_cf == pytest.approx(a.w_cf, abs=TOL)
        assert b.w_curr == pytest.approx(a.w_curr, abs=TOL)
        assert b.drift == pytest.approx(a.drift, abs=TOL)
        assert b.trade == pytest.approx(a.trade, abs=TOL)
        assert b.classification == a.classification
        if a.rel_share_change is None:
            assert b.rel_share_change is None
        else:
            assert b.rel_share_change == pytest.approx(a.rel_share_change, abs=1e-9)
    # EEE exited: no return available, treated as 0 and flagged.
    assert dc.FLAG_RETURN_MISSING in mode_b.get("EEE").flags
    assert any("no return" in w and "EEE" in w for w in mode_b.warnings)
    # The median implied ratio absorbs what k absorbs in mode A: a finite, positive number.
    assert mode_b.summary["median_implied_share_ratio"] > 0
    assert mode_b.summary["n_active_trades"] == mode_a.summary["n_active_trades"] == 1


def test_mode_b_normalizes_weights_and_handles_missing_returns() -> None:
    prev_w = {"AAA": 0.45, "BBB": 0.45}  # sums to 0.9
    curr_w = {"AAA": 0.5, "BBB": 0.5}
    result = decompose_from_weights(prev_w, curr_w, {"AAA": 0.1})
    assert any("prev weights sum to 0.9" in w for w in result.warnings)
    assert dc.FLAG_RETURN_MISSING in result.get("BBB").flags
    assert dc.FLAG_RETURN_MISSING not in result.get("AAA").flags
    # w_prev normalized to 0.5/0.5; AAA +10%, BBB flat -> w_cf = 0.55/1.05, 0.5/1.05.
    aaa = result.get("AAA")
    assert aaa.w_prev == pytest.approx(0.5)
    assert aaa.w_cf == pytest.approx(0.55 / 1.05)
    assert aaa.drift == pytest.approx(0.55 / 1.05 - 0.5)
    assert aaa.trade == pytest.approx(0.5 - 0.55 / 1.05)
    assert_sums_to_zero(result)
    assert result.prev_as_of is None and result.to_dict()["prev_as_of"] is None


# --------------------------------------------------------------------------
# Result helpers, serialization, guards
# --------------------------------------------------------------------------


def test_sorted_helper_and_to_dict_json() -> None:
    prev, curr = messy_pair()
    result = decompose(prev, curr)

    by_trade = result.sorted(by="trade")
    assert [c.trade for c in by_trade] == sorted((c.trade for c in result.changes), reverse=True)
    by_abs = result.sorted(by="trade", absolute=True)
    assert abs(by_abs[0].trade) == max(abs(c.trade) for c in result.changes)
    asc = result.sorted(by="w_curr", reverse=False)
    assert asc[0].ticker == "EEE"  # exited -> weight 0 first
    # None values (entries have no rel_share_change) sort last.
    by_rel = result.sorted(by="rel_share_change")
    assert by_rel[-1].rel_share_change is None
    eq_only = result.sorted(by="w_curr", equities_only=True)
    assert all(c.asset_class == EQUITY for c in eq_only)
    with pytest.raises(AttributeError):
        result.sorted(by="nonexistent")

    payload = result.to_dict()
    text = json.dumps(payload)  # must be JSON serializable
    back = json.loads(text)
    assert back["prev_as_of"] == "2026-10-07" and back["curr_as_of"] == "2026-10-08"
    assert back["scale_factor"] == pytest.approx(1.05)
    assert {c["ticker"] for c in back["changes"]} == set(result.by_ticker())
    assert back["summary"]["gross_trade"] == pytest.approx(result.summary["gross_trade"])
    assert back["changes"][0]["total_change"] == pytest.approx(result.changes[0].total_change)
    # Default ordering: largest current weight first.
    assert result.changes[0].w_curr == max(c.w_curr for c in result.changes)


def test_guards() -> None:
    prev = snap(D0, BASE)
    with pytest.raises(ValueError, match="different ETFs"):
        decompose(prev, snap(D1, BASE, etf="QQQ"))
    with pytest.raises(ValueError, match="swap"):
        decompose(snap(D1, BASE), snap(D0, BASE))
    same_day = decompose(snap(D0, BASE), snap(D0, BASE))
    assert any("same as_of" in w for w in same_day.warnings)
    with pytest.raises(ValueError, match="normalize"):
        decompose(prev, Snapshot("SOXX", D1, holdings=[]))


def test_no_common_equities_sets_scale_factor_one() -> None:
    prev = snap(D0, {"AAA": (10.0, 10.0)})
    curr = snap(D1, {"BBB": (10.0, 10.0)})
    result = decompose(prev, curr)
    assert result.scale_factor == 1.0
    assert any("scale_factor set to 1.0" in w for w in result.warnings)
    assert result.entries == ["BBB"] and result.exits == ["AAA"]


def test_decompose_latest_from_disk(tmp_path) -> None:
    assert decompose_latest(tmp_path, "SOXX") is None
    prev = snap(D0, BASE)
    save_snapshot(prev, snapshot_path(tmp_path, "SOXX", D0))
    assert decompose_latest(tmp_path, "SOXX") is None
    curr = snap(D1, with_changes(BASE, CCC=(1600.0, 5.0)))
    save_snapshot(curr, snapshot_path(tmp_path, "SOXX", D1))
    result = decompose_latest(tmp_path, "SOXX")
    assert result is not None
    assert (result.prev_as_of, result.curr_as_of) == (D0, D1)
    assert result.get("CCC").classification == CLASS_ACTIVE_TRADE


def test_ticker_change_total_change() -> None:
    c = TickerChange("X", "X", 0.1, 0.12, 0.11, 0.02, -0.01, 1.0, 1.0, 1.0, 0.0, 1.0, 1.2, 0.2, CLASS_FLOW_ONLY)
    assert c.total_change == pytest.approx(0.01)
    assert c.to_dict()["total_change"] == pytest.approx(0.01)


# --------------------------------------------------------------------------
# Adversarial review: scale factor robustness, split false positives,
# unpriced lines, JSON, imports
# --------------------------------------------------------------------------


def test_stdlib_only_imports() -> None:
    """decompose.py must not import pandas/numpy (or anything outside the stdlib)."""
    import ast
    import sys
    from pathlib import Path

    tree = ast.parse(Path(dc.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not imported & {"pandas", "numpy", "requests"}
    assert all(name in sys.stdlib_module_names or name == "etf_tracker" for name in imported), imported
    # Module independence: decompose may import holdings only.
    package_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("etf_tracker")
    }
    assert package_imports == {"etf_tracker.holdings"}


def test_scale_factor_ignores_entries_exits_zero_and_missing_shares() -> None:
    # 5% creation on the four names that are comparable; everything else must
    # be excluded from the median: an exit, an entry (huge), a zero->positive
    # line, a positive->zero line and a name without a share count.
    prev = snap(D0, with_changes(BASE, ZZZ=(0.0, 5.0), YYY=(100.0, 5.0)))
    prev.holdings.append(Holding("SOXX", D0, "NNN", "NNN Inc", EQUITY, None, 10.0, 5_000.0, None))
    k = 1.05
    curr_positions = {t: (s * k, p) for t, (s, p) in BASE.items() if t != "EEE"}  # EEE exits
    curr_positions.update({"FFF": (99_999.0, 1.0), "ZZZ": (500.0, 5.0), "YYY": (0.0, 5.0)})
    curr = snap(D1, curr_positions)
    curr.holdings.append(Holding("SOXX", D1, "NNN", "NNN Inc", EQUITY, None, 10.0, 5_000.0, None))
    assert dc.estimate_scale_factor(prev, curr) == pytest.approx(k)
    result = decompose(prev, curr)
    assert result.scale_factor == pytest.approx(k)
    assert result.get("ZZZ").classification == CLASS_ENTRY
    assert result.get("YYY").classification == CLASS_EXIT
    assert result.get("FFF").classification == CLASS_ENTRY
    assert result.get("EEE").classification == CLASS_EXIT
    assert result.get("NNN").rel_share_change is None
    for t in ("AAA", "BBB", "CCC", "DDD"):
        assert result.get(t).classification == CLASS_FLOW_ONLY
        assert result.get(t).rel_share_change == pytest.approx(0.0, abs=TOL)
    assert_sums_to_zero(result)


def test_scale_factor_is_an_observed_ratio_with_even_count() -> None:
    # Four comparable names (even count), one 2:1 split: the median must stay
    # at the flow ratio of the three untouched names and the split must be
    # recognized.
    four = {t: BASE[t] for t in ("AAA", "BBB", "CCC", "DDD")}
    result = decompose(snap(D0, four), snap(D1, with_changes(four, DDD=(200.0, 50.0))))
    assert result.scale_factor == 1.0
    assert result.get("DDD").classification == CLASS_CORPORATE_ACTION
    assert result.summary["n_active_trades"] == 0

    # Two of four names bought 10%: k must be an observed ratio (1.0), so the
    # two untouched names are flow-only instead of all four being flagged
    # with a fictitious +-4.8%.
    result = decompose(snap(D0, four), snap(D1, with_changes(four, CCC=(110.0 * 2000 / 100, 5.0), DDD=(110.0, 100.0))))
    assert result.scale_factor == 1.0
    assert result.get("AAA").classification == CLASS_FLOW_ONLY
    assert result.get("BBB").classification == CLASS_FLOW_ONLY
    assert result.get("CCC").classification == CLASS_ACTIVE_TRADE
    assert result.get("DDD").rel_share_change == pytest.approx(0.1)

    # Degenerate two-name fund with one split: still detected.
    two_prev = snap(D0, {"AAA": (1000.0, 10.0), "DDD": (100.0, 100.0)})
    two_curr = snap(D1, {"AAA": (1000.0, 10.0), "DDD": (200.0, 50.0)})
    result = decompose(two_prev, two_curr)
    assert result.scale_factor == 1.0
    assert result.get("DDD").classification == CLASS_CORPORATE_ACTION
    assert result.get("AAA").classification == CLASS_FLOW_ONLY


def test_scale_factor_median_robust_to_integer_rounding_noise() -> None:
    # Share counts are published as integers: ratios differ in the 6th digit.
    prev_positions = {f"T{i:02d}": (float(1_000 + 37 * i), 10.0 + i) for i in range(30)}  # even count (SOXX)
    curr_positions = {t: (float(round(s * 1.0123)), p) for t, (s, p) in prev_positions.items()}
    result = decompose(snap(D0, prev_positions), snap(D1, curr_positions))
    assert result.scale_factor == pytest.approx(1.0123, abs=5e-4)
    assert all(c.classification == CLASS_FLOW_ONLY for c in result.changes)
    assert result.summary["gross_trade"] < 1e-3


def test_split_detection_requires_inverse_price_move() -> None:
    prev = snap(D0, BASE)
    # Price doubled on news, shares unchanged: drift, not a split.
    news = decompose(prev, snap(D1, with_changes(BASE, AAA=(1000.0, 20.0))))
    assert news.get("AAA").classification == CLASS_FLOW_ONLY
    assert news.get("AAA").split_ratio is None
    assert news.get("AAA").price_return == pytest.approx(1.0)
    assert news.get("AAA").drift > 0 and abs(news.get("AAA").trade) < TOL
    assert news.summary["n_corporate_action_suspects"] == 0

    # Shares halved, price unchanged: a sale.
    sold = decompose(prev, snap(D1, with_changes(BASE, AAA=(500.0, 10.0))))
    assert sold.get("AAA").classification == CLASS_ACTIVE_TRADE
    assert sold.get("AAA").rel_share_change == pytest.approx(-0.5)

    # Shares doubled AND price doubled (bought into a rally): a trade.
    rally = decompose(prev, snap(D1, with_changes(BASE, AAA=(2000.0, 20.0))))
    assert rally.get("AAA").classification == CLASS_ACTIVE_TRADE
    assert rally.get("AAA").split_ratio is None

    # Shares halved AND price doubled: 1:2 reverse split.
    reverse = decompose(prev, snap(D1, with_changes(BASE, AAA=(500.0, 20.0))))
    assert reverse.get("AAA").classification == CLASS_CORPORATE_ACTION
    assert reverse.get("AAA").split_ratio == pytest.approx(0.5)
    assert abs(reverse.get("AAA").trade) < TOL
    assert reverse.get("AAA").price_return == pytest.approx(0.0, abs=TOL)

    # A price move far outside the tolerance alongside a 2x share count is a trade.
    not_split = decompose(prev, snap(D1, with_changes(BASE, AAA=(2000.0, 5.6))))
    assert not_split.get("AAA").classification == CLASS_ACTIVE_TRADE
    # Within tolerance (split plus a 2% market move) it is a split.
    split = decompose(prev, snap(D1, with_changes(BASE, AAA=(2000.0, 5.1))))
    assert split.get("AAA").classification == CLASS_CORPORATE_ACTION
    assert split.get("AAA").price_return == pytest.approx(0.02)


def test_several_splits_with_flow_are_all_detected() -> None:
    positions = {**BASE, "FFF": (300.0, 50.0), "GGG": (400.0, 20.0)}
    k = 1.1
    curr = {t: (s * k, p) for t, (s, p) in positions.items()}
    curr["AAA"] = (1000.0 * k * 2, 5.0)
    curr["BBB"] = (500.0 * k * 3, 40.0 / 3)
    curr["CCC"] = (2000.0 * k * 0.5, 10.0)
    result = decompose(snap(D0, positions), snap(D1, curr))
    assert result.scale_factor == pytest.approx(k)
    assert result.get("AAA").split_ratio == 2.0
    assert result.get("BBB").split_ratio == 3.0
    assert result.get("CCC").split_ratio == 0.5
    assert result.summary["n_corporate_action_suspects"] == 3
    assert result.summary["n_active_trades"] == 0
    for c in result.changes:
        assert abs(c.trade) < 1e-9
    assert_sums_to_zero(result)


def test_held_name_without_current_price_is_not_reported_as_exit() -> None:
    # A halted stock: the file still lists the shares but no price and no
    # market value.  It must not show up as a full exit (a false forced-sale
    # signal); the previous price is carried forward and flagged.
    prev = snap(D0, BASE)
    curr = snap(D1, BASE)
    curr.holdings[0] = Holding("SOXX", D1, "AAA", "AAA Inc", EQUITY, 1000.0, None, None, None)
    result = decompose(prev, curr)
    aaa = result.get("AAA")
    assert aaa.classification == CLASS_FLOW_ONLY
    assert aaa.w_curr == pytest.approx(10_000 / 70_000, abs=TOL)
    assert aaa.price_curr == 10.0
    assert dc.FLAG_PRICE_MISSING in aaa.flags
    assert result.exits == []
    assert any("AAA" in w and "previous price" in w for w in result.warnings)
    assert_sums_to_zero(result)


def test_sums_hold_with_every_kind_of_messy_line_at_once() -> None:
    prev = snap(D0, BASE, cash=3_000.0)
    prev.holdings.append(Holding("SOXX", D0, "USD", "US Dollar", CASH, None, None, -500.0, None))  # payable
    prev.holdings.append(Holding("SOXX", D0, "MVO", "MV only", EQUITY, None, 20.0, 4_000.0, None))
    k = 1.05
    curr_positions = {
        "AAA": (1000.0 * k, 10.5),
        "BBB": (500.0 * k, 39.0),
        "CCC": (2000.0 * k * 0.9, 5.2),
        "DDD": (100.0 * k * 2, 50.5),  # split + flow
        "FFF": (300.0, 50.0),  # entry; EEE exits
    }
    curr = snap(D1, curr_positions, cash=5_000.0)
    curr.holdings.append(Holding("SOXX", D1, "MVO", "MV only", EQUITY, None, 21.0, 4_200.0 * k, None))
    result = decompose(prev, curr)
    assert result.scale_factor == pytest.approx(k)
    assert_sums_to_zero(result)
    assert result.get("DDD").classification == CLASS_CORPORATE_ACTION
    assert result.get("CCC").classification == CLASS_ACTIVE_TRADE
    assert result.get("MVO").rel_share_change is None
    assert result.get("MVO").price_return == pytest.approx(0.05)
    assert result.get("USD").shares_prev == pytest.approx(2_500.0)
    assert result.entries == ["FFF"] and result.exits == ["EEE"]
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["changes"] and all("ticker" in c for c in payload["changes"])


def test_duplicate_merge_does_not_fabricate_a_price() -> None:
    # Two lines for the same ticker, one with only a market value: the merged
    # price must not become total_mv / partial_shares.
    prev = snap(D0, BASE)
    prev.holdings.append(Holding("SOXX", D0, "AAA", "AAA Inc", EQUITY, None, None, 5_000.0, None))
    curr = snap(D1, with_changes(BASE, AAA=(1500.0, 10.0)))
    result = decompose(prev, curr)
    aaa = result.get("AAA")
    # Before the fix the merged line got price 15 = 15,000 / 1,000 and the
    # 1.5x ratio against the real price of 10 was then mistaken for a 3:2 split.
    assert aaa.classification == CLASS_FLOW_ONLY
    assert aaa.split_ratio is None
    assert dc.FLAG_SHARES_MISSING in aaa.flags
    assert result.scale_factor == 1.0
    assert aaa.price_prev == pytest.approx(10.0)
    assert aaa.w_prev == pytest.approx(15_000 / 75_000, abs=TOL)
    assert aaa.w_curr == pytest.approx(15_000 / 75_000, abs=TOL)
    assert abs(aaa.trade) < TOL and abs(aaa.drift) < TOL
    assert_sums_to_zero(result)


def test_mode_b_equivalence_with_even_universe_and_whitespace_keys() -> None:
    four = {t: BASE[t] for t in ("AAA", "BBB", "CCC", "DDD")}
    prev = snap(D0, four, cash=2_000.0)
    curr = snap(D1, {"AAA": (1000.0, 10.4), "BBB": (500.0, 41.0), "CCC": (2200.0, 4.9), "DDD": (90.0, 102.0)}, cash=2_100.0)
    mode_a = decompose(prev, curr)
    returns = {f" {t} ": curr.by_ticker()[t].price / prev.by_ticker()[t].price - 1.0 for t in four}
    returns["USD"] = 0.0
    mode_b = decompose_from_weights(
        prev.normalized_weights(), curr.normalized_weights(), returns, asset_classes={"USD": CASH}
    )
    assert not any("no return" in w for w in mode_b.warnings)
    for ticker, a in mode_a.by_ticker().items():
        b = mode_b.get(ticker)
        assert b.w_cf == pytest.approx(a.w_cf, abs=TOL)
        assert b.drift == pytest.approx(a.drift, abs=TOL)
        assert b.trade == pytest.approx(a.trade, abs=TOL)
        assert b.classification == a.classification
        if a.rel_share_change is not None:
            assert b.rel_share_change == pytest.approx(a.rel_share_change, abs=1e-9)
    # A ticker appearing twice modulo whitespace is aggregated, not overwritten.
    agg = decompose_from_weights({"AAA": 0.25, "AAA ": 0.25, "BBB": 0.5}, {"AAA": 0.5, "BBB": 0.5}, {})
    assert agg.get("AAA").w_prev == pytest.approx(0.5)


def test_result_json_handles_none_and_non_finite_values() -> None:
    prev, curr = messy_pair()
    result = decompose(prev, curr)
    payload = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    eee = next(c for c in payload["changes"] if c["ticker"] == "EEE")
    assert eee["shares_curr"] is None and eee["rel_share_change"] is None
    fff = next(c for c in payload["changes"] if c["ticker"] == "FFF")
    assert fff["price_return"] is None and fff["split_ratio"] is None
    assert payload["prev_as_of"] == "2026-10-07"
    # Non-finite floats never leak into the JSON (they are not valid JSON).
    bad = TickerChange("X", "X", 0.1, 0.1, 0.1, 0.0, 0.0, None, None, None, float("inf"), None, None, float("nan"), CLASS_FLOW_ONLY)
    text = json.dumps(bad.to_dict(), allow_nan=False)
    assert json.loads(text)["rel_share_change"] is None and json.loads(text)["price_return"] is None
    assert json.loads(json.dumps(decompose_from_weights({"A": 1.0}, {"A": 1.0}, {}).to_dict()))["curr_as_of"] is None


def test_sort_changes_rejects_unknown_attribute_even_when_empty() -> None:
    with pytest.raises(AttributeError):
        dc.sort_changes([], by="bogus")
    assert dc.sort_changes([], by="trade") == []


def test_held_name_without_previous_price_is_not_reported_as_entry() -> None:
    # Mirror image of the halted-stock case: yesterday's file had the shares
    # but no price/market value.  Today's price is carried back so the name is
    # not a spurious entry (and tomorrow not a spurious exit).
    prev = snap(D0, BASE)
    prev.holdings[0] = Holding("SOXX", D0, "AAA", "AAA Inc", EQUITY, 1000.0, None, None, None)
    curr = snap(D1, with_changes(BASE, AAA=(1000.0, 12.0)))
    result = decompose(prev, curr)
    aaa = result.get("AAA")
    assert aaa.classification == CLASS_FLOW_ONLY
    assert aaa.price_prev == 12.0 and aaa.price_return == pytest.approx(0.0)
    assert aaa.w_prev == pytest.approx(12_000 / 72_000, abs=TOL)
    assert dc.FLAG_PRICE_CARRIED in aaa.flags and dc.FLAG_PRICE_MISSING in aaa.flags
    assert aaa.flags.count(dc.FLAG_PRICE_MISSING) == 1
    assert result.entries == [] and result.exits == []
    assert result.scale_factor == 1.0
    assert_sums_to_zero(result)
    # A genuinely unpriced line (no shares either) is still treated as absent.
    prev.holdings[0] = Holding("SOXX", D0, "AAA", "AAA Inc", EQUITY, None, None, None, None)
    unpriced = decompose(prev, curr)
    assert unpriced.get("AAA").classification == CLASS_ENTRY
    assert dc.FLAG_UNPRICED in unpriced.get("AAA").flags
