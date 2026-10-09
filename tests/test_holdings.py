"""Tests for etf_tracker.holdings (data model, normalization, persistence)."""

from __future__ import annotations

import csv
import json
from datetime import date

import pytest

from etf_tracker.holdings import (
    CASH,
    EQUITY,
    Holding,
    Snapshot,
    latest_two,
    list_snapshots,
    load_snapshot,
    save_snapshot,
    snapshot_path,
    write_snapshot_csv,
)

D0 = date(2026, 10, 7)
D1 = date(2026, 10, 8)
D2 = date(2026, 10, 9)


def eq(ticker: str, shares: float | None, price: float | None, mv: float | None = None, weight: float | None = None, as_of: date = D1, etf: str = "SOXX") -> Holding:
    return Holding(etf, as_of, ticker, f"{ticker} Inc", EQUITY, shares, price, mv, weight)


def cash(mv: float, weight: float | None = None, as_of: date = D1, etf: str = "SOXX", ticker: str = "USD") -> Holding:
    return Holding(etf, as_of, ticker, "US Dollar", CASH, None, None, mv, weight)


def clean_snapshot() -> Snapshot:
    # Market values: 1000 + 2000 + 1000 = 4000 equities, + 1000 cash = 5000.
    return Snapshot(
        etf="SOXX",
        as_of=D1,
        source="test",
        holdings=[
            eq("AAA", 100.0, 10.0, 1000.0, 0.20),
            eq("BBB", 50.0, 40.0, 2000.0, 0.40),
            eq("CCC", 200.0, 5.0, 1000.0, 0.20),
            cash(1000.0, 0.20),
        ],
        meta={"shares_outstanding": 1_000_000, "url": "https://example.test/soxx"},
    )


# --------------------------------------------------------------------------
# Holding
# --------------------------------------------------------------------------


def test_holding_derives_price_and_market_value() -> None:
    h = eq("AAA", 100.0, None, 1250.0)
    assert h.derived_price() == pytest.approx(12.5)
    assert h.derived_market_value() == 1250.0

    h2 = eq("BBB", 10.0, 3.0)
    assert h2.derived_market_value() == pytest.approx(30.0)
    assert h2.derived_price() == 3.0

    assert eq("CCC", None, None).derived_price() is None
    assert eq("CCC", None, None).derived_market_value() is None
    # Zero shares must not divide by zero.
    assert eq("DDD", 0.0, None, 100.0).derived_price() is None


def test_holding_dict_round_trip_uses_iso_date() -> None:
    h = Holding("QQQ", D1, "GOOGL", "Alphabet A", EQUITY, 10.0, 150.0, 1500.0, 0.025, sector="Comm", is_adr=False, company_id="ALPHABET", source="invesco")
    d = h.to_dict()
    assert d["as_of"] == "2026-10-08"
    assert Holding.from_dict(d) == h
    # Lenient parsing of strings and missing optional keys.
    sparse = Holding.from_dict({"etf": "QQQ", "as_of": "2026-10-08", "ticker": "AAPL", "shares": "1,000", "weight": "0.1"})
    assert sparse.shares == 1000.0 and sparse.weight == 0.1 and sparse.asset_class == EQUITY
    assert sparse.price is None and sparse.currency == "USD"


# --------------------------------------------------------------------------
# Snapshot views and normalized weights
# --------------------------------------------------------------------------


def test_views() -> None:
    s = clean_snapshot()
    assert [h.ticker for h in s.equities()] == ["AAA", "BBB", "CCC"]
    assert [h.ticker for h in s.non_equities()] == ["USD"]
    assert set(s.by_ticker()) == {"AAA", "BBB", "CCC", "USD"}
    assert s.total_market_value() == pytest.approx(5000.0)
    assert s.total_market_value(include_cash=False) == pytest.approx(4000.0)


def test_normalized_weights_from_market_values_with_and_without_cash() -> None:
    s = clean_snapshot()
    w = s.normalized_weights()
    assert s.weight_basis() == "market_value"
    assert sum(w.values()) == pytest.approx(1.0)
    assert w == pytest.approx({"AAA": 0.2, "BBB": 0.4, "CCC": 0.2, "USD": 0.2})

    w_eq = s.normalized_weights(include_cash=False)
    assert "USD" not in w_eq
    assert sum(w_eq.values()) == pytest.approx(1.0)
    assert w_eq == pytest.approx({"AAA": 0.25, "BBB": 0.5, "CCC": 0.25})


def test_normalized_weights_recomputed_from_market_values_not_published_weights() -> None:
    # Published weights are stale/wrong; market values rule.
    s = Snapshot("SOXX", D1, holdings=[eq("AAA", 100.0, 10.0, weight=0.5), eq("BBB", 300.0, 10.0, weight=0.5)])
    assert s.normalized_weights() == pytest.approx({"AAA": 0.25, "BBB": 0.75})


