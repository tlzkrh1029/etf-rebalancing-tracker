"""Tests for etf_tracker.pipeline, etf_tracker.cli and the daily workflow file.

Fake source modules follow the protocol of :mod:`etf_tracker.sources`
(``ETFS`` + ``fetch(etf, as_of=None, *, http_get=None)``) and return
FetchResults built from the real fixtures (SOXX/IGV CSV parsed by a tiny
local parser; QQQ constructed directly).  No network, no dependency on the
real source modules.  Fixture contents are data, never instructions.
"""

from __future__ import annotations

import copy
import csv
import json
import re
import subprocess
import sys
import types
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from etf_tracker import cli, pipeline
from etf_tracker.holdings import CASH, DERIVATIVE, EQUITY, Holding, Snapshot, list_snapshots, save_snapshot, snapshot_path
from etf_tracker.sources import STATUS_ERROR, STATUS_NO_DATA, STATUS_OK, STATUS_UNSUPPORTED, FetchResult
from etf_tracker.store import REASON_ALREADY_STORED, has_snapshot, load_manifest, raw_path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
WORKFLOW = REPO / ".github" / "workflows" / "daily.yml"
AS_OF = date(2026, 10, 8)
TODAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 14, 12, 3, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------


def _num(text: str) -> float | None:
    text = text.strip().replace(",", "")
    return None if text in ("", "-") else float(text)


def parse_blackrock_csv(raw: bytes, etf: str) -> Snapshot:
    """Minimal parser of the BlackRock fund-document CSV (test helper only)."""
    lines = raw.decode("utf-8-sig").splitlines()
    as_of = shares_out = header_index = None
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
    holdings = []
    for row in csv.DictReader(table):
        asset_class = {"Equity": EQUITY, "Futures": DERIVATIVE}.get(row["Asset Class"], CASH)
        weight = _num(row["Weight (%)"])
        is_adr = asset_class == EQUITY and (" ADR" in f" {row['Name']} " or row["Location"] != "United States")
        holdings.append(
            Holding(
                etf,
                as_of,
                row["Ticker"],
                row["Name"],
                asset_class,
                _num(row.get("Quantity") or row.get("Shares") or ""),
                _num(row["Price"]),
                _num(row["Market Value"]),
                None if weight is None else weight / 100.0,
                sector=row["Sector"],
                is_adr=is_adr if asset_class == EQUITY else None,
            )
        )
    return Snapshot(etf=etf, as_of=as_of, source="fixture-csv", holdings=holdings, meta={"shares_outstanding": shares_out})


def qqq_snapshot(as_of: date = AS_OF) -> Snapshot:
    weights = {"AAPL": 0.12, "MSFT": 0.11, "NVDA": 0.10, "AMZN": 0.08, "GOOGL": 0.04, "GOOG": 0.035, "META": 0.045}
    rest = 0.995 - sum(weights.values())
    for t in ("AVGO", "TSLA", "COST", "NFLX", "AMD", "PEP", "ADBE", "CSCO", "TMUS", "INTU", "QCOM", "TXN", "AMGN", "ISRG"):
        weights[t] = rest / 14
    tna = 1_000_000.0
    holdings = [Holding("QQQ", as_of, t, f"{t} Inc", EQUITY, 1000.0, w * tna / 1000.0, w * tna, w) for t, w in weights.items()]
    holdings.append(Holding("QQQ", as_of, "USD", "US Dollar", CASH, None, None, 5000.0, 0.005))
    return Snapshot("QQQ", as_of, "synthetic", holdings, {"nav": 500.0, "shares_outstanding": 2000.0, "total_net_assets": tna + 5000.0})


def base_snapshot(etf: str) -> Snapshot:
    if etf == "QQQ":
        return qqq_snapshot()
    return parse_blackrock_csv((FIXTURES / f"{etf}_holdings_2026-10-08.csv").read_bytes(), etf)


def on_date(snapshot: Snapshot, as_of: date) -> Snapshot:
    out = copy.deepcopy(snapshot)
    out.as_of = as_of
    for h in out.holdings:
        h.as_of = as_of
    return out


# ---------------------------------------------------------------------------
# Fake sources
# ---------------------------------------------------------------------------


