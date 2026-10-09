"""Tests for etf_tracker.bridge: Snapshot -> Constituent adapters."""

from __future__ import annotations

from datetime import date

import pytest

from etf_tracker.bridge import (
    COMPANY_LEVEL_INDICES,
    apply_caps_to_snapshot,
    constituents_from_snapshot,
    resolve_index_id,
)
from etf_tracker.holdings import CASH, EQUITY, Holding, Snapshot
from etf_tracker.rules import Constituent, aggregate_by_company, rules_for

AS_OF = date(2026, 10, 9)


def _h(
    etf: str,
    ticker: str,
    shares: float | None = None,
    price: float | None = None,
    *,
    weight: float | None = None,
    market_value: float | None = None,
    asset_class: str = EQUITY,
    name: str | None = None,
    is_adr: bool | None = None,
    company_id: str | None = None,
    as_of: date = AS_OF,
) -> Holding:
    return Holding(
        etf=etf,
        as_of=as_of,
        ticker=ticker,
        name=name or f"{ticker} Inc",
        asset_class=asset_class,
        shares=shares,
        price=price,
        market_value=market_value,
        weight=weight,
        is_adr=is_adr,
        company_id=company_id,
    )


def _snapshot(etf: str, holdings: list[Holding], as_of: date = AS_OF) -> Snapshot:
    return Snapshot(etf=etf, as_of=as_of, source="test", holdings=holdings)


def _weights(constituents: list[Constituent]) -> dict[str, float]:
    return {c.ticker: c.weight for c in constituents}


# ---------------------------------------------------------------------------
# resolve_index_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given, expected", [("QQQ", "QQQ"), ("qqq", "QQQ"), ("Soxx", "SOXX"), ("igv", "IGV")])
def test_resolve_index_id_accepts_etf_tickers_case_insensitively(given, expected):
    assert resolve_index_id(given) == expected


def test_resolve_index_id_unknown_raises_key_error():
    with pytest.raises(KeyError):
        resolve_index_id("SPY")


def test_company_level_indices_are_the_two_company_capped_methodologies():
    assert COMPANY_LEVEL_INDICES == frozenset({"QQQ", "IGV"})


# ---------------------------------------------------------------------------
# constituents_from_snapshot: weights
# ---------------------------------------------------------------------------


def test_cash_is_excluded_and_equity_weights_renormalise_to_one():
    snap = _snapshot(
        "SOXX",
        [
            _h("SOXX", "AAA", shares=100, price=10.0),  # 1000
            _h("SOXX", "BBB", shares=100, price=30.0),  # 3000
            _h("SOXX", "USD", shares=None, price=None, market_value=1000.0, asset_class=CASH),
        ],
    )
    cons = constituents_from_snapshot(snap, "SOXX")
    w = _weights(cons)
    assert set(w) == {"AAA", "BBB"}
    assert w["AAA"] == pytest.approx(0.25)
    assert w["BBB"] == pytest.approx(0.75)
    assert sum(w.values()) == pytest.approx(1.0)
    # The fund-level weights would have been 0.20 / 0.60 / 0.20; the cash
    # line must not leak into the constituent weights.
    assert snap.normalized_weights(include_cash=True)["AAA"] == pytest.approx(0.20)


def test_weights_are_fractions_not_percents():
    snap = _snapshot("QQQ", [_h("QQQ", t, shares=1, price=p) for t, p in [("A", 60.0), ("B", 40.0)]])
    cons = constituents_from_snapshot(snap)
    assert all(0.0 < c.weight <= 1.0 for c in cons)
    assert _weights(cons) == pytest.approx({"A": 0.6, "B": 0.4})


def test_published_weight_fallback_when_market_values_are_missing():
    # No shares/prices anywhere: the published weights are the only source.
    snap = _snapshot(
        "IGV",
        [
            _h("IGV", "AAA", weight=0.30),
            _h("IGV", "BBB", weight=0.60),
            _h("IGV", "USD", weight=0.10, asset_class=CASH),
        ],
    )
    w = _weights(constituents_from_snapshot(snap, "IGV"))
    assert w == pytest.approx({"AAA": 1 / 3, "BBB": 2 / 3})


def test_duplicate_ticker_lines_are_aggregated_and_first_name_wins():
    snap = _snapshot(
        "QQQ",
        [
            _h("QQQ", "AAA", shares=100, price=10.0, name="First line"),
            _h("QQQ", "BBB", shares=100, price=10.0),
            _h("QQQ", "AAA", shares=100, price=10.0, name="Second line"),
        ],
    )
    cons = constituents_from_snapshot(snap)
    assert [c.ticker for c in cons] == ["AAA", "BBB"]  # file order, one per ticker
    assert cons[0].name == "First line"
    assert _weights(cons) == pytest.approx({"AAA": 2 / 3, "BBB": 1 / 3})


