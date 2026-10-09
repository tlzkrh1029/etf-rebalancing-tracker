"""A fetch restricted with --etf must never shrink the report files.

Regression test for the first Actions run (2026-10-09): ``run --etf SOXX
--force`` rewrote reports/latest.* with SOXX only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from etf_tracker import cli, pipeline
from test_pipeline_cli import AS_OF, NOW, TODAY, fake_sources


def _etfs_in_latest(root: Path) -> set[str]:
    return set(json.loads((root / "reports" / "latest.json").read_text(encoding="utf-8"))["etfs"])


def test_report_universe_grows_with_stored_snapshots(tmp_path: Path):
    registry = fake_sources()
    assert pipeline.report_universe(tmp_path, ["SOXX"]) == ["SOXX"]
    pipeline.run_fetch(tmp_path, ["QQQ"], sources=registry, now=NOW)
    assert pipeline.report_universe(tmp_path, ["SOXX"]) == ["SOXX", "QQQ"]
    pipeline.run_fetch(tmp_path, ["IGV"], sources=registry, now=NOW)
    assert pipeline.report_universe(tmp_path, []) == ["QQQ", "IGV"]
    assert pipeline.report_universe(tmp_path, ["spy", "SOXX"]) == ["SOXX", "QQQ", "IGV", "SPY"]


def test_single_etf_run_keeps_every_tracked_etf_in_the_reports(tmp_path: Path):
    registry = fake_sources()
    first = pipeline.run_daily(tmp_path, ["SOXX", "QQQ", "IGV"], today=TODAY, sources=registry, now=NOW)
    assert first.exit_code == pipeline.EXIT_OK
    assert _etfs_in_latest(tmp_path) == {"SOXX", "QQQ", "IGV"}

    forced = pipeline.run_daily(tmp_path, ["SOXX"], force=True, today=TODAY, sources=registry, now=NOW)
    assert forced.exit_code == pipeline.EXIT_OK
    assert forced.stored_etfs == ["SOXX"]
    assert set(forced.analysis["etfs"]) == {"SOXX", "QQQ", "IGV"}
    assert forced.reports_written
    assert _etfs_in_latest(tmp_path) == {"SOXX", "QQQ", "IGV"}
    assert len(registry["QQQ"].calls) == 1  # QQQ was analysed from disk, not fetched again


def test_single_etf_run_with_only_that_etf_stored_is_not_a_failure(tmp_path: Path):
    registry = fake_sources()
    outcome = pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=registry, now=NOW)
    assert outcome.exit_code == pipeline.EXIT_OK
    assert set(outcome.analysis["etfs"]) == {"SOXX"}
    assert outcome.analysis["errors"] == {}
    assert outcome.errors == []


def test_cli_report_subcommand_covers_all_tracked_etfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline, "SOURCES", fake_sources())
    assert cli.main(["run", "--root", str(tmp_path), "--today", TODAY.isoformat(), "--log-level", "WARNING"]) == 0
    assert cli.main(["report", "--root", str(tmp_path), "--etf", "SOXX", "--today", TODAY.isoformat(), "--log-level", "WARNING"]) == 0
    assert _etfs_in_latest(tmp_path) == {"SOXX", "QQQ", "IGV"}
    assert pipeline.has_snapshot(tmp_path, "SOXX", AS_OF)