class FakeSource:
    """A source module double: serves the fixture snapshot for any date.

    ``behaviour(etf, as_of) -> FetchResult | None`` overrides the default
    answer; ``raise_exc`` makes ``fetch`` raise.
    """

    def __init__(self, *etfs: str, behaviour: Callable[[str, date | None], FetchResult | None] | None = None, raise_exc: Exception | None = None):
        self.ETFS = tuple(etfs)
        self.__name__ = "fake_" + "_".join(etfs).lower()
        self.behaviour = behaviour
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, date | None, object]] = []

    def fetch(self, etf: str, as_of: date | None = None, *, http_get=None) -> FetchResult:
        self.calls.append((etf, as_of, http_get))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.behaviour is not None:
            answer = self.behaviour(etf, as_of)
            if answer is not None:
                return answer
        snapshot = base_snapshot(etf)
        if as_of is not None:
            snapshot = on_date(snapshot, as_of)
        raw = {".csv": f"{etf},{snapshot.as_of.isoformat()}\n".encode()} if etf != "QQQ" else {".json": b"{}", ".fund.json": b"{}"}
        return FetchResult(etf, STATUS_OK, snapshot, raw, "ok", f"fake-{etf.lower()}", as_of.isoformat() if as_of else None)


def fake_sources(**overrides: object) -> dict[str, object]:
    ishares = FakeSource("SOXX", "IGV")
    invesco = FakeSource("QQQ")
    registry: dict[str, object] = {"SOXX": ishares, "IGV": ishares, "QQQ": invesco}
    registry.update(overrides)
    return registry


def error_result(etf: str, as_of: date | None, status: str = STATUS_ERROR, message: str = "HTTP 503") -> FetchResult:
    return FetchResult(etf, status, None, {}, message, "fake", as_of.isoformat() if as_of else None)


# ---------------------------------------------------------------------------
# resolve_source / fetch_one
# ---------------------------------------------------------------------------


def test_default_registry_names_lazy_modules():
    assert pipeline.SOURCES == {
        "SOXX": "etf_tracker.sources.ishares",
        "IGV": "etf_tracker.sources.ishares",
        "QQQ": "etf_tracker.sources.invesco",
    }


def test_resolve_source_accepts_modules_and_dotted_paths():
    registry = fake_sources()
    assert pipeline.resolve_source("soxx", registry) is registry["SOXX"]
    assert pipeline.resolve_source("qqq", registry) is registry["QQQ"]
    fake = FakeSource("X")
    assert pipeline.resolve_source("x", {"X": fake}) is fake


def test_resolve_source_rejects_unknown_and_fetchless():
    with pytest.raises(KeyError):
        pipeline.resolve_source("SPY", fake_sources())
    with pytest.raises(TypeError):
        pipeline.resolve_source("X", {"X": "etf_tracker.sources"})
    with pytest.raises(ImportError):
        pipeline.resolve_source("X", {"X": "etf_tracker.sources.does_not_exist"})


def test_fetch_one_isolates_source_exceptions():
    registry = fake_sources(QQQ=FakeSource("QQQ", raise_exc=RuntimeError("kaboom")))
    result = pipeline.fetch_one("QQQ", None, sources=registry)
    assert result.status == STATUS_ERROR and "RuntimeError: kaboom" in result.message
    assert pipeline.fetch_one("SPY", None, sources=registry).status == STATUS_ERROR


def test_fetch_one_passes_http_get_through():
    registry = fake_sources()
    sentinel = object()
    pipeline.fetch_one("SOXX", AS_OF, sources=registry, http_get=sentinel)
    assert registry["SOXX"].calls == [("SOXX", AS_OF, sentinel)]


# ---------------------------------------------------------------------------
# run_fetch
# ---------------------------------------------------------------------------


def test_run_fetch_stores_all_etfs_and_is_idempotent(tmp_path: Path):
    registry = fake_sources()
    outcomes = pipeline.run_fetch(tmp_path, ["SOXX", "QQQ", "IGV"], sources=registry, now=NOW)
    assert [o.etf for o in outcomes] == ["SOXX", "QQQ", "IGV"]
    assert all(o.stored and o.ok for o in outcomes)
    for etf in ("SOXX", "QQQ", "IGV"):
        assert has_snapshot(tmp_path, etf, AS_OF)
    assert raw_path(tmp_path, "SOXX", AS_OF, ".csv").read_bytes() == b"SOXX,2026-10-08\n"
    assert raw_path(tmp_path, "QQQ", AS_OF, ".fund.json").exists()
    manifest = load_manifest(tmp_path)
    assert manifest["etfs"]["QQQ"]["latest_as_of"] == "2026-10-08"
    assert manifest["etfs"]["QQQ"]["source"] == "fake-qqq"
    assert manifest["etfs"]["SOXX"]["equity_count"] == 30

    again = pipeline.run_fetch(tmp_path, ["SOXX", "QQQ", "IGV"], sources=registry, now=NOW)
    assert all(not o.stored and o.ok and o.reason == REASON_ALREADY_STORED for o in again)
    forced = pipeline.run_fetch(tmp_path, ["SOXX"], force=True, sources=registry, now=NOW)
    assert forced[0].stored is True


