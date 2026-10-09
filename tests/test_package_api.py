"""Tests for the etf_tracker top-level package API (re-exports and version)."""

from __future__ import annotations

import inspect
import sys
from datetime import date

import pytest

import etf_tracker
from etf_tracker import bridge, decompose, holdings, market_calendar, rules


def test_version():
    assert etf_tracker.__version__ == "0.2.0"
    assert "__version__" in etf_tracker.__all__


def test_all_names_resolve_and_are_unique():
    assert len(etf_tracker.__all__) == len(set(etf_tracker.__all__))
    for name in etf_tracker.__all__:
        assert hasattr(etf_tracker, name), name


def test_submodule_attributes_are_modules_not_shadowed():
    """Re-exporting a function named like a submodule would hide the module."""
    for name, module in [
        ("market_calendar", market_calendar),
        ("holdings", holdings),
        ("rules", rules),
        ("decompose", decompose),
        ("bridge", bridge),
        ("http", etf_tracker.http),
        ("sources", etf_tracker.sources),
        ("store", etf_tracker.store),
        ("analysis", etf_tracker.analysis),
        ("report", etf_tracker.report),
        ("pipeline", etf_tracker.pipeline),
    ]:
        assert inspect.ismodule(getattr(etf_tracker, name)), name
        assert getattr(etf_tracker, name) is sys.modules[f"etf_tracker.{name}"], name


def test_data_layer_entry_points_are_reexported():
    """0.2.0: the daily job, backfill, analysis, reports and the fetch registry."""
    from etf_tracker import analysis, pipeline, report, sources, store

    assert etf_tracker.run_daily is pipeline.run_daily
    assert etf_tracker.run_backfill is pipeline.run_backfill
    assert etf_tracker.run_fetch is pipeline.run_fetch
    assert etf_tracker.run_analysis is pipeline.run_analysis
    assert etf_tracker.fetch_one is pipeline.fetch_one
    assert etf_tracker.SOURCES is pipeline.SOURCES
    assert set(etf_tracker.SOURCES) == {"SOXX", "QQQ", "IGV"}
    assert etf_tracker.analyze_all is analysis.analyze_all
    assert etf_tracker.analyze_etf is analysis.analyze_etf
    # The top-level write_reports is the real writer, not the pipeline wrapper.
    assert etf_tracker.write_reports is report.write_reports
    assert etf_tracker.render_markdown is report.render_markdown
    assert etf_tracker.ingest is store.ingest
    assert etf_tracker.FetchResult is sources.FetchResult
    assert etf_tracker.STATUS_OK == "ok"
    for name in ("run_daily", "run_backfill", "analyze_all", "write_reports", "SOURCES"):
        assert name in etf_tracker.__all__, name