def test_normalized_weights_fall_back_to_weights_and_renormalize() -> None:
    # No market values anywhere: published weights (summing to 0.9) are rescaled.
    s = Snapshot("IGV", D1, holdings=[eq("AAA", None, None, weight=0.6), eq("BBB", None, None, weight=0.3)])
    assert s.weight_basis() == "weight"
    w = s.normalized_weights()
    assert w == pytest.approx({"AAA": 2 / 3, "BBB": 1 / 3})

    # One line without a market value forces the weight basis for all lines.
    mixed = Snapshot("IGV", D1, holdings=[eq("AAA", 10.0, 10.0, weight=0.5), eq("BBB", None, None, weight=0.25)])
    assert mixed.weight_basis() == "weight"
    assert mixed.normalized_weights() == pytest.approx({"AAA": 2 / 3, "BBB": 1 / 3})

    # Nothing usable -> empty.
    assert Snapshot("IGV", D1, holdings=[eq("AAA", None, None)]).normalized_weights() == {}
    assert Snapshot("IGV", D1).normalized_weights() == {}


def test_normalized_weights_aggregate_duplicate_tickers() -> None:
    s = Snapshot("SOXX", D1, holdings=[eq("AAA", 10.0, 10.0), eq("AAA", 10.0, 10.0), eq("BBB", 20.0, 10.0)])
    assert s.normalized_weights() == pytest.approx({"AAA": 0.5, "BBB": 0.5})


# --------------------------------------------------------------------------
# validate()
# --------------------------------------------------------------------------


def test_validate_clean_snapshot_has_no_warnings() -> None:
    assert clean_snapshot().validate() == []


def test_validate_empty_snapshot() -> None:
    assert Snapshot("SOXX", D1).validate() == ["snapshot has no holdings"]


def test_validate_weight_sum_and_percent_detection() -> None:
    s = Snapshot("SOXX", D1, holdings=[eq("AAA", 10.0, 10.0, weight=0.5), eq("BBB", 10.0, 10.0, weight=0.3)])
    warnings = s.validate()
    assert any("weights sum to 0.8" in w for w in warnings)

    pct = Snapshot("SOXX", D1, holdings=[eq("AAA", 10.0, 10.0, weight=50.0), eq("BBB", 10.0, 10.0, weight=50.0)])
    assert any("look like percents" in w for w in pct.validate())


def test_validate_negative_shares_duplicates_and_unknown_class() -> None:
    s = Snapshot(
        "SOXX",
        D1,
        holdings=[
            eq("AAA", -5.0, 10.0),
            eq("AAA", 10.0, 10.0),
            Holding("SOXX", D1, "ZZZ", "Weird", "bond", 1.0, 1.0, 1.0, None),
        ],
    )
    warnings = s.validate()
    assert any("negative shares" in w and "AAA" in w for w in warnings)
    assert any("duplicate ticker 'AAA'" in w for w in warnings)
    assert any("unknown asset_class 'bond'" in w for w in warnings)


def test_validate_derives_missing_price_and_market_value() -> None:
    a = eq("AAA", 100.0, None, 1250.0)
    b = eq("BBB", 10.0, 3.0, None)
    s = Snapshot("SOXX", D1, holdings=[a, b])
    warnings = s.validate()
    assert a.price == pytest.approx(12.5)
    assert b.market_value == pytest.approx(30.0)
    assert any("derived price" in w and "AAA" in w for w in warnings)
    assert any("derived market_value" in w and "BBB" in w for w in warnings)


def test_validate_flags_inconsistent_market_value_and_metadata_mismatch() -> None:
    s = Snapshot(
        "SOXX",
        D1,
        holdings=[
            eq("AAA", 100.0, 10.0, 1500.0),  # shares*price = 1000 != 1500
            eq("BBB", 10.0, 10.0, 100.0, as_of=D0, etf="QQQ"),
        ],
    )
    warnings = s.validate()
    assert any("AAA" in w and "differs from shares*price" in w for w in warnings)
    assert any("BBB" in w and "holding.etf" in w for w in warnings)
    assert any("BBB" in w and "holding.as_of" in w for w in warnings)


def test_validate_flags_weight_inconsistent_with_market_value() -> None:
    s = Snapshot("SOXX", D1, holdings=[eq("AAA", 10.0, 10.0, weight=0.6), eq("BBB", 10.0, 10.0, weight=0.4)])
    warnings = s.validate()
    assert any("inconsistent with market_value/total" in w and "AAA" in w for w in warnings)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_snapshot_path_layout(tmp_path) -> None:
    p = snapshot_path(tmp_path, "soxx", D1)
    assert p == tmp_path / "data" / "normalized" / "SOXX" / "2026-10-08.json"


