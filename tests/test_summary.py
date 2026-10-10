"""reports/summary.json: the compact dashboard view of the analysis."""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import timedelta
from pathlib import Path

import pytest

from analysis_fixture import build_analysis_all
from etf_tracker import pipeline, summary
from etf_tracker.decompose import CLASS_ACTIVE_TRADE, CLASS_CORPORATE_ACTION, CLASS_ENTRY, CLASS_EXIT, CLASS_FLOW_ONLY
from test_pipeline_cli import NOW, TODAY, fake_sources

REPO = Path(__file__).resolve().parent.parent
LATEST = REPO / "reports" / "latest.json"
#: Ceiling for the committed data (3 ETFs, ~236 equities).  The exact
#: contract shape measures ~95 KB on a quiet day (equities ~60 KB, caps
#: ~10 KB because every uncapped IGV name is a forced buyer); the bound
#: guards against regressing toward latest.json (~220 KB).
MAX_BYTES = 80 * 1024

UNCHANGED_ETF_KEYS = ("etf", "index_id", "as_of", "prev_as_of", "source", "fund", "caps", "next_events", "warnings")


@pytest.fixture(scope="module")
def committed() -> dict:
    if not LATEST.is_file():
        pytest.skip("reports/latest.json is not committed")
    return json.loads(LATEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def built(committed: dict) -> dict:
    return summary.build_summary(committed)


def _equity_holdings(entry: dict) -> list[dict]:
    return [h for h in entry["holdings"] if h["asset_class"] == "equity"]


# ---------------------------------------------------------------------------
# Committed reports/latest.json
# ---------------------------------------------------------------------------


def test_top_level_and_etf_keys_follow_the_contract(committed: dict, built: dict):
    assert set(built) == set(summary.TOP_LEVEL_KEYS)
    assert built["generated_utc"] == committed["generated_utc"]
    assert built["today"] == committed["today"]
    assert built["errors"] == committed["errors"]
    assert set(built["etfs"]) == set(committed["etfs"]) == {"SOXX", "QQQ", "IGV"}
    for etf, block in built["etfs"].items():
        src = committed["etfs"][etf]
        assert set(block) == set(summary.ETF_KEYS), etf
        for key in UNCHANGED_ETF_KEYS:
            assert block[key] == src[key], (etf, key)
        assert "holdings" not in block
        for row in block["equities"]:
            assert set(row) == set(summary.EQUITY_KEYS)
        if block["decomposition"] is not None:
            assert set(block["decomposition"]) == set(summary.DECOMPOSITION_KEYS)
            assert "changes" not in block["decomposition"]


def test_equities_are_the_equity_holdings_sorted_and_renormalised(committed: dict, built: dict):
    for etf, block in built["etfs"].items():
        src_equities = _equity_holdings(committed["etfs"][etf])
        rows = block["equities"]
        assert block["equity_count"] == len(rows) == len(src_equities), etf
        assert sorted(r["ticker"] for r in rows) == sorted(h["ticker"] for h in src_equities)
        weights = [r["weight_eq"] for r in rows]
        assert weights == sorted(weights, reverse=True), etf
        assert math.isclose(sum(weights), 1.0, rel_tol=0.0, abs_tol=1e-9), (etf, sum(weights))
        published = {h["ticker"]: h for h in src_equities}
        total_mv = sum(h["market_value"] for h in src_equities)
        for row in rows:
            src = published[row["ticker"]]
            assert row["weight_fund"] == src["weight"]
            # the capping rules' basis: market-value share of the equity sleeve
            assert math.isclose(row["weight_eq"], src["market_value"] / total_mv, rel_tol=1e-12)
            assert row["is_adr"] is src["is_adr"]
            for key in ("name",):
                assert row[key] == src[key]
        # cash and futures are excluded from the base, so the published equity weights sum below one
        assert sum(r["weight_fund"] for r in rows) < 1.0 - 1e-6
    assert built["etfs"]["QQQ"]["equity_count"] == 100
    assert built["etfs"]["SOXX"]["equity_count"] == 30
    # the equity share is on the capping rules' basis, so it matches caps.metrics
    for etf in ("SOXX", "IGV"):
        block = built["etfs"][etf]
        assert math.isclose(block["equities"][0]["weight_eq"], block["caps"]["metrics"]["max_weight"], abs_tol=1e-12)


def test_decomposition_is_reduced_to_top_changes_and_notable(committed: dict, built: dict):
    assert built["etfs"]["QQQ"]["decomposition"] is None  # first QQQ snapshot: nothing to decompose
    for etf in ("SOXX", "IGV"):
        src = committed["etfs"][etf]["decomposition"]
        dec = built["etfs"][etf]["decomposition"]
        for key in ("prev_as_of", "curr_as_of", "mode", "scale_factor", "summary"):
            assert dec[key] == src[key], (etf, key)
        assert dec["n_changes"] == len(src["changes"])
        top = dec["top_changes"]
        assert 0 < len(top) <= summary.TOP_CHANGES
        assert all(c["asset_class"] == "equity" for c in top)
        keys = [(-round(abs(c["trade"]), 4), -abs(c["total_change"])) for c in top]
        assert keys == sorted(keys), etf
        by_ticker = {c["ticker"]: c for c in src["changes"]}
        for c in top:
            src = by_ticker[c["ticker"]]
        assert set(c) == set(summary.CHANGE_KEYS) and all(c[k] == src.get(k) for k in summary.CHANGE_KEYS)  # reduced to CHANGE_KEYS, values unchanged
        # quiet day: the largest price moves lead, and nothing is notable
        equities = [c for c in src["changes"] if c["asset_class"] == "equity"]
        assert all(round(abs(c["trade"]), 4) == 0.0 for c in equities)
        biggest = max(equities, key=lambda c: abs(c["total_change"]))
        assert top[0]["ticker"] == biggest["ticker"]
        assert dec["notable"] == [], etf
    # the cash/derivative lines labelled active_trade on the quiet day are exactly what notable leaves out
    soxx = committed["etfs"]["SOXX"]["decomposition"]["changes"]
    skipped = [c for c in soxx if c["classification"] != CLASS_FLOW_ONLY]
    assert skipped and all(c["asset_class"] != "equity" and c["classification"] == CLASS_ACTIVE_TRADE for c in skipped)


def test_written_file_is_small_canonical_json(tmp_path: Path, committed: dict, built: dict):
    path = summary.write_summary(tmp_path, committed)
    assert path == tmp_path / "reports" / "summary.json"
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert text == summary.to_json(built)
    assert json.loads(text) == built
    assert "NaN" not in text and "Infinity" not in text
    size = path.stat().st_size
    assert size < MAX_BYTES, size
    assert size < LATEST.stat().st_size / 2
    assert not path.with_suffix(".json.tmp").exists()


def test_build_summary_is_deterministic_and_pure(committed: dict, built: dict):
    before = json.dumps(committed, sort_keys=True)
    again = summary.build_summary(copy.deepcopy(committed))
    assert again == built
    assert summary.to_json(again) == summary.to_json(built)
    assert json.dumps(committed, sort_keys=True) == before  # input untouched


# ---------------------------------------------------------------------------
# Synthetic analyses
# ---------------------------------------------------------------------------


def test_notable_lists_entries_exits_and_active_trades(tmp_path: Path):
    analysis = build_analysis_all()
    out = summary.build_summary(analysis)
    igv = out["etfs"]["IGV"]["decomposition"]
    by_class = {(c["ticker"], c["classification"]) for c in igv["notable"]}
    assert ("GTLB", CLASS_ENTRY) in by_class
    assert ("DOCU", CLASS_EXIT) in by_class
    assert ("CRWD", CLASS_ACTIVE_TRADE) in by_class
    assert all(c["classification"] != CLASS_FLOW_ONLY for c in igv["notable"])
    assert igv["summary"]["entries"] == ["GTLB"] and igv["summary"]["exits"] == ["DOCU"]
    # the exit (w_curr = 0) is a real trade and leads top_changes
    assert igv["top_changes"][0]["ticker"] in {"DOCU", "GTLB", "CRWD"}
    assert len(igv["top_changes"]) == summary.TOP_CHANGES
    qqq = out["etfs"]["QQQ"]["decomposition"]
    assert ("NFLX", CLASS_CORPORATE_ACTION) in {(c["ticker"], c["classification"]) for c in qqq["notable"]}
    assert ("PLTR", CLASS_ACTIVE_TRADE) in {(c["ticker"], c["classification"]) for c in qqq["notable"]}
    soxx = out["etfs"]["SOXX"]["decomposition"]
    assert {c["ticker"] for c in soxx["notable"] if c["asset_class"] == "equity"} == {"AMD", "TXN"}
    # notable rows are the original change dicts
    changes = {c["ticker"]: c for c in analysis["etfs"]["IGV"]["decomposition"]["changes"]}
    for c in igv["notable"]:
        src = changes[c["ticker"]]
        assert set(c) == set(summary.CHANGE_KEYS) and all(c[k] == src.get(k) for k in summary.CHANGE_KEYS)
    # the file round-trips
    path = summary.write_summary(tmp_path, analysis)
    assert json.loads(path.read_text(encoding="utf-8")) == out


def test_notable_keeps_non_equity_entries_and_exits_but_not_their_daily_balance_moves():
    def change(ticker: str, asset_class: str, classification: str) -> dict:
        return {"ticker": ticker, "asset_class": asset_class, "classification": classification, "trade": 0.0, "total_change": 0.0}

    analysis = {
        "etfs": {
            "SOXX": {
                "decomposition": {
                    "changes": [
                        change("USD", "cash", CLASS_ACTIVE_TRADE),
                        change("IXTZ6", "derivative", CLASS_EXIT),
                        change("IXTH7", "derivative", CLASS_ENTRY),
                        change("AMD", "equity", CLASS_ACTIVE_TRADE),
                        change("NVDA", "equity", CLASS_FLOW_ONLY),
                    ]
                }
            }
        }
    }
    dec = summary.build_summary(analysis)["etfs"]["SOXX"]["decomposition"]
    assert [c["ticker"] for c in dec["notable"]] == ["IXTZ6", "IXTH7", "AMD"]
    assert [c["ticker"] for c in dec["top_changes"]] == ["AMD", "NVDA"]
    assert dec["n_changes"] == 5


def test_top_changes_order_rounds_trade_then_uses_total_change():
    changes = [
        {"ticker": "A", "asset_class": "equity", "trade": 0.00004, "total_change": -0.010},
        {"ticker": "B", "asset_class": "equity", "trade": -0.00003, "total_change": 0.002},
        {"ticker": "C", "asset_class": "equity", "trade": 0.0012, "total_change": 0.0001},
        {"ticker": "D", "asset_class": "equity", "trade": -0.0300, "total_change": -0.031},
        {"ticker": "E", "asset_class": "cash", "trade": 0.5, "total_change": 0.5},
    ] + [{"ticker": f"Z{i}", "asset_class": "equity", "trade": 0.0, "total_change": 0.0} for i in range(12)]
    dec = summary.build_summary({"etfs": {"X": {"decomposition": {"changes": changes}}}})["etfs"]["X"]["decomposition"]
    top = [c["ticker"] for c in dec["top_changes"]]
    assert top[:4] == ["D", "C", "A", "B"]  # A and B round to |trade| 0 and fall back to |total_change|
    assert "E" not in top and len(top) == summary.TOP_CHANGES


def test_non_finite_numbers_become_null_and_the_file_still_writes(tmp_path: Path):
    analysis = build_analysis_all()
    soxx = analysis["etfs"]["SOXX"]
    nan, inf = float("nan"), float("inf")
    soxx["holdings"][0]["price"] = nan
    soxx["holdings"][0]["market_value"] = nan  # one market value missing -> published weights are the base
    soxx["holdings"][1]["shares"] = inf
    soxx["fund"]["nav"] = nan
    soxx["decomposition"]["summary"]["gross_trade"] = nan
    soxx["decomposition"]["changes"][0]["rel_share_change"] = nan
    soxx["decomposition"]["scale_factor"] = -inf

    out = summary.build_summary(analysis)
    block = out["etfs"]["SOXX"]
    rows = {r["ticker"]: r for r in block["equities"]}
    first, second = soxx["holdings"][0]["ticker"], soxx["holdings"][1]["ticker"]
    assert "price" not in rows[first] and "market_value" not in rows[first]  # trimmed from the contract
    assert rows[second]["weight_eq"] is not None
    assert block["decomposition"]["top_changes"] and set(block["decomposition"]["top_changes"][0]) == set(summary.CHANGE_KEYS)
    assert block["fund"]["nav"] is None
    assert block["decomposition"]["summary"]["gross_trade"] is None
    assert block["decomposition"]["scale_factor"] is None
    assert math.isclose(sum(r["weight_eq"] for r in block["equities"]), 1.0, abs_tol=1e-9)
    for row in block["equities"]:
        assert math.isclose(row["weight_eq"], row["weight_fund"] / sum(r["weight_fund"] for r in block["equities"]), abs_tol=1e-12)
    text = summary.write_summary(tmp_path, analysis).read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    json.loads(text)


def test_partial_analysis_does_not_raise():
    out = summary.build_summary({"etfs": {"SOXX": {}}})
    block = out["etfs"]["SOXX"]
    assert set(block) == set(summary.ETF_KEYS)
    assert block["etf"] == "SOXX" and block["equities"] == [] and block["equity_count"] == 0
    assert block["decomposition"] is None and block["fund"] is None
    assert out["generated_utc"] is None and out["errors"] == {}
    assert summary.build_summary({}) == {"generated_utc": None, "today": None, "errors": {}, "etfs": {}}
    with pytest.raises(TypeError):
        summary.build_summary(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pipeline hook
# ---------------------------------------------------------------------------


def _report_tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted((root / "reports").rglob("*")) if p.is_file()}


def test_write_reports_writes_summary_and_is_byte_identical_on_repeat(tmp_path: Path):
    pytest.importorskip("etf_tracker.report")
    pipeline.run_fetch(tmp_path, ["SOXX", "QQQ", "IGV"], sources=fake_sources(), now=NOW)
    analysis = pipeline.run_analysis(tmp_path, ["SOXX", "QQQ", "IGV"], TODAY, now=NOW)

    paths = pipeline.write_reports(tmp_path, analysis)
    assert [p.name for p in paths] == ["latest.json", "latest.md", "2026-10-09.md", "2026-10-09.json"]  # unchanged return value
    path = tmp_path / "reports" / "summary.json"
    first = path.read_bytes()
    data = json.loads(first)
    assert set(data["etfs"]) == {"SOXX", "QQQ", "IGV"}
    assert data["generated_utc"] == analysis["generated_utc"] == "2026-10-09T14:12:03Z"
    assert data == summary.build_summary(analysis)

    pipeline.write_reports(tmp_path, analysis)
    assert path.read_bytes() == first

    # a later analysis of the same snapshots differs in the stamp only
    later = pipeline.run_analysis(tmp_path, ["SOXX", "QQQ", "IGV"], TODAY, now=NOW + timedelta(hours=4))
    pipeline.write_reports(tmp_path, later)
    scrub = re.compile(r"2026-10-09T1[48]:12:03Z")
    assert scrub.sub("@", path.read_text(encoding="utf-8")) == scrub.sub("@", first.decode("utf-8"))
    assert path.read_bytes() != first


def test_run_daily_writes_summary_next_to_the_reports(tmp_path: Path):
    pytest.importorskip("etf_tracker.report")
    outcome = pipeline.run_daily(tmp_path, ["SOXX", "QQQ", "IGV"], today=TODAY, sources=fake_sources(), now=NOW)
    assert outcome.exit_code == pipeline.EXIT_OK
    data = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert set(data["etfs"]) == {"SOXX", "QQQ", "IGV"}
    assert data["today"] == "2026-10-09"
    assert data["etfs"]["SOXX"]["equities"][0]["ticker"] == "AMD"
    before = _report_tree(tmp_path)
    second = pipeline.run_daily(tmp_path, ["SOXX", "QQQ", "IGV"], today=TODAY, sources=fake_sources(), now=NOW + timedelta(hours=4))
    assert second.reports_written is False
    assert _report_tree(tmp_path) == before  # nothing new: summary left alone too


def test_summary_failure_is_logged_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    pytest.importorskip("etf_tracker.report")
    import etf_tracker.summary as module

    def boom(root, analysis):
        raise RuntimeError("disk full")

    monkeypatch.setattr(module, "write_summary", boom)
    analysis = build_analysis_all()
    with caplog.at_level("WARNING", logger="etf_tracker.pipeline"):
        paths = pipeline.write_reports(tmp_path, analysis)
    assert len(paths) == 4 and all(p.is_file() for p in paths)
    assert not (tmp_path / "reports" / "summary.json").exists()
    assert "summary not written: RuntimeError: disk full" in caplog.text


# ---------------------------------------------------------------------------
# Adversarial review: edge cases of the equity sleeve, NaN anywhere, and the
# pipeline keeping summary.json alive
# ---------------------------------------------------------------------------


def _holding(ticker: str, weight, market_value=None, asset_class: str = "equity", **extra) -> dict:
    row = {
        "ticker": ticker,
        "name": ticker,
        "asset_class": asset_class,
        "weight": weight,
        "shares": None,
        "price": None,
        "market_value": market_value,
        "is_adr": False,
        "company_id": ticker,
    }
    row.update(extra)
    return row


def _equities_of(holdings: list[dict]) -> dict:
    return summary.build_summary({"etfs": {"X": {"holdings": holdings}}})["etfs"]["X"]


def test_weight_eq_counts_a_line_without_the_basis_input_as_zero_like_the_capping_rules():
    """One market value is missing, so the published weights are the basis; a
    line with no weight at all (or a NaN one) is 0 on that basis, as in
    ``Snapshot.normalized_weights``, never null: every weight_eq is a float
    and the sleeve sums to one."""
    holdings = [
        _holding("B", 0.3, None),
        _holding("A", 0.6, 600.0),
        _holding("D", float("nan"), None),
        _holding("C", None, None),
        _holding("USD", 0.1, 100.0, asset_class="cash"),
    ]
    block = _equities_of(holdings)
    rows = block["equities"]
    assert block["equity_count"] == 4
    assert [r["ticker"] for r in rows] == ["A", "B", "C", "D"]  # zeros last, then by ticker
    by = {r["ticker"]: r for r in rows}
    assert by["A"]["weight_eq"] == pytest.approx(0.6 / 0.9) and by["B"]["weight_eq"] == pytest.approx(0.3 / 0.9)
    assert by["C"]["weight_eq"] == 0.0 and by["C"]["weight_fund"] is None
    assert by["D"]["weight_eq"] == 0.0 and by["D"]["weight_fund"] is None
    assert all(isinstance(r["weight_eq"], float) for r in rows)
    assert math.isclose(sum(r["weight_eq"] for r in rows), 1.0, abs_tol=1e-12)
    assert "NaN" not in summary.to_json(summary.build_summary({"etfs": {"X": {"holdings": holdings}}}))


def test_weight_eq_falls_back_to_published_weights_when_market_values_cannot_be_normalised():
    # every market value is present but zero: the market-value basis is unusable, the weights are
    rows = _equities_of([_holding("A", 0.5, 0.0), _holding("B", 0.25, 0.0)])["equities"]
    assert [(r["ticker"], r["weight_eq"]) for r in rows] == [("A", pytest.approx(2 / 3)), ("B", pytest.approx(1 / 3))]
    # nothing usable at all: null, never NaN or a ZeroDivisionError
    rows = _equities_of([_holding("A", None), _holding("B", 0.0)])["equities"]
    assert [(r["ticker"], r["weight_eq"]) for r in rows] == [("A", None), ("B", None)]
    assert _equities_of([])["equities"] == []


def test_share_classes_and_duplicate_tickers_each_keep_a_row(committed: dict):
    qqq = summary.build_summary(committed)["etfs"]["QQQ"]
    tickers = [r["ticker"] for r in qqq["equities"]]
    assert "GOOGL" in tickers and "GOOG" in tickers  # two share classes, two rows (company-level caps live in caps.metrics)
    assert len(tickers) == len(set(tickers)) == qqq["equity_count"]

    # a ticker listed twice (two lines of one security) keeps both rows; the sleeve still sums to one
    # and the order depends on the rows' content only, not on the order the holdings came in
    holdings = [
        _holding("B", 0.2, 200.0),
        _holding("A", 0.3, 300.0),
        _holding("A", 0.3, 300.0, name="A second line"),
        _holding("C", 0.2, 200.0),
    ]
    out = _equities_of(holdings)
    assert out["equity_count"] == 4
    assert [(r["ticker"], r["name"]) for r in out["equities"]] == [("A", "A"), ("A", "A second line"), ("B", "B"), ("C", "C")]
    assert math.isclose(sum(r["weight_eq"] for r in out["equities"]), 1.0, abs_tol=1e-12)
    shuffled = [holdings[3], holdings[2], holdings[0], holdings[1]]
    assert _equities_of(shuffled)["equities"] == out["equities"]


def test_non_finite_values_anywhere_in_the_copied_blocks_become_null():
    nan, inf = float("nan"), float("inf")
    analysis = {
        "generated_utc": "2026-10-09T14:05:00Z",
        "today": "2026-10-09",
        "errors": {},
        "etfs": {
            "X": {
                "fund": {"nav": -inf},
                "caps": {"metrics": {"max_weight": nan}, "forced_buyers": [["A", inf]]},
                "next_events": [{"trading_days_to_reference": nan}],
                "warnings": [],
                "decomposition": {
                    "scale_factor": nan,
                    "summary": {"largest_trade_up": ["A", nan]},
                    "changes": [
                        {"ticker": "A", "asset_class": "equity", "classification": CLASS_ACTIVE_TRADE, "trade": nan, "total_change": inf, "w_prev": nan, "w_curr": 0.1},
                        {"ticker": "B", "asset_class": "equity", "classification": CLASS_FLOW_ONLY, "trade": 0.001, "total_change": 0.001},
                    ],
                },
            }
        },
    }
    out = summary.build_summary(analysis)
    text = summary.to_json(out)
    assert "NaN" not in text and "Infinity" not in text
    json.loads(text)
    x = out["etfs"]["X"]
    assert x["fund"]["nav"] is None
    assert x["caps"]["metrics"]["max_weight"] is None and x["caps"]["forced_buyers"] == [["A", None]]
    assert x["next_events"][0]["trading_days_to_reference"] is None
    dec = x["decomposition"]
    assert dec["scale_factor"] is None and dec["summary"]["largest_trade_up"] == ["A", None]
    assert [c["ticker"] for c in dec["top_changes"]] == ["B", "A"]  # a NaN trade / inf total_change sort as 0
    assert [c["ticker"] for c in dec["notable"]] == ["A"]
    assert dec["notable"][0]["trade"] is None and dec["notable"][0]["total_change"] is None


def test_reports_needed_when_only_summary_json_is_missing(tmp_path: Path):
    """The dashboard reads summary.json; a run that finds it missing must rewrite it
    even when no new snapshot was stored (same policy as reports/latest.*)."""
    pytest.importorskip("etf_tracker.report")
    analysis = build_analysis_all()
    pipeline.write_reports(tmp_path, analysis)
    needed, reason = pipeline.reports_needed(tmp_path, analysis, [])
    assert needed is False, reason
    path = tmp_path / "reports" / "summary.json"
    path.unlink()
    needed, reason = pipeline.reports_needed(tmp_path, analysis, [])
    assert needed is True and "summary.json" in reason, reason


def test_run_daily_restores_a_deleted_summary_without_new_data(tmp_path: Path):
    pytest.importorskip("etf_tracker.report")
    etfs = ["SOXX", "QQQ", "IGV"]
    pipeline.run_daily(tmp_path, etfs, today=TODAY, sources=fake_sources(), now=NOW)
    path = tmp_path / "reports" / "summary.json"
    first = path.read_bytes()
    path.unlink()
    second = pipeline.run_daily(tmp_path, etfs, today=TODAY, sources=fake_sources(), now=NOW + timedelta(hours=4))
    assert second.exit_code == pipeline.EXIT_OK
    assert second.reports_written is True and "summary.json" in second.report_reason
    assert path.is_file()
    scrub = re.compile(r"2026-10-09T1[48]:12:03Z")
    assert scrub.sub("@", path.read_text(encoding="utf-8")) == scrub.sub("@", first.decode("utf-8"))
    # with every file in place a further run leaves the reports alone
    third = pipeline.run_daily(tmp_path, etfs, today=TODAY, sources=fake_sources(), now=NOW + timedelta(hours=5))
    assert third.reports_written is False and "no new snapshot" in third.report_reason


def test_summary_is_written_even_when_history_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    pytest.importorskip("etf_tracker.report")
    import etf_tracker.history as history

    def boom(*args, **kwargs):
        raise RuntimeError("history boom")

    monkeypatch.setattr(history, "write_history", boom)
    analysis = build_analysis_all()
    with caplog.at_level("WARNING", logger="etf_tracker.pipeline"):
        paths = pipeline.write_reports(tmp_path, analysis)
    assert len(paths) == 4
    assert "history not written: RuntimeError: history boom" in caplog.text
    assert "summary not written" not in caplog.text
    data = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert set(data["etfs"]) == {"SOXX", "QQQ", "IGV"} and data["generated_utc"] == analysis["generated_utc"]


def test_cli_report_subcommand_writes_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("etf_tracker.report")
    from etf_tracker import cli

    monkeypatch.setattr(pipeline, "SOURCES", fake_sources())
    assert cli.main(["fetch", "--root", str(tmp_path)]) == 0
    assert cli.main(["report", "--root", str(tmp_path), "--today", "2026-10-09"]) == 0
    data = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert data["today"] == "2026-10-09" and set(data["etfs"]) == {"SOXX", "QQQ", "IGV"}
    assert data["etfs"]["QQQ"]["decomposition"] is None  # a single stored snapshot