def test_importing_the_package_does_not_import_issuer_modules():
    """The pipeline resolves sources lazily; the network code stays out of
    ``import etf_tracker`` so that unit tests and the report step never touch
    it by accident."""
    import subprocess

    code = (
        "import sys, etf_tracker; "
        "print(sorted(m for m in sys.modules if m.startswith('etf_tracker.sources.')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout


def test_decompose_function_is_exposed_as_decompose_snapshots():
    assert etf_tracker.decompose_snapshots is decompose.decompose
    assert callable(etf_tracker.decompose_snapshots)
    assert inspect.ismodule(etf_tracker.decompose)


def test_import_submodule_as_alias_still_yields_the_module():
    import etf_tracker.decompose as dc  # the idiom tests/test_decompose.py relies on

    assert inspect.ismodule(dc)
    assert dc.decompose is etf_tracker.decompose_snapshots


@pytest.mark.parametrize(
    "top_name, module, inner_name",
    [
        ("is_trading_day", market_calendar, "is_trading_day"),
        ("third_friday", market_calendar, "third_friday"),
        ("Holding", holdings, "Holding"),
        ("Snapshot", holdings, "Snapshot"),
        ("save_snapshot", holdings, "save_snapshot"),
        ("Constituent", rules, "Constituent"),
        ("CapResult", rules, "CapResult"),
        ("rules_for", rules, "rules_for"),
        ("SOXXRules", rules, "SOXXRules"),
        ("DecompositionResult", decompose, "DecompositionResult"),
        ("decompose_from_weights", decompose, "decompose_from_weights"),
        ("constituents_from_snapshot", bridge, "constituents_from_snapshot"),
        ("apply_caps_to_snapshot", bridge, "apply_caps_to_snapshot"),
    ],
)
def test_reexports_are_the_same_objects(top_name, module, inner_name):
    assert getattr(etf_tracker, top_name) is getattr(module, inner_name)


def test_every_submodule_public_name_is_reachable_through_the_package():
    """Names not re-exported must still be reachable via the submodule attribute."""
    for module in (market_calendar, holdings, rules, decompose, bridge):
        for name in module.__all__:
            assert hasattr(module, name), (module.__name__, name)


def test_end_to_end_weights_stay_fractions_across_modules():
    """Snapshot -> decomposition -> constituents -> caps, all in fractions.

    A Nasdaq-100-like universe: Alphabet's two share classes together hold
    26% (above the 24% company trigger) while no other cap binds, so the
    company-level mapping of GOOGL/GOOG is what makes both classes forced
    sellers.  Prices move between the two snapshots but share counts do not,
    so the decomposition must attribute everything to drift.
    """
    as_of_prev = date(2026, 10, 8)
    as_of_curr = date(2026, 10, 9)
    shares = {"GOOGL": 130.0, "GOOG": 130.0}
    shares.update({f"M{i:02d}": 40.0 for i in range(15)})  # 15 x 4.0%
    shares.update({f"S{i:02d}": 10.0 for i in range(14)})  # 14 x 1.0%
    assert sum(shares.values()) == 1000.0

    def snap(as_of: date, price_of: dict[str, float]) -> etf_tracker.Snapshot:
        lines = [
            etf_tracker.Holding(
                etf="QQQ", as_of=as_of, ticker=t, name=t, asset_class=etf_tracker.EQUITY,
                shares=n, price=price_of.get(t, 1.0), market_value=None, weight=None,
            )
            for t, n in shares.items()
        ]
        lines.append(
            etf_tracker.Holding(
                etf="QQQ", as_of=as_of, ticker="USD", name="Cash", asset_class=etf_tracker.CASH,
                shares=None, price=None, market_value=100.0, weight=None,
            )
        )
        return etf_tracker.Snapshot(etf="QQQ", as_of=as_of, holdings=lines)

    prev = snap(as_of_prev, {})
    curr = snap(as_of_curr, {"GOOGL": 1.1, "GOOG": 1.1})

    # Decomposition: weights are fractions and a pure price move is all drift.
    result = etf_tracker.decompose_snapshots(prev, curr)
    assert sum(c.w_prev for c in result.changes) == pytest.approx(1.0)
    assert sum(c.w_curr for c in result.changes) == pytest.approx(1.0)
    assert all(0.0 <= c.w_curr <= 1.0 for c in result.changes)
    assert result.scale_factor == pytest.approx(1.0)
    assert all(abs(c.trade) < 1e-9 for c in result.changes)
    assert result.get("GOOGL").drift > 0 > result.get("M00").drift

    # Bridge: equity-only fractions, share classes grouped at company level.
    cons = etf_tracker.constituents_from_snapshot(curr, "QQQ")
    assert sum(c.weight for c in cons) == pytest.approx(1.0)
    assert etf_tracker.aggregate_by_company(cons)["ALPHABET"] == pytest.approx(286 / 1026)

    # Rules: the December annual-reconstitution rules apply after 2026-10-09;
    # Alphabet is capped at 20% as a company, split evenly across its classes.
    caps = etf_tracker.apply_caps_to_snapshot(curr, "QQQ")
    assert caps.event_type == "annual_reconstitution"
    assert caps.feasible
    assert sum(caps.target_weights.values()) == pytest.approx(1.0)
    assert all(0.0 <= w <= 1.0 for w in caps.target_weights.values())
    assert caps.target_weights["GOOGL"] == pytest.approx(0.10)
    assert caps.target_weights["GOOG"] == pytest.approx(0.10)
    assert caps.forced_sellers == ["GOOG", "GOOGL"]
    assert all(caps.deltas[t] > 0 for t in caps.deltas if t not in ("GOOG", "GOOGL"))
    assert caps.as_of == as_of_curr
    assert etf_tracker.is_trading_day(caps.as_of)