def test_run_fetch_one_failure_does_not_stop_the_others(tmp_path: Path):
    registry = fake_sources(QQQ=FakeSource("QQQ", behaviour=error_result))
    outcomes = pipeline.run_fetch(tmp_path, ["SOXX", "QQQ", "IGV"], sources=registry, now=NOW)
    by_etf = {o.etf: o for o in outcomes}
    assert by_etf["QQQ"].failed and by_etf["QQQ"].reason == "error: HTTP 503"
    assert by_etf["SOXX"].stored and by_etf["IGV"].stored
    assert load_manifest(tmp_path)["etfs"]["QQQ"]["last_status"] == "error"


# ---------------------------------------------------------------------------
# run_backfill
# ---------------------------------------------------------------------------


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_run_backfill_walks_trading_days_skips_stored_and_pauses(tmp_path: Path):
    registry = fake_sources()
    stored_already = date(2026, 9, 30)
    save_snapshot(on_date(base_snapshot("SOXX"), stored_already), snapshot_path(tmp_path, "SOXX", stored_already))
    sleep = FakeSleep()
    # 2026-09-26 (Sat) .. 2026-10-05 (Mon): trading days 28, 29, 30 Sep, 1, 2, 5 Oct
    outcomes = pipeline.run_backfill(
        tmp_path, "soxx", date(2026, 9, 26), date(2026, 10, 5), sources=registry, sleep=sleep, now=NOW
    )
    visited = [o.as_of for o in outcomes]
    assert visited == [date(2026, 9, 28), date(2026, 9, 29), stored_already, date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)]
    skipped = [o for o in outcomes if not o.stored]
    assert len(skipped) == 1 and skipped[0].as_of == stored_already and skipped[0].reason == REASON_ALREADY_STORED
    requested = [c[1] for c in registry["SOXX"].calls]
    assert requested == [date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)]
    assert sleep.calls == [0.5] * 4  # between the five requests, none before the first
    assert list_snapshots(tmp_path, "SOXX") == visited


def test_run_backfill_force_refetches_stored_dates(tmp_path: Path):
    registry = fake_sources()
    d = date(2026, 10, 1)
    save_snapshot(on_date(base_snapshot("SOXX"), d), snapshot_path(tmp_path, "SOXX", d))
    outcomes = pipeline.run_backfill(tmp_path, "SOXX", d, d, force=True, sources=registry, sleep=FakeSleep(), now=NOW)
    assert outcomes[0].stored is True and len(registry["SOXX"].calls) == 1


def test_run_backfill_stops_after_three_consecutive_errors(tmp_path: Path):
    source = FakeSource("SOXX", "IGV", behaviour=error_result)
    registry = fake_sources(SOXX=source, IGV=source)
    sleep = FakeSleep()
    outcomes = pipeline.run_backfill(tmp_path, "SOXX", date(2026, 9, 1), date(2026, 9, 30), sources=registry, sleep=sleep, now=NOW)
    assert len(outcomes) == 3
    assert all(o.failed for o in outcomes)
    assert [o.as_of for o in outcomes] == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    assert len(source.calls) == 3 and sleep.calls == [0.5, 0.5]


def test_run_backfill_error_counter_resets_on_success(tmp_path: Path):
    failing = {date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 4), date(2026, 9, 8)}

    def flaky(etf: str, as_of: date | None) -> FetchResult | None:
        return error_result(etf, as_of) if as_of in failing else None

    registry = fake_sources(SOXX=FakeSource("SOXX", behaviour=flaky))
    outcomes = pipeline.run_backfill(tmp_path, "SOXX", date(2026, 9, 1), date(2026, 9, 9), sources=registry, sleep=FakeSleep(), now=NOW)
    # 1,2 fail; 3 ok; 4 fails; 7 (Labor Day, skipped) ; 8 fails; 9 ok -> never three in a row
    assert [o.as_of for o in outcomes] == [date(2026, 9, d) for d in (1, 2, 3, 4, 8, 9)]
    assert [o.stored for o in outcomes] == [False, False, True, False, False, True]


def test_run_backfill_stops_when_source_has_no_history(tmp_path: Path):
    def no_history(etf: str, as_of: date | None) -> FetchResult | None:
        return error_result(etf, as_of, STATUS_UNSUPPORTED, "invesco serves only the current snapshot")

    registry = fake_sources(QQQ=FakeSource("QQQ", behaviour=no_history))
    outcomes = pipeline.run_backfill(tmp_path, "QQQ", date(2026, 9, 1), date(2026, 9, 30), sources=registry, sleep=FakeSleep(), now=NOW)
    assert len(outcomes) == 1 and outcomes[0].status == STATUS_UNSUPPORTED and not outcomes[0].failed