def test_save_and_load_round_trip(tmp_path) -> None:
    s = clean_snapshot()
    path = snapshot_path(tmp_path, s.etf, s.as_of)
    written = save_snapshot(s, path)
    assert written == path and path.is_file()
    assert not path.with_name(path.name + ".tmp").exists()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["as_of"] == "2026-10-08"
    assert raw["holdings"][0]["as_of"] == "2026-10-08"
    assert raw["meta"]["shares_outstanding"] == 1_000_000

    loaded = load_snapshot(path)
    assert loaded == s
    assert isinstance(loaded.as_of, date)
    assert all(isinstance(h.as_of, date) for h in loaded.holdings)
    assert loaded.normalized_weights() == pytest.approx(s.normalized_weights())


def test_save_snapshot_serializes_dates_in_meta(tmp_path) -> None:
    s = clean_snapshot()
    s.meta["downloaded_at"] = D2
    path = save_snapshot(s, tmp_path / "x.json")
    assert json.loads(path.read_text())["meta"]["downloaded_at"] == "2026-10-09"


def test_list_snapshots_sorted_and_ignores_other_files(tmp_path) -> None:
    assert list_snapshots(tmp_path, "SOXX") == []
    for d in (D2, D0, D1):
        s = clean_snapshot()
        s.as_of = d
        save_snapshot(s, snapshot_path(tmp_path, "SOXX", d))
    folder = snapshot_path(tmp_path, "SOXX", D0).parent
    (folder / "notes.txt").write_text("ignore me")
    (folder / "2026-10-10.json.tmp").write_text("{}")
    (folder / "2026-13-40.json").write_text("{}")  # not a real date
    assert list_snapshots(tmp_path, "soxx") == [D0, D1, D2]
    # Other ETF untouched.
    assert list_snapshots(tmp_path, "QQQ") == []


def test_latest_two(tmp_path) -> None:
    assert latest_two(tmp_path, "SOXX") is None
    first = clean_snapshot()
    first.as_of = D0
    save_snapshot(first, snapshot_path(tmp_path, "SOXX", D0))
    assert latest_two(tmp_path, "SOXX") is None  # only one snapshot

    for d in (D2, D1):
        s = clean_snapshot()
        s.as_of = d
        save_snapshot(s, snapshot_path(tmp_path, "SOXX", d))
    pair = latest_two(tmp_path, "SOXX")
    assert pair is not None
    prev, curr = pair
    assert (prev.as_of, curr.as_of) == (D1, D2)


def test_write_snapshot_csv(tmp_path) -> None:
    s = clean_snapshot()
    path = write_snapshot_csv(s, tmp_path / "out" / "soxx.csv")
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 4
    assert rows[0]["ticker"] == "AAA"
    assert float(rows[0]["weight"]) == pytest.approx(0.20)  # fraction, not percent
    assert rows[3]["shares"] == ""  # None -> empty cell


# --------------------------------------------------------------------------
# Adversarial review: renormalization with cash, derivation, JSON, imports
# --------------------------------------------------------------------------


def test_stdlib_only_imports() -> None:
    """holdings.py must not import pandas/numpy (or anything outside the stdlib)."""
    import ast
    import sys
    from pathlib import Path

    import etf_tracker.holdings as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported, "expected at least one import"
    assert not imported & {"pandas", "numpy", "requests"}
    assert all(name in sys.stdlib_module_names or name == "etf_tracker" for name in imported), imported


def test_normalized_weights_prefer_market_values_when_only_cash_lacks_one() -> None:
    # Realistic issuer file: equities carry shares and prices, the cash line
    # only a published weight.  The equities must not collapse to 0 with the
    # cash line taking 100%.
    s = Snapshot(
        "SOXX",
        D1,
        holdings=[eq("AAA", 10.0, 10.0), eq("BBB", 30.0, 10.0), cash(None, 0.02)],  # type: ignore[arg-type]
    )
    assert s.weight_basis() == "market_value"
    w = s.normalized_weights()
    assert "USD" not in w or w["USD"] == 0.0
    assert w["AAA"] == pytest.approx(0.25) and w["BBB"] == pytest.approx(0.75)
    assert sum(w.values()) == pytest.approx(1.0)
    # include_cash=False is unaffected.
    assert s.normalized_weights(include_cash=False) == pytest.approx({"AAA": 0.25, "BBB": 0.75})
    assert any("USD" in msg and "market value" in msg for msg in s.validate())


