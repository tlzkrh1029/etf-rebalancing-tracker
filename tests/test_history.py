"""reports/history.json: compact per-day series for dashboards."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from etf_tracker import history, pipeline
from etf_tracker.holdings import save_snapshot, snapshot_path
from test_pipeline_cli import NOW, TODAY, base_snapshot, fake_sources, on_date

D1, D2, D3 = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)


def _store(root: Path, etf: str, *dates: date) -> None:
    for d in dates:
        save_snapshot(on_date(base_snapshot(etf), d), snapshot_path(root, etf, d))


def test_build_etf_history_parallel_arrays(tmp_path: Path):
    _store(tmp_path, "SOXX", D2, D1, D3)
    block = history.build_etf_history(tmp_path, "SOXX")
    assert block["index_id"] == "SOXX"
    assert block["dates"] == ["2026-10-06", "2026-10-07", "2026-10-08"]
    for key in ("max_weight", "adr_sum", "top5_sum", "names_over_8"):
        assert len(block["metrics"][key]) == 3
    assert block["metrics"]["max_weight"][-1] > 0.09  # AMD 9.5% in the fixture
    assert block["tickers"][0] == "AMD" and len(block["tickers"]) >= history.TOP_N
    assert all(len(v) == 3 for v in block["weights"].values())
    assert len(block["shares_outstanding"]) == 3 and block["shares_outstanding"][-1] == 82_700_000.0
    assert block["limits"]["single_cap"] == 0.08 and block["limits"]["adr_cap"] == 0.10
    json.dumps(block, allow_nan=False)


def test_tickers_include_names_that_ever_breached_the_cap(tmp_path: Path):
    _store(tmp_path, "IGV", D1, D2)
    block = history.build_etf_history(tmp_path, "IGV", top_n=3)
    assert block["limits"]["single_cap"] == 0.085
    over = {t for t, series in block["weights"].items() if any(w and w > 0.085 for w in series)}
    assert {"PANW", "PLTR", "CRWD"} <= over
    assert len(block["tickers"]) <= history.MAX_TICKERS


def test_qqq_limits_and_missing_etf(tmp_path: Path):
    _store(tmp_path, "QQQ", D3)
    block = history.build_etf_history(tmp_path, "QQQ")
    assert block["limits"]["company_trigger"] == 0.24 and block["limits"]["cohort_trigger"] == 0.48
    assert "sum_over_4_5" in block["metrics"] and len(block["dates"]) == 1
    assert history.build_etf_history(tmp_path, "IGV") is None


def test_write_history_skips_empty_etfs_and_is_json(tmp_path: Path):
    _store(tmp_path, "SOXX", D3)
    path = history.write_history(tmp_path, now=datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path == tmp_path / "reports" / "history.json"
    assert list(data["etfs"]) == ["SOXX"] and data["generated_utc"] == "2026-10-09T15:00:00Z"


def test_run_daily_writes_history_next_to_the_reports(tmp_path: Path):
    outcome = pipeline.run_daily(tmp_path, ["SOXX", "QQQ", "IGV"], today=TODAY, sources=fake_sources(), now=NOW)
    assert outcome.exit_code == pipeline.EXIT_OK
    data = json.loads((tmp_path / "reports" / "history.json").read_text(encoding="utf-8"))
    assert set(data["etfs"]) == {"SOXX", "QQQ", "IGV"}
    assert data["etfs"]["SOXX"]["dates"] == ["2026-10-08"]