def test_run_backfill_no_data_dates_continue(tmp_path: Path):
    def unpublished(etf: str, as_of: date | None) -> FetchResult | None:
        return error_result(etf, as_of, STATUS_NO_DATA, "empty template") if as_of == date(2026, 9, 2) else None

    registry = fake_sources(SOXX=FakeSource("SOXX", behaviour=unpublished))
    outcomes = pipeline.run_backfill(tmp_path, "SOXX", date(2026, 9, 1), date(2026, 9, 3), sources=registry, sleep=FakeSleep(), now=NOW)
    assert [(o.as_of, o.status, o.stored) for o in outcomes] == [
        (date(2026, 9, 1), STATUS_OK, True),
        (date(2026, 9, 2), STATUS_NO_DATA, False),
        (date(2026, 9, 3), STATUS_OK, True),
    ]


def test_run_backfill_rejects_reversed_range(tmp_path: Path):
    with pytest.raises(ValueError):
        pipeline.run_backfill(tmp_path, "SOXX", date(2026, 9, 3), date(2026, 9, 1), sources=fake_sources(), sleep=FakeSleep())


# ---------------------------------------------------------------------------
# write_reports / run_daily
# ---------------------------------------------------------------------------


@pytest.fixture
def no_report_module(monkeypatch: pytest.MonkeyPatch):
    """Make ``etf_tracker.report`` unimportable for the test."""
    monkeypatch.delitem(sys.modules, "etf_tracker.report", raising=False)
    monkeypatch.setitem(sys.modules, "etf_tracker.report", None)  # None in sys.modules -> ImportError


@pytest.fixture
def fake_report_module(monkeypatch: pytest.MonkeyPatch):
    module = types.ModuleType("etf_tracker.report")
    module.calls = []

    def write_reports(root: Path, analysis: dict) -> list[Path]:
        module.calls.append((root, analysis))
        out = Path(root) / "reports" / "latest.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ok\n", encoding="utf-8")
        (out.parent / "latest.json").write_text(json.dumps({"today": analysis.get("today")}), encoding="utf-8")
        return [out]

    module.write_reports = write_reports
    monkeypatch.setitem(sys.modules, "etf_tracker.report", module)
    return module


def test_write_reports_tolerates_missing_module(tmp_path: Path, no_report_module, caplog):
    with caplog.at_level("WARNING", logger="etf_tracker.pipeline"):
        assert pipeline.write_reports(tmp_path, {"etfs": {}}) == []
    assert "report module unavailable" in caplog.text


def test_write_reports_delegates(tmp_path: Path, fake_report_module):
    paths = pipeline.write_reports(tmp_path, {"etfs": {"SOXX": {}}})
    assert paths == [tmp_path / "reports" / "latest.md"]
    assert fake_report_module.calls[0][0] == tmp_path


def test_run_daily_all_ok_exit_0(tmp_path: Path, fake_report_module):
    registry = fake_sources()
    outcome = pipeline.run_daily(tmp_path, ["SOXX", "QQQ", "IGV"], today=TODAY, sources=registry, now=NOW)
    assert outcome.exit_code == pipeline.EXIT_OK
    assert outcome.failed_etfs == [] and outcome.errors == []
    assert set(outcome.analysis["etfs"]) == {"SOXX", "QQQ", "IGV"}
    assert outcome.analysis["errors"] == {}
    assert outcome.analysis["today"] == "2026-10-09"
    assert outcome.report_paths == [tmp_path / "reports" / "latest.md"]
    assert fake_report_module.calls[0][1]["etfs"].keys() == outcome.analysis["etfs"].keys()
    json.dumps(outcome.to_dict(), allow_nan=False)


def test_run_daily_second_run_is_up_to_date_exit_0(tmp_path: Path, no_report_module):
    registry = fake_sources()
    pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=registry, now=NOW)
    second = pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=registry, now=NOW)
    assert second.exit_code == pipeline.EXIT_OK
    assert second.fetch_outcomes[0].reason == REASON_ALREADY_STORED
    assert second.report_paths == []


def test_run_daily_one_failed_etf_exit_2(tmp_path: Path, no_report_module):
    registry = fake_sources(QQQ=FakeSource("QQQ", behaviour=error_result))
    outcome = pipeline.run_daily(tmp_path, ["SOXX", "QQQ", "IGV"], today=TODAY, sources=registry, now=NOW)
    assert outcome.exit_code == pipeline.EXIT_FAILED
    assert outcome.failed_etfs == ["QQQ"]
    assert set(outcome.analysis["etfs"]) == {"SOXX", "IGV"}
    assert "QQQ" in outcome.analysis["errors"]
    assert any(e.startswith("QQQ: fetch error") for e in outcome.errors)


def test_run_daily_failed_fetch_but_stored_history_still_analyses(tmp_path: Path, no_report_module):
    pipeline.run_daily(tmp_path, ["QQQ"], today=TODAY, sources=fake_sources(), now=NOW)
    registry = fake_sources(QQQ=FakeSource("QQQ", behaviour=error_result))
    outcome = pipeline.run_daily(tmp_path, ["QQQ"], today=TODAY, sources=registry, now=NOW)
    assert outcome.exit_code == pipeline.EXIT_FAILED  # the fetch failed even though analysis ran on yesterday's data
    assert "QQQ" in outcome.analysis["etfs"]


