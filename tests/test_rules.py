"""Tests for etf_tracker.rules: capping rules and rebalance schedules."""

from __future__ import annotations

from datetime import date

import pytest

from etf_tracker.rules import (
    EVENT_KINDS,
    INDEX_FOR_ETF,
    SHARE_CLASS_GROUPS,
    Constituent,
    IGVRules,
    QQQRules,
    RebalanceEvent,
    SOXXRules,
    rules_for,
)

try:  # the calendar module is owned by another part of the project
    import etf_tracker.market_calendar  # noqa: F401

    HAVE_CALENDAR = True
except ImportError:  # pragma: no cover - depends on the checkout
    HAVE_CALENDAR = False

needs_calendar = pytest.mark.skipif(
    not HAVE_CALENDAR, reason="etf_tracker.market_calendar is not available yet"
)

AS_OF = date(2026, 10, 9)
TOL = 1e-9


def filler(prefix: str, n: int, each: float, **kw) -> list[Constituent]:
    """``n`` small names of equal weight."""
    return [Constituent(f"{prefix}{i:02d}", each, **kw) for i in range(n)]


def assert_sums_to_one(result) -> None:
    assert result.feasible, result.notes
    assert abs(sum(result.target_weights.values()) - 1.0) < TOL


def assert_rank_preserved(current: dict[str, float], target: dict[str, float]) -> None:
    """A name that was larger initially must not end up smaller than a name it beat."""
    for a in current:
        for b in current:
            if current[a] > current[b] + 1e-12:
                assert target[a] >= target[b] - 1e-12, (a, b, target[a], target[b])


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


def test_company_id_defaults_and_share_class_groups():
    assert Constituent("NVDA", 0.1).company_id == "NVDA"
    assert Constituent("GOOGL", 0.1).company_id == Constituent("GOOG", 0.1).company_id
    assert Constituent("googl", 0.1).company_id == SHARE_CLASS_GROUPS["GOOGL"]
    assert Constituent("GOOG", 0.1, company_id="X").company_id == "X"
    with pytest.raises(ValueError):
        Constituent("A", -0.1)


def test_rules_for_and_index_map():
    assert INDEX_FOR_ETF == {"SOXX": "SOXX", "QQQ": "QQQ", "IGV": "IGV"}
    assert isinstance(rules_for("SOXX"), SOXXRules)
    assert isinstance(rules_for("qqq"), QQQRules)
    assert isinstance(rules_for("IGV"), IGVRules)
    with pytest.raises(KeyError):
        rules_for("SPY")
    assert "quarterly_rebalance" in EVENT_KINDS


def test_duplicate_ticker_rejected():
    with pytest.raises(ValueError):
        SOXXRules().apply_caps([Constituent("A", 0.5), Constituent("A", 0.5)], AS_OF)


# ---------------------------------------------------------------------------
# SOXX capping
# ---------------------------------------------------------------------------


def soxx_two_over_8_one_over_4() -> list[Constituent]:
    # top-5 by uncapped weight: NVDA 12, AVGO 10, AMD 7, QCOM 6, TXN 5.5 (=40.5%)
    # MU 5% is outside the top-5 and above 4%; 24 names share the other 54.5%.
    return [
        Constituent("NVDA", 0.12),
        Constituent("AVGO", 0.10),
        Constituent("AMD", 0.07),
        Constituent("QCOM", 0.06),
        Constituent("TXN", 0.055),
        Constituent("MU", 0.05),
        *filler("S", 24, 0.545 / 24),
    ]