def test_zero_and_negative_weight_lines_are_dropped_and_rest_renormalised():
    snap = _snapshot(
        "SOXX",
        [
            _h("SOXX", "AAA", shares=100, price=10.0),
            _h("SOXX", "ZERO", shares=0, price=10.0),
            _h("SOXX", "SHORT", shares=-10, price=10.0),
            _h("SOXX", "BBB", shares=100, price=10.0),
        ],
    )
    cons = constituents_from_snapshot(snap, "SOXX")
    assert {c.ticker for c in cons} == {"AAA", "BBB"}
    assert sum(c.weight for c in cons) == pytest.approx(1.0)
    assert all(c.weight > 0 for c in cons)


def test_no_usable_equity_lines_returns_empty_list():
    assert constituents_from_snapshot(_snapshot("QQQ", []), "QQQ") == []
    cash_only = _snapshot("QQQ", [_h("QQQ", "USD", market_value=5.0, asset_class=CASH)])
    assert constituents_from_snapshot(cash_only, "QQQ") == []


def test_index_id_defaults_to_snapshot_etf_and_unknown_raises():
    snap = _snapshot("QQQ", [_h("QQQ", "AAA", shares=1, price=1.0)])
    assert constituents_from_snapshot(snap)[0].ticker == "AAA"
    with pytest.raises(KeyError):
        constituents_from_snapshot(snap, "SPY")
    with pytest.raises(KeyError):
        constituents_from_snapshot(_snapshot("VGT", [_h("VGT", "AAA", shares=1, price=1.0)]))


# ---------------------------------------------------------------------------
# constituents_from_snapshot: company ids (share classes)
# ---------------------------------------------------------------------------


def test_qqq_share_classes_share_a_company_id():
    snap = _snapshot(
        "QQQ",
        [
            _h("QQQ", "GOOGL", shares=100, price=10.0),
            _h("QQQ", "GOOG", shares=100, price=10.0),
            _h("QQQ", "AAPL", shares=200, price=10.0),
        ],
    )
    cons = constituents_from_snapshot(snap, "QQQ")
    by = {c.ticker: c for c in cons}
    assert by["GOOGL"].company_id == by["GOOG"].company_id == "ALPHABET"
    assert by["AAPL"].company_id == "AAPL"
    assert aggregate_by_company(cons) == pytest.approx({"ALPHABET": 0.5, "AAPL": 0.5})


def test_igv_is_company_level_too():
    snap = _snapshot("IGV", [_h("IGV", "GOOGL", shares=1, price=1.0), _h("IGV", "GOOG", shares=1, price=1.0)])
    cons = constituents_from_snapshot(snap, "IGV")
    assert {c.company_id for c in cons} == {"ALPHABET"}


def test_soxx_keeps_company_id_per_security():
    # Hypothetical: were a dual-class name in SOXX, the per-security caps of the
    # NYSE Semiconductor Index would not aggregate it.
    snap = _snapshot("SOXX", [_h("SOXX", "GOOGL", shares=1, price=1.0), _h("SOXX", "GOOG", shares=1, price=1.0)])
    cons = constituents_from_snapshot(snap, "SOXX")
    assert {c.company_id for c in cons} == {"GOOGL", "GOOG"}


def test_explicit_holding_company_id_wins_for_every_index():
    for etf in ("SOXX", "QQQ", "IGV"):
        snap = _snapshot(
            etf,
            [
                _h(etf, "GOOGL", shares=1, price=1.0, company_id="X"),
                _h(etf, "NEWA", shares=1, price=1.0, company_id="NEW"),
                _h(etf, "NEWB", shares=1, price=1.0, company_id="NEW"),
            ],
        )
        by = {c.ticker: c.company_id for c in constituents_from_snapshot(snap, etf)}
        assert by == {"GOOGL": "X", "NEWA": "NEW", "NEWB": "NEW"}, etf


# ---------------------------------------------------------------------------
# constituents_from_snapshot: ADR flag
# ---------------------------------------------------------------------------


def test_adr_flag_copied_when_known_and_false_when_unknown():
    snap = _snapshot(
        "SOXX",
        [
            _h("SOXX", "TSM", shares=1, price=1.0, is_adr=True),
            _h("SOXX", "NVDA", shares=1, price=1.0, is_adr=False),
            _h("SOXX", "AMD", shares=1, price=1.0, is_adr=None),
        ],
    )
    by = {c.ticker: c.is_adr for c in constituents_from_snapshot(snap, "SOXX")}
    assert by == {"TSM": True, "NVDA": False, "AMD": False}