def test_run_daily_nothing_analysed_exit_3(tmp_path: Path, no_report_module):
    source = FakeSource("SOXX", "IGV", behaviour=error_result)
    registry = fake_sources(SOXX=source, IGV=source, QQQ=FakeSource("QQQ", behaviour=error_result))
    outcome = pipeline.run_daily(tmp_path, ["SOXX", "QQQ", "IGV"], today=TODAY, sources=registry, now=NOW)
    assert outcome.exit_code == pipeline.EXIT_NOTHING_ANALYSED
    assert sorted(outcome.failed_etfs) == ["IGV", "QQQ", "SOXX"]
    assert outcome.analysis["etfs"] == {}


def test_run_daily_no_data_is_not_a_failure(tmp_path: Path, no_report_module):
    pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=fake_sources(), now=NOW)
    holiday = FakeSource("SOXX", "IGV", behaviour=lambda e, d: error_result(e, d, STATUS_NO_DATA, "empty template"))
    outcome = pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=fake_sources(SOXX=holiday), now=NOW)
    assert outcome.exit_code == pipeline.EXIT_OK
    assert outcome.fetch_outcomes[0].status == STATUS_NO_DATA


def test_run_daily_report_exception_is_recorded_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = types.ModuleType("etf_tracker.report")

    def boom(root, analysis):
        raise RuntimeError("template missing")

    module.write_reports = boom
    monkeypatch.setitem(sys.modules, "etf_tracker.report", module)
    outcome = pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=fake_sources(), now=NOW)
    assert outcome.exit_code == pipeline.EXIT_FAILED
    assert outcome.failed_etfs == []
    assert outcome.errors == ["report: RuntimeError: template missing"]
    assert has_snapshot(tmp_path, "SOXX", AS_OF)  # data was stored before the report failed


def test_run_daily_logs_one_summary_line_per_etf(tmp_path: Path, no_report_module, caplog):
    with caplog.at_level("INFO", logger="etf_tracker.pipeline"):
        pipeline.run_daily(tmp_path, ["SOXX", "QQQ"], today=TODAY, sources=fake_sources(), now=NOW)
    summary = [r.getMessage() for r in caplog.records if "analysis as_of" in r.getMessage()]
    assert len(summary) == 2
    assert summary[0].startswith("SOXX: fetch ok (stored); analysis as_of 2026-10-08")
    assert any("daily run finished: exit 0" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_normalize_argv_inserts_default_subcommand():
    assert cli._normalize_argv([]) == ["run"]
    assert cli._normalize_argv(["--root", "x", "--json"]) == ["run", "--root", "x", "--json"]
    assert cli._normalize_argv(["fetch", "--etf", "SOXX"]) == ["fetch", "--etf", "SOXX"]
    assert cli._normalize_argv(["--help"]) == ["--help"]


def test_cli_run_with_json_exit_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_report_module, capsys):
    monkeypatch.setattr(pipeline, "SOURCES", fake_sources())
    code = cli.main(["run", "--root", str(tmp_path), "--today", "2026-10-09", "--json", "--log-level", "WARNING"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["etfs"]) == {"SOXX", "QQQ", "IGV"}
    assert out["etfs"]["SOXX"]["as_of"] == "2026-10-08"
    assert out["etfs"]["QQQ"]["caps"]["special_rebalance"] is not None
    assert has_snapshot(tmp_path, "IGV", AS_OF)


def test_cli_default_subcommand_is_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_report_module):
    monkeypatch.setattr(pipeline, "SOURCES", fake_sources())
    assert cli.main(["--root", str(tmp_path), "--etf", "soxx", "--today", "2026-10-09"]) == 0
    assert has_snapshot(tmp_path, "SOXX", AS_OF) and not has_snapshot(tmp_path, "QQQ", AS_OF)


def test_cli_run_exit_codes_2_and_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_report_module):
    monkeypatch.setattr(pipeline, "SOURCES", fake_sources(QQQ=FakeSource("QQQ", behaviour=error_result)))
    assert cli.main(["run", "--root", str(tmp_path), "--today", "2026-10-09"]) == 2
    failing = FakeSource("SOXX", "IGV", behaviour=error_result)
    monkeypatch.setattr(pipeline, "SOURCES", {"SOXX": failing, "IGV": failing, "QQQ": FakeSource("QQQ", behaviour=error_result)})
    empty = tmp_path / "empty"
    assert cli.main(["run", "--root", str(empty), "--today", "2026-10-09"]) == 3