def test_soxx_single_name_and_outside_top5_caps():
    res = SOXXRules().apply_caps(soxx_two_over_8_one_over_4(), AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    # Capped names sit exactly on their caps.
    assert t["NVDA"] == pytest.approx(0.08, abs=TOL)
    assert t["AVGO"] == pytest.approx(0.08, abs=TOL)
    assert t["MU"] == pytest.approx(0.04, abs=TOL)
    # The freed 4% + 2% + 1% = 7% goes pro rata to the uncapped 73%: factor 80/73.
    factor = 0.80 / 0.73
    assert t["AMD"] == pytest.approx(0.07 * factor, abs=TOL)
    assert t["QCOM"] == pytest.approx(0.06 * factor, abs=TOL)
    assert t["TXN"] == pytest.approx(0.055 * factor, abs=TOL)
    assert t["S00"] == pytest.approx(0.545 / 24 * factor, abs=TOL)
    # Nothing newly breaches after redistribution.
    assert t["AMD"] < 0.08 and t["S00"] < 0.04
    assert any("8%" in b for b in res.binding_constraints)
    assert any("4%" in b and "MU" in b for b in res.binding_constraints)
    # Metrics describe the current (pre-cap) state.
    assert res.metrics["names_over_8"] == 2
    assert res.metrics["names_over_4_outside_top5"] == 1
    assert res.metrics["top5_sum"] == pytest.approx(0.405)
    assert res.metrics["adr_sum"] == 0.0
    assert res.target_metrics["names_over_8"] == 0
    assert res.metrics["turnover"] == pytest.approx((0.04 + 0.02 + 0.01), abs=1e-9)


def test_soxx_delta_sign_convention():
    res = SOXXRules().apply_caps(soxx_two_over_8_one_over_4(), AS_OF)
    assert res.deltas["NVDA"] == pytest.approx(-0.04, abs=TOL)  # cut: forced selling
    assert res.deltas["MU"] == pytest.approx(-0.01, abs=TOL)
    assert res.deltas["AMD"] > 0  # recipient: forced buying
    assert abs(sum(res.deltas.values())) < TOL
    assert res.sorted_deltas()[0][0] == "NVDA"
    assert set(res.forced_sellers) == {"NVDA", "AVGO", "MU"}
    assert "AMD" in res.forced_buyers


def test_soxx_adr_aggregate_cap():
    # Nothing breaches the single-name caps; ADRs sum to 13% > 10%.
    cons = [
        Constituent("NVDA", 0.075),
        Constituent("AVGO", 0.07),
        Constituent("AMD", 0.065),
        Constituent("ASML", 0.06, is_adr=True),
        Constituent("TSM", 0.05, is_adr=True),
        Constituent("ARM", 0.02, is_adr=True),
        *filler("S", 24, 0.66 / 24),
    ]
    res = SOXXRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    adr_scale = 0.10 / 0.13
    assert t["ASML"] == pytest.approx(0.06 * adr_scale, abs=TOL)
    assert t["TSM"] == pytest.approx(0.05 * adr_scale, abs=TOL)
    assert t["ARM"] == pytest.approx(0.02 * adr_scale, abs=TOL)
    assert t["ASML"] + t["TSM"] + t["ARM"] == pytest.approx(0.10, abs=TOL)
    # The freed 3% goes to the 87% of non-ADRs -> factor 90/87, still below caps.
    factor = 0.90 / 0.87
    assert t["NVDA"] == pytest.approx(0.075 * factor, abs=TOL)
    assert t["S00"] == pytest.approx(0.66 / 24 * factor, abs=TOL)
    assert t["NVDA"] < 0.08
    assert res.metrics["adr_sum"] == pytest.approx(0.13)
    assert res.target_metrics["adr_sum"] == pytest.approx(0.10, abs=TOL)
    assert any("ADR" in b for b in res.binding_constraints)
    assert res.deltas["ASML"] < 0 < res.deltas["NVDA"]


def test_soxx_adr_cap_interacts_with_single_caps():
    # ADR redistribution must not push a non-ADR through its 8% cap.
    cons = [
        Constituent("NVDA", 0.079),
        Constituent("AVGO", 0.05),
        Constituent("AMD", 0.05),
        Constituent("ASML", 0.08, is_adr=True),
        Constituent("TSM", 0.08, is_adr=True),
        *filler("S", 25, 0.661 / 25),
    ]
    res = SOXXRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    assert t["NVDA"] == pytest.approx(0.08, abs=TOL)
    assert t["ASML"] + t["TSM"] == pytest.approx(0.10, abs=TOL)
    assert all(w <= 0.08 + TOL for w in t.values())
    top5 = {"NVDA", "AVGO", "AMD", "ASML", "TSM"}
    assert all(t[k] <= 0.04 + TOL for k in t if k not in top5)


def test_soxx_unchanged_when_no_cap_binds():
    cons = [Constituent(f"A{i}", 1 / 30) for i in range(30)]
    res = SOXXRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    assert res.binding_constraints == []
    assert all(abs(d) < TOL for d in res.deltas.values())


def test_soxx_infeasible_caps_are_flagged_not_renormalised():
    cons = [Constituent(f"A{i}", 0.1) for i in range(10)]  # 5*8% + 5*4% = 60% < 100%
    res = SOXXRules().apply_caps(cons, AS_OF)
    assert res.feasible is False
    assert sum(res.target_weights.values()) == pytest.approx(0.60, abs=TOL)
    assert all(w <= 0.08 + TOL for w in res.target_weights.values())
    assert any("infeasible" in n for n in res.notes)


def test_soxx_input_normalisation_note():
    cons = [Constituent(f"A{i}", 3.0) for i in range(30)]  # percent-like, sums to 90
    res = SOXXRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    assert any("normalised" in n for n in res.notes)
    assert res.current_weights["A0"] == pytest.approx(1 / 30)


def test_soxx_check_constraints():
    breaches = SOXXRules().check_constraints(soxx_two_over_8_one_over_4())
    rules = {b.rule: b for b in breaches}
    assert set(rules) == {"soxx_single_cap_8", "soxx_outside_top5_cap_4"}
    assert rules["soxx_single_cap_8"].tickers == ("NVDA", "AVGO")
    assert rules["soxx_outside_top5_cap_4"].tickers == ("MU",)
    assert rules["soxx_single_cap_8"].value == pytest.approx(0.12)
    assert SOXXRules().check_constraints([Constituent(f"A{i}", 1 / 30) for i in range(30)]) == []


# ---------------------------------------------------------------------------
# QQQ capping
# ---------------------------------------------------------------------------


def qqq_cohort_52() -> list[Constituent]:
    # Companies above 4.5%: A 12, B 11, C 10, D 9, E 5, F 5 -> 52% (>= 48%).
    return [
        Constituent("A", 0.12),
        Constituent("B", 0.11),
        Constituent("C", 0.10),
        Constituent("D", 0.09),
        Constituent("E", 0.05),
        Constituent("F", 0.05),
        *filler("R", 24, 0.02),
    ]


def test_qqq_cohort_cut_to_40_preserves_rank_and_sums_to_one():
    res = QQQRules().apply_caps(qqq_cohort_52(), AS_OF, "quarterly_rebalance")
    assert_sums_to_one(res)
    t = res.target_weights
    factor = 0.40 / 0.52
    for k, w in (("A", 0.12), ("B", 0.11), ("C", 0.10), ("D", 0.09), ("E", 0.05), ("F", 0.05)):
        assert t[k] == pytest.approx(w * factor, abs=TOL)
    assert sum(t[k] for k in "ABCDEF") == pytest.approx(0.40, abs=TOL)
    # The remaining 60% is spread over the 24 small names (48% -> 60%, factor 1.25).
    assert t["R00"] == pytest.approx(0.025, abs=TOL)
    assert_rank_preserved(res.current_weights, t)
    assert res.metrics["sum_over_4_5"] == pytest.approx(0.52)
    assert res.metrics["max_company_weight"] == pytest.approx(0.12)
    assert res.target_metrics["sum_over_4_5"] < 0.48
    assert any("40%" in b for b in res.binding_constraints)
    assert res.deltas["A"] < 0 < res.deltas["R00"]


def test_qqq_outsider_below_4_5_is_adjusted_downward_to_keep_rank_order():
    # G (4.4%) is below 4.5% but would overtake E/F (5% * 40/52 = 3.846%) if it
    # were scaled up with the others, so it is cut to the smallest cohort weight.
    cons = [
        Constituent("A", 0.12),
        Constituent("B", 0.11),
        Constituent("C", 0.10),
        Constituent("D", 0.09),
        Constituent("E", 0.05),
        Constituent("F", 0.05),
        Constituent("G", 0.044),
        *filler("R", 20, 0.0218),
    ]
    res = QQQRules().apply_caps(cons, AS_OF, "quarterly_rebalance")
    assert_sums_to_one(res)
    t = res.target_weights
    smallest_cohort = 0.05 * 0.40 / 0.52
    assert t["G"] == pytest.approx(smallest_cohort, abs=TOL)
    assert res.deltas["G"] < 0  # downward adjustment of a name below 4.5%
    rest_factor = (0.60 - smallest_cohort) / (20 * 0.0218)
    assert t["R00"] == pytest.approx(0.0218 * rest_factor, abs=TOL)
    assert_rank_preserved(res.current_weights, t)


def test_qqq_unchanged_below_triggers():
    cons = [
        Constituent("A", 0.10),
        Constituent("B", 0.09),
        Constituent("C", 0.06),
        Constituent("D", 0.05),  # cohort sum 30% < 48%
        *filler("R", 35, 0.70 / 35),
    ]
    res = QQQRules().apply_caps(cons, AS_OF, "quarterly_rebalance")
    assert_sums_to_one(res)
    assert res.binding_constraints == []
    assert all(abs(d) < TOL for d in res.deltas.values())
    assert res.target_weights == pytest.approx(res.current_weights, abs=TOL)


def test_qqq_company_cap_aggregates_share_classes():
    # Alphabet = GOOGL 14% + GOOG 12% = 26% > 24% -> company capped at 20%,
    # split pro rata between the share classes; others scaled by 80/74.
    cons = [
        Constituent("GOOGL", 0.14),
        Constituent("GOOG", 0.12),
        Constituent("MSFT", 0.04),
        *filler("R", 35, 0.70 / 35),
    ]
    res = QQQRules().apply_caps(cons, AS_OF, "quarterly_rebalance")
    assert_sums_to_one(res)
    t = res.target_weights
    assert t["GOOGL"] + t["GOOG"] == pytest.approx(0.20, abs=TOL)
    assert t["GOOGL"] == pytest.approx(0.20 * 14 / 26, abs=TOL)
    assert t["GOOG"] == pytest.approx(0.20 * 12 / 26, abs=TOL)
    assert t["MSFT"] == pytest.approx(0.04 * 0.80 / 0.74, abs=TOL)
    assert res.metrics["max_company_weight"] == pytest.approx(0.26)
    assert res.metrics["max_security_weight"] == pytest.approx(0.14)
    assert any("20%" in b and "ALPHABET" in b for b in res.binding_constraints)


def test_qqq_company_between_20_and_24_is_not_capped():
    cons = [Constituent("A", 0.22), *filler("R", 39, 0.78 / 39)]
    res = QQQRules().apply_caps(cons, AS_OF, "quarterly_rebalance")
    assert_sums_to_one(res)
    assert res.target_weights["A"] == pytest.approx(0.22, abs=TOL)


def test_qqq_december_security_above_15_cut_to_14():
    cons = [
        Constituent("AAPL", 0.16),
        Constituent("MSFT", 0.07),
        Constituent("NVDA", 0.06),
        Constituent("AMZN", 0.05),  # cohort 34% < 48%: no company-level change
        *filler("R", 33, 0.02),
    ]
    rules = QQQRules()
    dec = rules.apply_caps(cons, AS_OF, "annual_reconstitution")
    assert_sums_to_one(dec)
    t = dec.target_weights
    assert t["AAPL"] == pytest.approx(0.14, abs=TOL)
    factor = 0.86 / 0.84
    assert t["MSFT"] == pytest.approx(0.07 * factor, abs=TOL)
    assert t["R00"] == pytest.approx(0.02 * factor, abs=TOL)
    # top-5 after stage 1 = 14 + 7.17 + 6.14 + 5.12 + 2.05 = 34.5% < 40%: no stage 2
    assert dec.target_metrics["top5_security_sum"] < 0.40
    assert any("14%" in b for b in dec.binding_constraints)
    # The security-level rules apply in December only.
    q = rules.apply_caps(cons, AS_OF, "quarterly_rebalance")
    assert q.target_weights["AAPL"] == pytest.approx(0.16, abs=TOL)
    assert q.binding_constraints == []


def test_qqq_december_top5_securities_cut_to_38_5():
    cons = [
        Constituent("A", 0.10),
        Constituent("B", 0.09),
        Constituent("C", 0.09),
        Constituent("D", 0.08),
        Constituent("E", 0.06),  # top-5 = 42% >= 40%; company cohort 42% < 48%
        Constituent("F", 0.045),
        *filler("R", 30, 0.535 / 30),
    ]
    res = QQQRules().apply_caps(cons, AS_OF, "annual_reconstitution")
    assert_sums_to_one(res)
    t = res.target_weights
    factor = 0.385 / 0.42
    assert sum(t[k] for k in "ABCDE") == pytest.approx(0.385, abs=TOL)
    assert t["E"] == pytest.approx(0.06 * factor, abs=TOL)
    # Outsiders capped at min(4.4%, fifth largest): F would go above the cap.
    fifth = 0.06 * factor
    assert t["F"] <= min(0.044, fifth) + TOL
    assert_rank_preserved(res.current_weights, t)
    assert res.metrics["top5_security_sum"] == pytest.approx(0.42)


def test_qqq_special_rebalance_trigger():
    rules = QQQRules()
    # Company above 24%.
    check = rules.special_rebalance_triggered([Constituent("A", 0.25), *filler("R", 25, 0.03)])
    assert check and check.triggered
    assert any("24%" in r for r in check.reasons)
    assert check.metrics["max_company_weight"] == pytest.approx(0.25)
    # Aggregate of companies above 4.5% exceeds 48% (49%).
    cons = [
        Constituent("A", 0.13),
        Constituent("B", 0.12),
        Constituent("C", 0.12),
        Constituent("D", 0.12),
        *filler("R", 17, 0.03),
    ]
    check = rules.special_rebalance_triggered(cons)
    assert check.triggered
    assert any("48%" in r for r in check.reasons)
    assert check.metrics["sum_over_4_5"] == pytest.approx(0.49)
    # Exactly 48% does not trigger a special rebalance (rule is "exceeds").
    cons = [Constituent(k, 0.12) for k in "ABCD"] + filler("R", 26, 0.02)
    check = rules.special_rebalance_triggered(cons)
    assert not check
    assert check.reasons == ()
    # Share classes are aggregated before testing the 24% trigger.
    cons = [Constituent("GOOGL", 0.13), Constituent("GOOG", 0.12), *filler("R", 25, 0.03)]
    assert rules.special_rebalance_triggered(cons).triggered
    # Comfortably below both triggers.
    assert not rules.special_rebalance_triggered(qqq_unchanged := [
        Constituent("A", 0.10),
        *filler("R", 30, 0.03),
    ])
    assert QQQRules().check_constraints(qqq_unchanged) == []


def test_qqq_check_constraints_event_specific():
    cons = [Constituent("AAPL", 0.16), *filler("R", 42, 0.02)]
    q = {b.rule for b in QQQRules().check_constraints(cons, "quarterly_rebalance")}
    d = {b.rule for b in QQQRules().check_constraints(cons, "annual_reconstitution")}
    assert q == set()
    assert d == {"qqq_security_15"}
    cohort = {b.rule for b in QQQRules().check_constraints(qqq_cohort_52())}
    assert cohort == {"qqq_cohort_48"}


# ---------------------------------------------------------------------------
# IGV capping
# ---------------------------------------------------------------------------


def test_igv_single_cap_proportional_redistribution():
    cons = [
        Constituent("A", 0.10),
        Constituent("B", 0.06),
        Constituent("C", 0.05),
        *filler("R", 20, 0.79 / 20),
    ]
    res = IGVRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    assert t["A"] == pytest.approx(0.085, abs=TOL)
    factor = 0.915 / 0.90
    assert t["B"] == pytest.approx(0.06 * factor, abs=TOL)
    assert t["C"] == pytest.approx(0.05 * factor, abs=TOL)
    assert t["R00"] == pytest.approx(0.79 / 20 * factor, abs=TOL)
    assert t["R00"] < 0.045  # no recipient crosses into the cohort
    assert res.metrics["max_weight"] == pytest.approx(0.10)
    assert res.metrics["sum_over_4_5"] == pytest.approx(0.21)
    assert res.deltas["A"] == pytest.approx(-0.015, abs=TOL)
    assert any("8.5%" in b for b in res.binding_constraints)


def test_igv_aggregate_rule_reduces_lowest_breaching_name_to_4_5():
    # Cohort: 8.5, 8.5, 8.4, 8.3, 8.2, 5.0 = 46.9% > 45%.  Cumulative weights
    # cross 45% at F, which can only give up 0.5% before reaching 4.5%.
    cons = [
        Constituent("A", 0.085),
        Constituent("B", 0.085),
        Constituent("C", 0.084),
        Constituent("D", 0.083),
        Constituent("E", 0.082),
        Constituent("F", 0.050),
        *filler("R", 20, 0.531 / 20),
    ]
    res = IGVRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    assert t["F"] == pytest.approx(0.045, abs=TOL)
    for k in "ABCDE":
        assert t[k] == pytest.approx(res.current_weights[k], abs=TOL)
    assert t["R00"] == pytest.approx(0.531 / 20 + 0.005 / 20, abs=TOL)
    assert res.target_metrics["sum_over_4_5"] == pytest.approx(0.419, abs=TOL)
    assert res.metrics["sum_over_4_5"] == pytest.approx(0.469)
    assert any("45%" in b and "F" in b for b in res.binding_constraints)


def test_igv_aggregate_rule_partial_cut_then_next_name():
    # Cohort 20, 15, 12, 5 = 52%.  Cumulative crosses 45% at C (47%): C is cut
    # by 2% to 10%.  Still 50% > 45%: now D causes the breach and goes to 4.5%.
    cons = [
        Constituent("A", 0.20),
        Constituent("B", 0.15),
        Constituent("C", 0.12),
        Constituent("D", 0.05),
        *filler("R", 16, 0.03),
    ]
    # A and B are above 8.5% in this synthetic set, so cap (a) would fire first;
    # disable it on this instance to check the aggregate logic on its own.
    rules = IGVRules()
    rules.SINGLE_CAP = 1.0
    res = rules.apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    assert t["A"] == pytest.approx(0.20, abs=TOL)
    assert t["B"] == pytest.approx(0.15, abs=TOL)
    assert t["C"] == pytest.approx(0.10, abs=TOL)
    assert t["D"] == pytest.approx(0.045, abs=TOL)
    assert t["R00"] == pytest.approx((0.48 + 0.02 + 0.005) / 16, abs=TOL)
    assert res.target_metrics["sum_over_4_5"] == pytest.approx(0.45, abs=TOL)


def test_igv_recipients_capped_at_4_5():
    # Only one small recipient: it can absorb at most up to 4.5%.
    rules = IGVRules()
    rules.SINGLE_CAP = 1.0
    cons = [
        Constituent("A", 0.30),
        Constituent("B", 0.20),
        Constituent("C", 0.10),
        Constituent("D", 0.36),
        Constituent("R", 0.04),
    ]
    res = rules.apply_caps(cons, AS_OF)
    assert res.feasible is False  # 0.5% capacity cannot fix a 51% breach
    assert res.target_weights["R"] == pytest.approx(0.045, abs=TOL)
    assert sum(res.target_weights.values()) == pytest.approx(1.0, abs=TOL)
    assert any("unsatisfied" in n for n in res.notes)


def test_igv_unchanged_when_no_rule_binds():
    cons = [Constituent("A", 0.08), Constituent("B", 0.05), *filler("R", 29, 0.87 / 29)]
    res = IGVRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    assert res.binding_constraints == []
    assert all(abs(d) < TOL for d in res.deltas.values())


def test_igv_check_constraints():
    cons = [Constituent("A", 0.10), Constituent("B", 0.06), *filler("R", 20, 0.84 / 20)]
    breaches = IGVRules().check_constraints(cons)
    assert [b.rule for b in breaches] == ["igv_single_cap_8_5"]
    assert breaches[0].tickers == ("A",)
    cohort = [Constituent(k, 0.08) for k in "ABCDEF"] + filler("R", 26, 0.02)  # 48% > 45%
    rules = {b.rule for b in IGVRules().check_constraints(cohort)}
    assert rules == {"igv_cohort_45"}


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def _find(events: list[RebalanceEvent], kind: str, month: int) -> RebalanceEvent:
    match = [e for e in events if e.kind == kind and e.effective_trade_date.month == month]
    assert len(match) == 1, (kind, month, events)
    return match[0]


@needs_calendar
def test_soxx_schedule_2026():
    events = SOXXRules().schedule(2026)
    assert [e.kind for e in events] == [
        "quarterly_rebalance",
        "quarterly_rebalance",
        "annual_reconstitution",
        "quarterly_rebalance",
    ]
    dec = _find(events, "quarterly_rebalance", 12)
    assert dec.reference_date == date(2026, 11, 30)
    assert dec.effective_trade_date == date(2026, 12, 18)
    assert dec.effective_date == date(2026, 12, 21)
    assert dec.announcement_date == date(2026, 12, 4)  # first Friday of December
    sep = _find(events, "annual_reconstitution", 9)
    assert sep.reference_date == date(2026, 8, 31)
    assert sep.effective_trade_date == date(2026, 9, 18)
    assert sep.announcement_date == date(2026, 9, 4)
    assert "2026-07-31" in sep.notes  # eligibility date
    mar = _find(events, "quarterly_rebalance", 3)
    assert mar.reference_date == date(2026, 2, 27)
    assert mar.effective_trade_date == date(2026, 3, 20)
    assert all(e.index_id == "SOXX" for e in events)


@needs_calendar
def test_qqq_schedule_2026_and_2027():
    events = QQQRules().schedule(2026)
    dec = _find(events, "annual_reconstitution", 12)
    assert dec.reference_date == date(2026, 11, 30)
    assert dec.effective_trade_date == date(2026, 12, 18)
    assert dec.effective_date == date(2026, 12, 21)
    # Sixth trading day prior to the effective date (Mon 21st): 18,17,16,15,14,11.
    assert dec.announcement_date == date(2026, 12, 11)
    assert sum(e.kind == "quarterly_rebalance" for e in events) == 3
    mar27 = _find(QQQRules().schedule(2027), "quarterly_rebalance", 3)
    assert mar27.reference_date == date(2027, 2, 26)
    assert mar27.effective_trade_date == date(2027, 3, 19)
    assert mar27.effective_date == date(2027, 3, 22)
    assert mar27.announcement_date == date(2027, 3, 12)


@needs_calendar
def test_igv_schedule_2026():
    events = IGVRules().schedule(2026)
    kinds = sorted((e.kind, e.effective_trade_date.month) for e in events)
    assert kinds == [
        ("quarterly_rebalance", 3),
        ("quarterly_rebalance", 6),
        ("quarterly_rebalance", 9),
        ("quarterly_rebalance", 12),
        ("semiannual_reconstitution", 6),
        ("semiannual_reconstitution", 12),
    ]
    dec = _find(events, "quarterly_rebalance", 12)
    assert dec.reference_date == date(2026, 12, 10)  # Thursday before the second Friday
    assert dec.effective_trade_date == date(2026, 12, 18)
    assert dec.effective_date == date(2026, 12, 21)
    assert dec.announcement_date is None
    jun = _find(events, "semiannual_reconstitution", 6)
    assert jun.reference_date == date(2026, 5, 29)
    # 2026-06-19 (third Friday) is the Juneteenth holiday: trade on Thursday.
    assert jun.effective_trade_date == date(2026, 6, 18)
    assert jun.effective_date == date(2026, 6, 22)
    jun_q = _find(events, "quarterly_rebalance", 6)
    assert jun_q.reference_date == date(2026, 6, 11)


@needs_calendar
def test_third_friday_holiday_moves_trade_to_previous_session():
    # 2027-06-18 is the third Friday of June and the Juneteenth holiday.
    from etf_tracker.market_calendar import is_trading_day

    if is_trading_day(date(2027, 6, 18)):  # pragma: no cover - calendar dependent
        pytest.skip("calendar does not treat 2027-06-18 as a holiday")
    ev = _find(SOXXRules().schedule(2027), "quarterly_rebalance", 6)
    assert ev.effective_trade_date == date(2027, 6, 17)
    assert ev.effective_date == date(2027, 6, 21)


@needs_calendar
def test_next_events_and_event_type_for():
    rules = QQQRules()
    nxt = rules.next_events(date(2026, 10, 9), n=4)
    assert [e.effective_trade_date for e in nxt] == [
        date(2026, 12, 18),
        date(2027, 3, 19),
        date(2027, 6, 17),  # 2027-06-18 is Juneteenth
        date(2027, 9, 17),
    ]
    assert nxt[0].kind == "annual_reconstitution"
    # An event trading today is still "next"; one that traded yesterday is not.
    assert rules.next_events(date(2026, 12, 18), n=1)[0].effective_trade_date == date(2026, 12, 18)
    assert rules.next_events(date(2026, 12, 19), n=1)[0].effective_trade_date == date(2027, 3, 19)
    assert rules.event_type_for(date(2026, 10, 9)) == "annual_reconstitution"
    assert rules.event_type_for(date(2027, 1, 5)) == "quarterly_rebalance"
    # IGV: the reconstitution wins when it shares the trade date with a rebalance,
    # and it is listed first so that dashboards showing next_events[0] agree.
    assert IGVRules().event_type_for(date(2026, 10, 9)) == "semiannual_reconstitution"
    igv_next = IGVRules().next_events(date(2026, 10, 9), n=2)
    assert [e.kind for e in igv_next] == ["semiannual_reconstitution", "quarterly_rebalance"]
    assert igv_next[0].effective_trade_date == igv_next[1].effective_trade_date == date(2026, 12, 18)
    assert IGVRules().event_type_for(date(2027, 1, 5)) == "quarterly_rebalance"
    assert len(SOXXRules().next_events(date(2026, 1, 1), n=10)) == 10


# ---------------------------------------------------------------------------
# Adversarial review: cap cascades, cohort interplay, thresholds, fixed points
# ---------------------------------------------------------------------------


def _reapply(rules, res, event_type="quarterly_rebalance", adrs=frozenset()):
    """Run the rules again on a result's target weights (fixed-point check)."""
    cons = [Constituent(t, w, is_adr=t in adrs) for t, w in res.target_weights.items()]
    return rules.apply_caps(cons, AS_OF, event_type)


def test_soxx_redistribution_cascades_into_the_4_percent_cap():
    # A's 12% excess scaled to the rest would lift F (6th, 3.9%) to 4.485%, so F
    # must be caught by the 4% cap and the factor recomputed on the rest.
    cons = [
        Constituent("A", 0.20),
        *[Constituent(k, 0.05) for k in "BCDE"],
        Constituent("F", 0.039),
        *filler("S", 24, 0.561 / 24),
    ]
    res = SOXXRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    assert t["A"] == pytest.approx(0.08, abs=TOL)
    assert t["F"] == pytest.approx(0.04, abs=TOL)
    factor = (1.0 - 0.08 - 0.04) / (1.0 - 0.20 - 0.039)
    assert t["B"] == pytest.approx(0.05 * factor, abs=TOL)
    assert t["S00"] == pytest.approx(0.561 / 24 * factor, abs=TOL)
    assert t["B"] < 0.08 and t["S00"] < 0.04
    assert any("4%" in b and "F" in b for b in res.binding_constraints)
    assert res.deltas["F"] == pytest.approx(0.001, abs=TOL)  # F still gains, up to its cap


def test_soxx_adr_in_top5_is_capped_then_scaled_with_other_adrs():
    # TSM (ADR, 12%) is in the initial top-5: first hit by the 8% cap, then the
    # whole ADR group (TSM + ASML) is scaled proportionally to 10%.
    cons = [
        Constituent("TSM", 0.12, is_adr=True),
        Constituent("ASML", 0.05, is_adr=True),
        Constituent("A", 0.07),
        Constituent("B", 0.06),
        Constituent("C", 0.05),
        *filler("S", 25, 0.65 / 25),
    ]
    res = SOXXRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    f1 = 0.92 / 0.88  # 4% excess of TSM over the other 88%
    adr_sum = 0.08 + 0.05 * f1
    adr_scale = 0.10 / adr_sum
    f2 = 0.90 / (1.0 - adr_sum)
    assert t["TSM"] == pytest.approx(0.08 * adr_scale, abs=TOL)
    assert t["ASML"] == pytest.approx(0.05 * f1 * adr_scale, abs=TOL)
    assert t["TSM"] + t["ASML"] == pytest.approx(0.10, abs=TOL)
    assert t["A"] == pytest.approx(0.07 * f1 * f2, abs=TOL)
    assert t["S00"] == pytest.approx(0.65 / 25 * f1 * f2, abs=TOL)
    assert t["TSM"] < 0.08  # ADR scaling takes it below the single-name cap
    assert res.deltas["TSM"] < res.deltas["ASML"] < 0 < res.deltas["A"]
    assert all(w <= 0.04 + TOL for k, w in t.items() if k.startswith("S"))


def test_soxx_adr_excess_respects_single_caps_of_recipients():
    # 16% of ADRs scaled to 10%: the 6% freed may not lift A/B/C through 8%,
    # so it has to flow on to the small names.
    cons = [
        *[Constituent(k, 0.079) for k in "ABC"],
        Constituent("ASML", 0.09, is_adr=True),
        Constituent("TSM", 0.09, is_adr=True),
        *filler("S", 25, 0.583 / 25),
    ]
    res = SOXXRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    for k in "ABC":
        assert t[k] == pytest.approx(0.08, abs=TOL)
    assert t["ASML"] == pytest.approx(0.05, abs=TOL)
    assert t["TSM"] == pytest.approx(0.05, abs=TOL)
    assert t["S00"] == pytest.approx((1.0 - 0.24 - 0.10) / 25, abs=TOL)
    assert t["S00"] < 0.04


def test_soxx_adrs_exactly_at_10_percent_are_not_touched():
    cons = [
        Constituent("ASML", 0.05, is_adr=True),
        Constituent("TSM", 0.05, is_adr=True),
        *filler("S", 28, 0.90 / 28),
    ]
    res = SOXXRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    assert res.binding_constraints == []
    assert all(abs(d) < TOL for d in res.deltas.values())
    assert SOXXRules().check_constraints(cons) == []


def test_soxx_initial_top5_is_fixed_before_capping():
    # E (5.0%) is 5th by uncapped weight; F (4.9%) is 6th.  After capping, E
    # keeps its 8% allowance and F is capped at 4% even though E and F were
    # nearly tied, i.e. the top-5 is decided once on the *initial* weights.
    cons = [
        Constituent("A", 0.15),
        *[Constituent(k, 0.06) for k in "BCD"],
        Constituent("E", 0.050),
        Constituent("F", 0.049),
        *filler("S", 24, (1.0 - 0.15 - 0.18 - 0.099) / 24),
    ]
    rules = SOXXRules()
    assert rules.initial_top5(_normalised(cons)) == ["A", "B", "C", "D", "E"]
    res = rules.apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    assert t["F"] == pytest.approx(0.04, abs=TOL)
    assert 0.05 < t["E"] < 0.08
    assert res.deltas["F"] < 0 < res.deltas["E"]
    breaches = {b.rule: b for b in rules.check_constraints(cons)}
    assert breaches["soxx_outside_top5_cap_4"].tickers == ("F",)


def _normalised(cons):
    total = sum(c.weight for c in cons)
    return {c.ticker: c.weight / total for c in cons}


def test_soxx_target_is_a_fixed_point():
    rules = SOXXRules()
    for cons in (
        soxx_two_over_8_one_over_4(),
        [
            Constituent("TSM", 0.12, is_adr=True),
            Constituent("ASML", 0.05, is_adr=True),
            Constituent("A", 0.07),
            Constituent("B", 0.06),
            Constituent("C", 0.05),
            *filler("S", 25, 0.65 / 25),
        ],
    ):
        res = rules.apply_caps(cons, AS_OF)
        adrs = frozenset(c.ticker for c in cons if c.is_adr)
        again = _reapply(rules, res, adrs=adrs)
        assert max(abs(d) for d in again.deltas.values()) < 1e-9
        assert again.binding_constraints == []


def test_qqq_two_companies_above_24_are_both_capped_at_20():
    cons = [Constituent("A", 0.30), Constituent("B", 0.26), *filler("R", 40, 0.44 / 40)]
    res = QQQRules().apply_caps(cons, AS_OF, "quarterly_rebalance")
    assert_sums_to_one(res)
    t = res.target_weights
    assert t["A"] == pytest.approx(0.20, abs=TOL)
    assert t["B"] == pytest.approx(0.20, abs=TOL)
    assert t["R00"] == pytest.approx(0.60 / 40, abs=TOL)
    assert res.deltas["A"] == pytest.approx(-0.10, abs=TOL)
    assert res.deltas["B"] == pytest.approx(-0.06, abs=TOL)
    assert_rank_preserved(res.current_weights, t)


def test_qqq_stage1_redistribution_can_trigger_stage2():
    # Cohort is only A (30%) before capping; stage 1 lifts the 5% names to
    # 5.71% so that the cohort becomes 20 + 5*5.71 = 48.57% >= 48% and stage 2
    # fires on the *resulting* weights, cutting A below 20%.
    cons = [Constituent("A", 0.30), *[Constituent(k, 0.05) for k in "BCDEF"], *filler("R", 45, 0.01)]
    res = QQQRules().apply_caps(cons, AS_OF, "quarterly_rebalance")
    assert_sums_to_one(res)
    t = res.target_weights
    f1 = 0.80 / 0.70
    cohort = 0.20 + 5 * 0.05 * f1
    assert cohort >= 0.48
    f2 = 0.40 / cohort
    assert t["A"] == pytest.approx(0.20 * f2, abs=TOL)
    assert t["B"] == pytest.approx(0.05 * f1 * f2, abs=TOL)
    assert t["B"] > 0.045  # cohort members stay above the threshold here
    assert t["R00"] == pytest.approx(0.60 / 45, abs=TOL)
    assert res.target_metrics["sum_over_4_5"] == pytest.approx(0.40, abs=TOL)
    assert len([b for b in res.binding_constraints if "20%" in b]) == 1
    assert len([b for b in res.binding_constraints if "40%" in b]) == 1
    assert_rank_preserved(res.current_weights, t)


def test_qqq_company_exactly_at_24_is_not_capped_but_cohort_exactly_48_is_cut():
    rules = QQQRules()
    at24 = [Constituent("A", 0.24), *filler("R", 38, 0.76 / 38)]
    res = rules.apply_caps(at24, AS_OF)
    assert res.target_weights["A"] == pytest.approx(0.24, abs=TOL)
    assert res.binding_constraints == []
    assert rules.check_constraints(at24) == []
    at48 = [Constituent(k, 0.12) for k in "ABCD"] + filler("R", 26, 0.02)
    res = rules.apply_caps(at48, AS_OF)
    assert_sums_to_one(res)
    assert res.target_weights["A"] == pytest.approx(0.10, abs=TOL)
    assert [b.rule for b in rules.check_constraints(at48)] == ["qqq_cohort_48"]
    # ... while the special-rebalance trigger is strict at both thresholds.
    assert not rules.special_rebalance_triggered(at24)
    assert not rules.special_rebalance_triggered(at48)
    assert rules.special_rebalance_triggered([Constituent("A", 0.2400001), *filler("R", 38, 0.7599999 / 38)])
    just_over = [Constituent(k, 0.12) for k in "ABC"] + [Constituent("D", 0.1200001)] + filler("R", 26, 0.5199999 / 26)
    assert rules.special_rebalance_triggered(just_over)


def test_qqq_quarterly_target_is_a_fixed_point():
    rules = QQQRules()
    for cons in (qqq_cohort_52(), [Constituent("GOOGL", 0.14), Constituent("GOOG", 0.12), Constituent("MSFT", 0.04), *filler("R", 35, 0.70 / 35)]):
        res = rules.apply_caps(cons, AS_OF, "quarterly_rebalance")
        again = _reapply(rules, res)
        assert max(abs(d) for d in again.deltas.values()) < 1e-9
        assert again.binding_constraints == []


def test_qqq_december_company_cohort_of_five_securities_hits_top5_rule_exactly():
    # Company cohort (Alphabet with two classes, MSFT, NVDA, AAPL) is cut to
    # 40%; those four companies are exactly the five largest securities, so the
    # security-level top-5 sum is exactly 40% -> "40% or greater" fires.
    cons = [
        Constituent("GOOGL", 0.14),
        Constituent("GOOG", 0.12),
        Constituent("MSFT", 0.16),
        Constituent("NVDA", 0.10),
        Constituent("AAPL", 0.09),
        *filler("R", 35, 0.39 / 35),
    ]
    rules = QQQRules()
    res = rules.apply_caps(cons, AS_OF, "annual_reconstitution")
    assert_sums_to_one(res)
    t = res.target_weights
    top5 = ["GOOGL", "GOOG", "MSFT", "NVDA", "AAPL"]
    assert sum(t[k] for k in top5) == pytest.approx(0.385, abs=TOL)
    f1 = 0.80 / 0.74
    cohort = 0.20 + (0.16 + 0.10 + 0.09) * f1
    comp_factor = 0.40 / cohort
    sec_factor = 0.385 / 0.40
    assert t["MSFT"] == pytest.approx(0.16 * f1 * comp_factor * sec_factor, abs=TOL)
    assert t["GOOGL"] == pytest.approx(0.20 * 14 / 26 * comp_factor * sec_factor, abs=TOL)
    assert t["R00"] == pytest.approx(0.615 / 35, abs=TOL)
    assert any("38.5%" in b for b in res.binding_constraints)
    # Same input at a quarterly rebalance: the security-level rules do not apply.
    q = rules.apply_caps(cons, AS_OF, "quarterly_rebalance")
    assert sum(q.target_weights[k] for k in top5) == pytest.approx(0.40, abs=TOL)
    assert not any("38.5%" in b for b in q.binding_constraints)


def test_qqq_december_security_pass_does_not_revisit_company_level():
    # A (25%) -> company cap 20% -> security cap 14%.  The 6% freed by the 14%
    # cap is spread pro rata and lifts eight 4.27% names above 4.5%, so the
    # final company cohort is 14 + 8*4.59 = 50.7% >= 48%.  Nasdaq applies the
    # levels in sequence without back-and-forth, so the weights stand, but the
    # re-breach must be reported in the notes.
    cons = [Constituent("A", 0.25), *[Constituent(f"B{i}", 0.04) for i in range(8)], *filler("R", 30, 0.43 / 30)]
    rules = QQQRules()
    res = rules.apply_caps(cons, AS_OF, "annual_reconstitution")
    assert_sums_to_one(res)
    t = res.target_weights
    assert t["A"] == pytest.approx(0.14, abs=TOL)
    f_company = 0.80 / 0.75
    f_security = 0.86 / 0.80
    assert t["B0"] == pytest.approx(0.04 * f_company * f_security, abs=TOL)
    assert t["B0"] > 0.045
    assert res.target_metrics["sum_over_4_5"] > 0.48
    assert res.feasible  # the methodology's result, not an infeasible one
    assert any("after the security-level pass" in n and "48%" in n for n in res.notes)
    assert [b for b in res.binding_constraints if "40%" in b] == []  # stage 2 never fired
    # The quarterly path (company level only) has no such note and is a fixed point.
    q = rules.apply_caps(cons, AS_OF, "quarterly_rebalance")
    assert q.notes == []
    assert max(abs(d) for d in _reapply(rules, q).deltas.values()) < 1e-9


def test_qqq_december_without_rebreach_has_no_note_and_is_a_fixed_point():
    cons = [
        Constituent("AAPL", 0.16),
        Constituent("MSFT", 0.07),
        Constituent("NVDA", 0.06),
        Constituent("AMZN", 0.05),
        *filler("R", 33, 0.02),
    ]
    rules = QQQRules()
    res = rules.apply_caps(cons, AS_OF, "annual_reconstitution")
    assert res.notes == []
    again = _reapply(rules, res, "annual_reconstitution")
    assert max(abs(d) for d in again.deltas.values()) < 1e-9


def test_qqq_special_rebalance_event_type_uses_company_rules_only():
    cons = [Constituent("AAPL", 0.16), *filler("R", 42, 0.02)]
    res = QQQRules().apply_caps(cons, AS_OF, "special_rebalance")
    assert res.target_weights["AAPL"] == pytest.approx(0.16, abs=TOL)
    assert res.binding_constraints == []
    assert QQQRules().check_constraints(cons, "special_rebalance") == []


def test_igv_single_cap_can_push_cohort_over_45_then_aggregate_rule_fires():
    # (a): A 12% -> 8.5%; B and C (8.5%) would be lifted above 8.5% so they are
    # held at the cap too; D, E, F and the fillers share factor 0.745/0.71.
    # (b): cohort = 25.5 + 2*8.394 + 4.827 = 47.1% > 45%; the cumulative weight
    # crosses 45% at F, which can only give up 0.327% before reaching 4.5%; the
    # remaining cohort (42.3%) satisfies the rule, so nobody else is touched.
    cons = [
        Constituent("A", 0.12),
        Constituent("B", 0.085),
        Constituent("C", 0.085),
        Constituent("D", 0.08),
        Constituent("E", 0.08),
        Constituent("F", 0.046),
        *filler("R", 20, 0.504 / 20),
    ]
    res = IGVRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    f = (1.0 - 3 * 0.085) / (1.0 - 0.12 - 0.17)
    for k in "ABC":
        assert t[k] == pytest.approx(0.085, abs=TOL)
    assert t["D"] == pytest.approx(0.08 * f, abs=TOL)
    assert t["E"] == pytest.approx(0.08 * f, abs=TOL)
    assert t["F"] == pytest.approx(0.045, abs=TOL)
    given_up = 0.046 * f - 0.045
    assert t["R00"] == pytest.approx(0.504 / 20 * f + given_up / 20, abs=TOL)
    assert res.target_metrics["sum_over_4_5"] == pytest.approx(0.255 + 0.16 * f, abs=TOL)
    assert res.target_metrics["sum_over_4_5"] < 0.45
    assert res.deltas["F"] == pytest.approx(-0.001, abs=TOL)
    assert [b for b in res.binding_constraints if "45%" in b] == [
        "IGV aggregate 45% rule (companies >4.5%): reduced F"
    ]


def test_igv_culprit_is_where_the_cumulative_weight_crosses_45():
    # Cohort 20, 15, 12, 6, 5 = 58%.  Cumulative: 20, 35, 47 -> C causes the
    # breach and is cut by 2%; then D (51%) and E (50%) each go to 4.5%.
    rules = IGVRules()
    rules.SINGLE_CAP = 1.0
    cons = [
        Constituent("A", 0.20),
        Constituent("B", 0.15),
        Constituent("C", 0.12),
        Constituent("D", 0.06),
        Constituent("E", 0.05),
        *filler("R", 21, 0.42 / 21),
    ]
    res = rules.apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    t = res.target_weights
    assert (t["A"], t["B"]) == (pytest.approx(0.20, abs=TOL), pytest.approx(0.15, abs=TOL))
    assert t["C"] == pytest.approx(0.10, abs=TOL)
    assert t["D"] == pytest.approx(0.045, abs=TOL)
    assert t["E"] == pytest.approx(0.045, abs=TOL)
    assert t["R00"] == pytest.approx((0.42 + 0.02 + 0.015 + 0.005) / 21, abs=TOL)
    assert t["R00"] < 0.045
    assert res.target_metrics["sum_over_4_5"] == pytest.approx(0.45, abs=TOL)
    assert any(b.endswith("reduced C, D, E") for b in res.binding_constraints)
    again = _reapply(rules, res)
    assert max(abs(d) for d in again.deltas.values()) < 1e-9


def test_igv_target_is_a_fixed_point():
    rules = IGVRules()
    cons = [
        Constituent("A", 0.12),
        Constituent("B", 0.085),
        Constituent("C", 0.085),
        Constituent("D", 0.08),
        Constituent("E", 0.08),
        Constituent("F", 0.046),
        *filler("R", 20, 0.504 / 20),
    ]
    res = rules.apply_caps(cons, AS_OF)
    again = _reapply(rules, res)
    assert max(abs(d) for d in again.deltas.values()) < 1e-9
    assert again.binding_constraints == []


def test_igv_cohort_exactly_45_is_not_a_breach():
    cons = [Constituent(k, 0.075) for k in "ABCDEF"] + filler("R", 22, 0.55 / 22)  # 45.0%
    res = IGVRules().apply_caps(cons, AS_OF)
    assert_sums_to_one(res)
    assert res.binding_constraints == []
    assert IGVRules().check_constraints(cons) == []


# -- schedules: real-world anchors and holiday interactions -------------------


@needs_calendar
def test_qqq_schedule_2023_2024_matches_published_dates():
    ev23 = {(e.kind, e.effective_trade_date.month): e for e in QQQRules().schedule(2023)}
    jun = ev23[("quarterly_rebalance", 6)]
    # Juneteenth 2023 fell on the Monday after the third Friday: effective Tuesday,
    # and the sixth trading day before Tue 20 June is Fri 9 June.
    assert jun.reference_date == date(2023, 5, 31)
    assert jun.effective_trade_date == date(2023, 6, 16)
    assert jun.effective_date == date(2023, 6, 20)
    assert jun.announcement_date == date(2023, 6, 9)
    dec = ev23[("annual_reconstitution", 12)]
    assert (dec.reference_date, dec.announcement_date) == (date(2023, 11, 30), date(2023, 12, 8))
    assert (dec.effective_trade_date, dec.effective_date) == (date(2023, 12, 15), date(2023, 12, 18))
    ev24 = {(e.kind, e.effective_trade_date.month): e for e in QQQRules().schedule(2024)}
    dec = ev24[("annual_reconstitution", 12)]
    assert dec.reference_date == date(2024, 11, 29)  # 30 Nov 2024 is a Saturday
    assert dec.announcement_date == date(2024, 12, 13)
    assert (dec.effective_trade_date, dec.effective_date) == (date(2024, 12, 20), date(2024, 12, 23))
    jun = ev24[("quarterly_rebalance", 6)]
    assert jun.announcement_date == date(2024, 6, 13)  # Juneteenth (Wed 19th) inside the window
    assert ev24[("quarterly_rebalance", 3)].reference_date == date(2024, 2, 29)  # leap day


@needs_calendar
def test_qqq_june_2026_announcement_skips_juneteenth():
    jun = _find(QQQRules().schedule(2026), "quarterly_rebalance", 6)
    assert jun.effective_trade_date == date(2026, 6, 18)  # Fri 19th closed
    assert jun.effective_date == date(2026, 6, 22)
    # Six trading days before Mon 22 June: 18, 17, 16, 15, 12, 11.
    assert jun.announcement_date == date(2026, 6, 11)
    assert jun.reference_date == date(2026, 5, 29)


@needs_calendar
def test_soxx_schedule_2025_and_month_end_holidays():
    events = SOXXRules().schedule(2025)
    mar, jun, sep, dec = events
    assert (mar.reference_date, mar.announcement_date) == (date(2025, 2, 28), date(2025, 3, 7))
    assert (mar.effective_trade_date, mar.effective_date) == (date(2025, 3, 21), date(2025, 3, 24))
    assert jun.reference_date == date(2025, 5, 30)
    assert sep.kind == "annual_reconstitution"
    assert (sep.reference_date, sep.announcement_date) == (date(2025, 8, 29), date(2025, 9, 5))
    assert (sep.effective_trade_date, sep.effective_date) == (date(2025, 9, 19), date(2025, 9, 22))
    assert "2025-07-31" in sep.notes
    assert dec.reference_date == date(2025, 11, 28)  # 30 Nov 2025 is a Sunday
    assert (dec.announcement_date, dec.effective_trade_date) == (date(2025, 12, 5), date(2025, 12, 19))
    # Memorial Day 2027-05-31 pushes the June 2027 reference date to Fri 28 May.
    jun27 = _find(SOXXRules().schedule(2027), "quarterly_rebalance", 6)
    assert jun27.reference_date == date(2027, 5, 28)
    assert jun27.announcement_date == date(2027, 6, 4)
    assert (jun27.effective_trade_date, jun27.effective_date) == (date(2027, 6, 17), date(2027, 6, 21))


@needs_calendar
def test_good_friday_2008_on_third_friday_moves_trade_to_thursday():
    from etf_tracker.market_calendar import is_trading_day

    assert not is_trading_day(date(2008, 3, 21))
    for rules in (SOXXRules(), QQQRules(), IGVRules()):
        mar = _find(rules.schedule(2008), "quarterly_rebalance", 3)
        assert mar.effective_trade_date == date(2008, 3, 20)
        assert mar.effective_date == date(2008, 3, 24)
    # Six trading days before Mon 24 March, skipping the closed Friday: 20, 19, 18, 17, 14, 13.
    assert _find(QQQRules().schedule(2008), "quarterly_rebalance", 3).announcement_date == date(2008, 3, 13)


@needs_calendar
def test_igv_schedule_2025_reference_dates():
    events = IGVRules().schedule(2025)
    q = {e.effective_trade_date.month: e for e in events if e.kind == "quarterly_rebalance"}
    assert [q[m].reference_date for m in (3, 6, 9, 12)] == [
        date(2025, 3, 13),
        date(2025, 6, 12),
        date(2025, 9, 11),
        date(2025, 12, 11),
    ]
    r = {e.effective_trade_date.month: e for e in events if e.kind == "semiannual_reconstitution"}
    assert r[6].reference_date == date(2025, 5, 30)
    assert r[12].reference_date == date(2025, 11, 28)
    assert r[12].effective_trade_date == q[12].effective_trade_date == date(2025, 12, 19)
    assert all(e.announcement_date is None for e in events)


@needs_calendar
def test_event_type_for_switches_the_day_after_the_trade_date():
    soxx = SOXXRules()
    assert soxx.event_type_for(date(2026, 9, 18)) == "annual_reconstitution"
    assert soxx.event_type_for(date(2026, 9, 19)) == "quarterly_rebalance"
    nxt = soxx.next_events(date(2026, 8, 31), n=3)
    assert [(e.kind, e.effective_trade_date) for e in nxt] == [
        ("annual_reconstitution", date(2026, 9, 18)),
        ("quarterly_rebalance", date(2026, 12, 18)),
        ("quarterly_rebalance", date(2027, 3, 19)),
    ]
    qqq = QQQRules()
    assert qqq.event_type_for(date(2026, 12, 18)) == "annual_reconstitution"
    assert qqq.event_type_for(date(2026, 12, 21)) == "quarterly_rebalance"
    # Every scheduled event has consistent ordering of its dates.
    for rules in (soxx, qqq, IGVRules()):
        for year in (2025, 2026, 2027):
            for e in rules.schedule(year):
                assert e.reference_date < e.effective_trade_date < e.effective_date
                if e.announcement_date is not None:
                    assert e.reference_date < e.announcement_date < e.effective_trade_date