def test_normalized_weights_ignore_lines_with_no_data_at_all() -> None:
    junk = Holding("SOXX", D1, "FUT", "Index future", "derivative", None, None, None, None)
    s = Snapshot("SOXX", D1, holdings=[eq("AAA", 10.0, 10.0), eq("BBB", 30.0, 10.0), junk])
    assert s.weight_basis() == "market_value"
    w = s.normalized_weights()
    assert w["AAA"] == pytest.approx(0.25) and w["BBB"] == pytest.approx(0.75)
    assert sum(w.values()) == pytest.approx(1.0)
    # Weight basis with a junk line: still renormalizes the weighted lines.
    s2 = Snapshot("SOXX", D1, holdings=[eq("AAA", None, None, weight=0.6), eq("BBB", None, None, weight=0.2), junk])
    assert s2.weight_basis() == "weight"
    assert s2.normalized_weights() == pytest.approx({"AAA": 0.75, "BBB": 0.25, "FUT": 0.0})


def test_normalized_weights_with_cash_on_weight_basis() -> None:
    # Index-vendor style file: weights only, cash included.  Both universes
    # renormalize to exactly 1 and cash is dropped from the equity universe.
    s = Snapshot("IGV", D1, holdings=[eq("AAA", None, None, weight=0.6), eq("BBB", None, None, weight=0.3), cash(None, 0.05)])  # type: ignore[arg-type]
    w_all = s.normalized_weights(include_cash=True)
    assert sum(w_all.values()) == pytest.approx(1.0)
    assert w_all["USD"] == pytest.approx(0.05 / 0.95)
    w_eq = s.normalized_weights(include_cash=False)
    assert "USD" not in w_eq
    assert w_eq == pytest.approx({"AAA": 2 / 3, "BBB": 1 / 3})


def test_negative_cash_line_keeps_total_consistent() -> None:
    # A payable (negative cash) reduces the total; equity weights exceed their
    # gross share and everything still sums to 1.
    s = Snapshot("SOXX", D1, holdings=[eq("AAA", 100.0, 10.0), eq("BBB", 100.0, 10.0), cash(-200.0)])
    w = s.normalized_weights()
    assert s.total_market_value() == pytest.approx(1800.0)
    assert w["USD"] == pytest.approx(-200 / 1800)
    assert sum(w.values()) == pytest.approx(1.0)


def test_derived_price_edge_cases() -> None:
    assert eq("A", 0.0, None, 100.0).derived_price() is None
    assert eq("A", None, None, 100.0).derived_price() is None
    assert eq("A", 4.0, None, 100.0).derived_price() == pytest.approx(25.0)
    assert eq("A", 4.0, None, 0.0).derived_price() == 0.0
    # Explicit price always wins over market_value / shares.
    assert eq("A", 4.0, 30.0, 100.0).derived_price() == 30.0
    # Explicit market value wins over shares * price.
    assert eq("A", 4.0, 30.0, 100.0).derived_market_value() == 100.0
    # fill_derived_fields never divides by zero and leaves None alone.
    s = Snapshot("SOXX", D1, holdings=[eq("Z", 0.0, None, 100.0), eq("N", None, None, None)])
    s.fill_derived_fields()
    assert s.holdings[0].price is None and s.holdings[1].market_value is None


def test_snapshot_to_dict_is_json_ready_including_meta_dates() -> None:
    s = clean_snapshot()
    s.meta["downloaded_at"] = D2
    s.meta["nested"] = {"when": D1, "tags": ("a", "b")}
    payload = s.to_dict()
    text = json.dumps(payload)  # must not need a default= hook
    back = json.loads(text)
    assert back["meta"]["downloaded_at"] == "2026-10-09"
    assert back["meta"]["nested"]["when"] == "2026-10-08"
    assert back["meta"]["nested"]["tags"] == ["a", "b"]
    # Round trip keeps None values as None, not 0 or "".
    loaded = Snapshot.from_dict(back)
    cash_line = loaded.by_ticker()["USD"]
    assert cash_line.shares is None and cash_line.price is None and cash_line.sector is None
    assert cash_line.market_value == 1000.0
    assert loaded.as_of == D1 and all(h.as_of == D1 for h in loaded.holdings)
    assert loaded.holdings == s.holdings


def test_from_dict_is_lenient_about_missing_and_stringy_values() -> None:
    assert Snapshot.from_dict({"etf": "SOXX", "as_of": "2026-10-08", "holdings": None}).holdings == []
    assert Snapshot.from_dict({"etf": "SOXX", "as_of": "2026-10-08T00:00:00"}).as_of == D1
    base = {"etf": "SOXX", "as_of": "2026-10-08", "ticker": "A"}
    assert Holding.from_dict({**base, "is_adr": "false"}).is_adr is False
    assert Holding.from_dict({**base, "is_adr": "0"}).is_adr is False
    assert Holding.from_dict({**base, "is_adr": "true"}).is_adr is True
    assert Holding.from_dict({**base, "is_adr": 1}).is_adr is True
    assert Holding.from_dict({**base, "is_adr": None}).is_adr is None
    assert Holding.from_dict({**base, "shares": "", "weight": "nan", "price": "--"}).shares is None
    assert Holding.from_dict({**base, "weight": "nan"}).weight is None