def test_cli_fetch_and_analyze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(pipeline, "SOURCES", fake_sources())
    assert cli.main(["fetch", "--root", str(tmp_path), "--etf", "SOXX", "--etf", "QQQ", "--json"]) == 0
    outcomes = json.loads(capsys.readouterr().out)
    assert [o["etf"] for o in outcomes] == ["SOXX", "QQQ"] and all(o["stored"] for o in outcomes)
    assert cli.main(["analyze", "--root", str(tmp_path), "--etf", "SOXX", "--today", "2026-10-09", "--json"]) == 0
    analysis = json.loads(capsys.readouterr().out)
    assert list(analysis["etfs"]) == ["SOXX"] and analysis["errors"] == {}
    # IGV was never fetched: analysing all three reports a partial failure
    assert cli.main(["analyze", "--root", str(tmp_path), "--today", "2026-10-09"]) == 2


def test_cli_analyze_without_data_exit_3(tmp_path: Path):
    assert cli.main(["analyze", "--root", str(tmp_path)]) == 3


def test_cli_backfill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    registry = fake_sources()
    monkeypatch.setattr(pipeline, "SOURCES", registry)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    code = cli.main(["backfill", "--root", str(tmp_path), "--etf", "SOXX", "--start", "2026-10-01", "--end", "2026-10-06", "--json", "--pause", "0"])
    assert code == 0
    outcomes = json.loads(capsys.readouterr().out)
    assert [o["as_of"] for o in outcomes] == ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]
    assert list_snapshots(tmp_path, "SOXX") == [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)]


def test_cli_backfill_usage_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline, "SOURCES", fake_sources())
    assert cli.main(["backfill", "--root", str(tmp_path), "--start", "2026-10-01", "--end", "2026-10-02"]) == 1
    assert cli.main(["backfill", "--root", str(tmp_path), "--etf", "SOXX", "--etf", "IGV", "--start", "2026-10-01", "--end", "2026-10-02"]) == 1
    assert cli.main(["backfill", "--root", str(tmp_path), "--etf", "SOXX", "--start", "2026-10-02", "--end", "2026-10-01"]) == 1
    with pytest.raises(SystemExit) as exc:
        cli.main(["backfill", "--root", str(tmp_path), "--etf", "SOXX", "--start", "2026/10/01", "--end", "2026-10-02"])
    assert exc.value.code == cli.EXIT_USAGE == 1  # never 2: that means "some ETF failed"
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--bogus"])
    assert exc.value.code == 1


def test_cli_report_subcommand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_report_module):
    monkeypatch.setattr(pipeline, "SOURCES", fake_sources())
    assert cli.main(["fetch", "--root", str(tmp_path)]) == 0
    assert cli.main(["report", "--root", str(tmp_path), "--today", "2026-10-09"]) == 0
    assert (tmp_path / "reports" / "latest.md").read_text(encoding="utf-8") == "ok\n"