def test_adr_tickers_override_marks_adrs_case_insensitively():
    snap = _snapshot(
        "SOXX",
        [
            _h("SOXX", "TSM", shares=1, price=1.0),
            _h("SOXX", "ASML", shares=1, price=1.0, is_adr=False),  # override wins over False
            _h("SOXX", "NVDA", shares=1, price=1.0),
        ],
    )
    by = {c.ticker: c.is_adr for c in constituents_from_snapshot(snap, "SOXX", adr_tickers=["tsm", " asml "])}
    assert by == {"TSM": True, "ASML": True, "NVDA": False}


def test_adr_flags_reach_the_soxx_adr_metric():
    weights = {"TSM": 300, "ASML": 200, "NVDA": 500}
    snap = _snapshot("SOXX", [_h("SOXX", t, shares=s, price=1.0) for t, s in weights.items()])
    cons = constituents_from_snapshot(snap, "SOXX", adr_tickers=("TSM", "ASML"))
    metrics = rules_for("SOXX").compute_metrics(cons)
    assert metrics["adr_sum"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# apply_caps_to_snapshot
# ---------------------------------------------------------------------------


def _qqq_snapshot_with_mega_cap(as_of: date = AS_OF) -> Snapshot:
    holdings = [_h("QQQ", "MEGA", shares=300, price=1.0, as_of=as_of)]
    holdings += [_h("QQQ", f"S{i:02d}", shares=35, price=1.0, as_of=as_of) for i in range(20)]  # 20 x 3.5%
    holdings.append(_h("QQQ", "USD", market_value=50.0, asset_class=CASH, as_of=as_of))
    return _snapshot("QQQ", holdings, as_of=as_of)


def test_apply_caps_to_snapshot_caps_a_30pct_company_at_20pct():
    result = apply_caps_to_snapshot(_qqq_snapshot_with_mega_cap(), "QQQ", "quarterly_rebalance")
    assert result.index_id == "QQQ"
    assert result.event_type == "quarterly_rebalance"
    assert result.as_of == AS_OF
    assert result.current_weights["MEGA"] == pytest.approx(0.30)  # cash excluded
    assert result.target_weights["MEGA"] == pytest.approx(0.20)
    assert sum(result.target_weights.values()) == pytest.approx(1.0)
    assert result.forced_sellers == ["MEGA"]
    assert result.deltas["MEGA"] == pytest.approx(-0.10)
    assert all(d > 0 for t, d in result.deltas.items() if t != "MEGA")
    assert "USD" not in result.current_weights


def test_apply_caps_to_snapshot_picks_event_type_from_calendar():
    # Next Nasdaq-100 event after 2026-10-09 is the December annual reconstitution.
    res_dec = apply_caps_to_snapshot(_qqq_snapshot_with_mega_cap(date(2026, 10, 9)), "QQQ")
    assert res_dec.event_type == "annual_reconstitution"
    # After the December reconstitution the next event is the March quarterly rebalance.
    res_mar = apply_caps_to_snapshot(_qqq_snapshot_with_mega_cap(date(2027, 1, 5)), "QQQ")
    assert res_mar.event_type == "quarterly_rebalance"
    # as_of override changes the event type even for the same snapshot.
    res_override = apply_caps_to_snapshot(_qqq_snapshot_with_mega_cap(date(2026, 10, 9)), as_of=date(2027, 1, 5))
    assert res_override.event_type == "quarterly_rebalance"
    assert res_override.as_of == date(2027, 1, 5)


def test_apply_caps_to_snapshot_defaults_index_to_snapshot_etf():
    result = apply_caps_to_snapshot(_qqq_snapshot_with_mega_cap())
    assert result.index_id == "QQQ"


def test_apply_caps_to_snapshot_without_equities_raises():
    with pytest.raises(ValueError):
        apply_caps_to_snapshot(_snapshot("SOXX", [_h("SOXX", "USD", market_value=1.0, asset_class=CASH)]), "SOXX")


def test_apply_caps_to_snapshot_soxx_with_adr_override_is_feasible_and_sums_to_one():
    # 30 names with a descending profile that breaches the 8% single cap.
    raw = [120, 110, 100, 90, 80] + [20] * 25  # sum 1000; top names 12%, 11%, 10%, 9%, 8%
    tickers = [f"C{i:02d}" for i in range(30)]
    snap = _snapshot("SOXX", [_h("SOXX", t, shares=s, price=1.0) for t, s in zip(tickers, raw)])
    result = apply_caps_to_snapshot(snap, "SOXX", "quarterly_rebalance", adr_tickers=["C00"])
    assert result.feasible
    assert sum(result.target_weights.values()) == pytest.approx(1.0)
    assert max(result.target_weights.values()) <= 0.08 + 1e-9
    assert result.metrics["adr_sum"] == pytest.approx(0.12)
    assert set(result.forced_sellers) >= {"C00", "C01", "C02", "C03"}