def test_cli_subprocess_module_entry_point(tmp_path: Path):
    env = {"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "etf_tracker", "analyze", "--root", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 3, proc.stderr
    payload = json.loads(proc.stdout)
    assert set(payload["errors"]) == {"SOXX", "QQQ", "IGV"}
    helped = subprocess.run([sys.executable, "-m", "etf_tracker", "--help"], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)
    assert helped.returncode == 0
    assert "{run,fetch,backfill,analyze,report}" in helped.stdout
    assert "보유 내역" in helped.stdout  # Korean help text for the owner


# ---------------------------------------------------------------------------
# Workflow file
# ---------------------------------------------------------------------------


def test_daily_workflow_file_has_the_required_pieces():
    text = WORKFLOW.read_text(encoding="utf-8")
    for needle in (
        "name: daily-holdings",
        "workflow_dispatch",
        "cron: '10 14 * * 1-5'",
        "cron: '10 18 * * 1-5'",
        "contents: write",
        "group: daily-holdings",
        "cancel-in-progress: false",
        "timeout-minutes: 30",
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "python-version: '3.12'",
        "python3 -m pytest -q",
        "python3 -m etf_tracker run --root .",
        "python3 -m etf_tracker backfill --root .",
        "github-actions[bot]",
        "git pull --rebase origin \"$GITHUB_REF_NAME\"",
        "git rebase --abort",
        "git push origin \"HEAD:$GITHUB_REF_NAME\"",
        "steps.run.outputs.exit_code != '0'",
        "backfill_start",
        "backfill_end",
        "as_of",
        "force",
    ):
        assert needle in text, needle
    # the comment block explaining schedule / UTC / idempotency comes first
    assert text.lstrip().startswith("#")
    head = text.split("name: daily-holdings")[0]
    assert "UTC" in head and "idempotent" in head
    # the commit step runs even when the tracker failed, and before the failing step
    assert text.index("Commit data and reports") < text.index("Fail the job when the tracker reported an error")
    assert "if: always()" in text


# ---------------------------------------------------------------------------
# Idempotency: a second run must change nothing under data/ or reports/
# ---------------------------------------------------------------------------


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_run_daily_twice_changes_nothing_and_skips_the_reports(tmp_path: Path, fake_report_module):
    registry = fake_sources()
    first = pipeline.run_daily(tmp_path, ["SOXX", "QQQ", "IGV"], today=TODAY, sources=registry, now=NOW)
    assert first.reports_written is True and first.report_paths
    data_before = _tree(tmp_path / "data")
    reports_before = _tree(tmp_path / "reports")
    assert len(fake_report_module.calls) == 1

    second = pipeline.run_daily(tmp_path, ["SOXX", "QQQ", "IGV"], today=TODAY, sources=registry, now=NOW + timedelta(hours=4))
    assert second.exit_code == pipeline.EXIT_OK
    assert all(o.reason == REASON_ALREADY_STORED for o in second.fetch_outcomes)
    assert second.reports_written is False and second.report_paths == []
    assert "no new snapshot" in second.report_reason
    assert len(fake_report_module.calls) == 1  # report module not called again
    assert _tree(tmp_path / "data") == data_before
    assert _tree(tmp_path / "reports") == reports_before
    assert "reports_written" in second.to_dict() and "report_reason" in second.to_dict()


def test_run_daily_rewrites_reports_on_force_new_data_or_missing_files(tmp_path: Path, fake_report_module):
    registry = fake_sources()
    pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=registry, now=NOW)
    assert len(fake_report_module.calls) == 1

    forced = pipeline.run_daily(tmp_path, ["SOXX"], force=True, today=TODAY, sources=registry, now=NOW)
    assert forced.reports_written is True and len(fake_report_module.calls) == 2

    (tmp_path / "reports" / "latest.json").unlink()
    repaired = pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=registry, now=NOW)
    assert repaired.reports_written is True and len(fake_report_module.calls) == 3
    assert "no report" in repaired.report_reason

    # a new as-of date for one ETF is enough to refresh every report
    newer = date(2026, 10, 9)
    registry["SOXX"].behaviour = lambda e, d: FetchResult(e, STATUS_OK, on_date(base_snapshot(e), newer), {".csv": b"x"}, "ok", "fake")
    fresh = pipeline.run_daily(tmp_path, ["SOXX"], today=date(2026, 10, 12), sources=registry, now=NOW)
    assert fresh.fetch_outcomes[0].stored and fresh.reports_written is True
    assert len(fake_report_module.calls) == 4
    assert "stored" in fresh.report_reason


def test_run_daily_failed_fetch_with_fresh_reports_leaves_them_alone(tmp_path: Path, fake_report_module):
    pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=fake_sources(), now=NOW)
    registry = fake_sources(SOXX=FakeSource("SOXX", "IGV", behaviour=error_result))
    outcome = pipeline.run_daily(tmp_path, ["SOXX"], today=TODAY, sources=registry, now=NOW)
    assert outcome.exit_code == pipeline.EXIT_FAILED
    assert outcome.reports_written is False and len(fake_report_module.calls) == 1


def test_reports_of_the_same_data_differ_only_in_generated_utc(tmp_path: Path):
    """The real report module twice on the same snapshots: deterministic output."""
    report = pytest.importorskip("etf_tracker.report")
    pipeline.run_fetch(tmp_path, ["SOXX", "QQQ", "IGV"], sources=fake_sources(), now=NOW)
    first = pipeline.run_analysis(tmp_path, ["SOXX", "QQQ", "IGV"], TODAY, now=NOW)
    paths = pipeline.write_reports(tmp_path, first)
    assert paths and all(p.is_file() for p in paths)
    before = _tree(tmp_path / "reports")
    second = pipeline.run_analysis(tmp_path, ["SOXX", "QQQ", "IGV"], TODAY, now=NOW + timedelta(hours=4))
    pipeline.write_reports(tmp_path, second)
    after = _tree(tmp_path / "reports")
    assert set(before) == set(after)
    scrub = re.compile(r"2026-10-09[T ]1[48]:12:03Z?")
    for name in before:
        a = scrub.sub("@", before[name].decode("utf-8"))
        b = scrub.sub("@", after[name].decode("utf-8"))
        assert a != before[name].decode("utf-8")  # the timestamp really is in the file
        assert a == b, name
    assert first["etfs"] == second["etfs"]
    del report


# ---------------------------------------------------------------------------
# Backfill guards
# ---------------------------------------------------------------------------


def test_run_backfill_clamps_the_range_to_today(tmp_path: Path):
    """A range running into the future must not send one request per future date."""
    registry = fake_sources()
    outcomes = pipeline.run_backfill(
        tmp_path, "SOXX", date(2026, 10, 6), date(2027, 12, 31), sources=registry, sleep=FakeSleep(), now=NOW, today=TODAY
    )
    assert [o.as_of for o in outcomes] == [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]
    assert len(registry["SOXX"].calls) == 4


def test_run_backfill_warns_when_the_source_answers_with_another_date(tmp_path: Path, caplog):
    def shifted(etf: str, as_of: date | None) -> FetchResult | None:
        snap = on_date(base_snapshot(etf), AS_OF)  # always 2026-10-08 whatever was asked
        return FetchResult(etf, STATUS_OK, snap, {".csv": b"x"}, "ok", "fake", as_of.isoformat() if as_of else None)

    registry = fake_sources(SOXX=FakeSource("SOXX", behaviour=shifted))
    with caplog.at_level("WARNING", logger="etf_tracker.pipeline"):
        outcomes = pipeline.run_backfill(tmp_path, "SOXX", date(2026, 10, 6), date(2026, 10, 7), sources=registry, sleep=FakeSleep(), now=NOW)
    assert [o.as_of for o in outcomes] == [AS_OF, AS_OF]
    assert outcomes[0].stored and not outcomes[1].stored
    assert any("2026-10-06" in r.getMessage() and "2026-10-08" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Exit codes through python -m etf_tracker
# ---------------------------------------------------------------------------


def test_cli_subprocess_propagates_partial_failure_and_usage_codes(tmp_path: Path):
    env = {"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}
    save_snapshot(base_snapshot("SOXX"), snapshot_path(tmp_path, "SOXX", AS_OF))

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "etf_tracker", *args], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)

    ok = run("analyze", "--root", str(tmp_path), "--etf", "SOXX", "--today", "2026-10-09")
    assert ok.returncode == 0, ok.stderr
    partial = run("analyze", "--root", str(tmp_path), "--today", "2026-10-09")  # QQQ and IGV have no snapshot
    assert partial.returncode == 2, partial.stderr
    usage = run("backfill", "--root", str(tmp_path), "--start", "2026-10-01", "--end", "2026-10-02")
    assert usage.returncode == 1, usage.stderr
    syntax = run("analyze", "--today", "not-a-date")
    assert syntax.returncode == 1, syntax.stderr


# ---------------------------------------------------------------------------
# Workflow structure
# ---------------------------------------------------------------------------


_CRON_RE = re.compile(r"cron: '(\d{1,2}) (\d{1,2}) \* \* 1-5'")


def test_daily_workflow_crons_are_weekday_utc_after_publication():
    text = WORKFLOW.read_text(encoding="utf-8")
    crons = [(int(m), int(h)) for m, h in _CRON_RE.findall(text)]
    assert crons == [(10, 14), (10, 18)]
    for minute, hour in crons:
        assert 0 <= minute < 60 and 13 < hour < 24  # after ~13:10 UTC publication, before midnight UTC


def test_daily_workflow_structure_when_yaml_is_available():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))  # PyYAML reads the bare key `on` as boolean True
    assert [c["cron"] for c in on["schedule"]] == ["10 14 * * 1-5", "10 18 * * 1-5"]
    assert set(on["workflow_dispatch"]["inputs"]) == {"etf", "as_of", "backfill_start", "backfill_end", "force"}
    assert wf["permissions"] == {"contents": "write"}
    assert wf["concurrency"] == {"group": "daily-holdings", "cancel-in-progress": False}
    job = wf["jobs"]["daily"]
    assert job["runs-on"] == "ubuntu-latest"
    names = [s.get("name") or s.get("uses") for s in job["steps"]]
    assert names == [
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "Install test dependencies",
        "Run unit tests",
        "Fetch, analyse and report",
        "Commit data and reports",
        "Fail the job when the tracker reported an error",
    ]
    by_name = {(s.get("name") or s.get("uses")): s for s in job["steps"]}
    assert by_name["Fetch, analyse and report"]["id"] == "run"
    assert by_name["Commit data and reports"]["if"] == "always()"
    assert by_name["Fail the job when the tracker reported an error"]["if"] == "steps.run.outputs.exit_code != '0'"
    assert by_name["actions/setup-python@v5"]["with"]["python-version"] == "3.12"
    run_script = by_name["Fetch, analyse and report"]["run"]
    assert "set +e" in run_script and 'echo "exit_code=$exit_code" >> "$GITHUB_OUTPUT"' in run_script
    assert run_script.rstrip().endswith("exit 0")
    commit_script = by_name["Commit data and reports"]["run"]
    assert "nothing to commit" in commit_script and "exit 0" in commit_script
    assert commit_script.index("git rebase --abort") > commit_script.index("git pull --rebase")
    # no secrets are referenced anywhere: the default GITHUB_TOKEN is enough
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")
