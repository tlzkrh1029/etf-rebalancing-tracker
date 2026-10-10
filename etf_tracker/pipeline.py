"""Orchestration of the daily job: fetch -> store -> analyse -> report.

Standard library only.

Source modules are looked up in :data:`SOURCES` (ETF ticker -> dotted
module path) and imported lazily, so importing this module never imports
the network code; tests pass a ``sources`` mapping of fake modules that
follow the protocol in :mod:`etf_tracker.sources`::

    ETFS: tuple[str, ...]
    fetch(etf, as_of=None, *, http_get=None) -> FetchResult

Every step is a plain function returning plain data so the CLI
(:mod:`etf_tracker.cli`) and the tests can call them one at a time:

* :func:`run_fetch` -- fetch and :func:`~etf_tracker.store.ingest` several
  ETFs for one date (``None`` = latest);
* :func:`run_backfill` -- walk the trading days of a range, oldest first,
  skipping stored dates, pausing between requests and giving up after
  repeated transport errors;
* :func:`run_analysis` -- :func:`etf_tracker.analysis.analyze_all`;
* :func:`write_reports` -- hand the analysis to :mod:`etf_tracker.report`
  when that module is available, then refresh the dashboard files
  ``reports/history.json`` and ``reports/summary.json`` (best effort);
* :func:`run_daily` -- all of the above with the exit code the workflow
  turns into a red or green run.

Exit codes of :func:`run_daily` (:data:`EXIT_OK`, :data:`EXIT_FAILED`,
:data:`EXIT_NOTHING_ANALYSED`): 0 when every ETF is ok or already up to
date, 2 when at least one ETF failed (fetch error, analysis error or
report error) but something was analysed, 3 when nothing could be analysed.

Idempotency
-----------
The daily job runs twice a day and on US market holidays, and the workflow
commits whatever changed under ``data/`` and ``reports/``.  So a run that
finds nothing new must change nothing: :func:`etf_tracker.store.ingest`
leaves the manifest alone when the state is unchanged, and
:func:`run_daily` rewrites the reports only when

* at least one ETF stored a new snapshot (any ETF refreshes every report,
  because the reports are one document), or
* ``force`` was given, or
* the latest report files or ``reports/summary.json`` are missing from
  ``reports/``.

Otherwise ``reports/latest.*`` keep describing the last run that brought
new data (their ``today``/``generated_utc`` say when that was), no dated
copy is written for the day, and the commit step finds nothing to commit.
The ``report`` subcommand of the CLI always writes.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from etf_tracker import market_calendar
from etf_tracker.analysis import analyze_all, utc_today
from etf_tracker.sources import STATUS_ERROR, STATUS_UNSUPPORTED, FetchResult
from etf_tracker.holdings import list_snapshots
from etf_tracker.store import REASON_ALREADY_STORED, IngestOutcome, has_snapshot, ingest
from etf_tracker.summary import summary_path

__all__ = [
    "DEFAULT_ETFS",
    "report_universe",
    "SOURCES",
    "EXIT_OK",
    "EXIT_FAILED",
    "EXIT_NOTHING_ANALYSED",
    "DEFAULT_PAUSE_S",
    "DEFAULT_MAX_CONSECUTIVE_ERRORS",
    "DailyOutcome",
    "resolve_source",
    "fetch_one",
    "run_fetch",
    "run_backfill",
    "run_analysis",
    "write_reports",
    "reports_needed",
    "run_daily",
]

log = logging.getLogger(__name__)

DEFAULT_ETFS: tuple[str, ...] = ("SOXX", "QQQ", "IGV")

#: ETF ticker -> source module (dotted path, imported on first use) or an
#: already imported module-like object.  Tests replace entries with fakes.
SOURCES: dict[str, str | ModuleType | Any] = {
    "SOXX": "etf_tracker.sources.ishares",
    "IGV": "etf_tracker.sources.ishares",
    "QQQ": "etf_tracker.sources.invesco",
}

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_NOTHING_ANALYSED = 3

#: Polite pause between consecutive backfill requests to one issuer.
DEFAULT_PAUSE_S = 0.5
#: Backfill stops after this many transport errors in a row.
DEFAULT_MAX_CONSECUTIVE_ERRORS = 3

SourceMap = Mapping[str, Any]
HttpGet = Callable[..., Any]


@dataclass
class DailyOutcome:
    """What :func:`run_daily` did."""

    fetch_outcomes: list[IngestOutcome] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)
    report_paths: list[Path] = field(default_factory=list)
    failed_etfs: list[str] = field(default_factory=list)
    exit_code: int = EXIT_OK
    errors: list[str] = field(default_factory=list)
    #: True when the report files were (re)written in this run.
    reports_written: bool = False
    #: Why the reports were written or left alone (see the module docstring).
    report_reason: str = ""

    @property
    def stored_etfs(self) -> list[str]:
        """ETFs for which a new snapshot was written to disk."""
        return [o.etf for o in self.fetch_outcomes if o.stored]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetch_outcomes": [o.to_dict() for o in self.fetch_outcomes],
            "analysis": self.analysis,
            "report_paths": [str(p) for p in self.report_paths],
            "reports_written": self.reports_written,
            "report_reason": self.report_reason,
            "failed_etfs": list(self.failed_etfs),
            "exit_code": self.exit_code,
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def _etf_key(etf: str) -> str:
    return str(etf).strip().upper()


def resolve_source(etf: str, sources: SourceMap | None = None) -> Any:
    """Return the source module serving ``etf`` (imported on demand).

    ``sources`` defaults to :data:`SOURCES`.  Raises ``KeyError`` when no
    source is registered and ``ImportError`` when the module is missing.
    """
    registry = SOURCES if sources is None else sources
    key = _etf_key(etf)
    try:
        spec = registry[key]
    except KeyError:
        raise KeyError(f"no data source registered for {key!r}; known: {sorted(registry)}") from None
    module = importlib.import_module(spec) if isinstance(spec, str) else spec
    if not callable(getattr(module, "fetch", None)):
        raise TypeError(f"source for {key!r} has no fetch() function")
    return module


def fetch_one(
    etf: str,
    as_of: date | None = None,
    *,
    sources: SourceMap | None = None,
    http_get: HttpGet | None = None,
) -> FetchResult:
    """Call the source's ``fetch`` and turn any exception into an ``error`` result.

    A source that raises (a bug, an unexpected document) must not abort the
    run for the other ETFs; the exception is logged with its traceback and
    reported as ``status='error'``.
    """
    key = _etf_key(etf)
    requested = as_of.isoformat() if as_of else None
    try:
        module = resolve_source(key, sources)
    except (KeyError, ImportError, TypeError) as exc:
        log.error("%s: %s", key, exc)
        return FetchResult(key, STATUS_ERROR, message=str(exc), requested_as_of=requested)
    try:
        result = module.fetch(key, as_of, http_get=http_get)
    except Exception as exc:  # noqa: BLE001 - isolate one source's failure
        log.exception("%s: source %s raised", key, getattr(module, "__name__", module))
        return FetchResult(key, STATUS_ERROR, message=f"{type(exc).__name__}: {exc}", requested_as_of=requested)
    if not isinstance(result, FetchResult):
        return FetchResult(key, STATUS_ERROR, message=f"source returned {type(result).__name__}, not FetchResult")
    return result


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def run_fetch(
    root: str | os.PathLike[str],
    etfs: Iterable[str],
    as_of: date | None = None,
    force: bool = False,
    sources: SourceMap | None = None,
    http_get: HttpGet | None = None,
    *,
    now: datetime | None = None,
) -> list[IngestOutcome]:
    """Fetch ``as_of`` (``None`` = latest) for each ETF and ingest it.

    Returns one :class:`~etf_tracker.store.IngestOutcome` per ETF in the
    order given.  Never raises for a single ETF's failure.
    """
    outcomes: list[IngestOutcome] = []
    for etf in etfs:
        key = _etf_key(etf)
        result = fetch_one(key, as_of, sources=sources, http_get=http_get)
        outcome = ingest(root, result, force=force, now=now)
        outcomes.append(outcome)
        _log_outcome(outcome)
    return outcomes


def _log_outcome(outcome: IngestOutcome) -> None:
    when = outcome.as_of.isoformat() if outcome.as_of else "-"
    if outcome.failed:
        log.error("%s %s: %s", outcome.etf, when, outcome.reason)
    elif not outcome.ok:
        log.warning("%s %s: %s", outcome.etf, when, outcome.reason)
    else:
        log.info("%s %s: %s", outcome.etf, when, outcome.reason)


def run_backfill(
    root: str | os.PathLike[str],
    etf: str,
    start: date,
    end: date,
    *,
    force: bool = False,
    sources: SourceMap | None = None,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    pause_s: float = DEFAULT_PAUSE_S,
    max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
    now: datetime | None = None,
    today: date | None = None,
) -> list[IngestOutcome]:
    """Fetch every trading day in ``[start, end]`` for one ETF, oldest first.

    Dates already stored are skipped without a request (unless ``force``).
    ``sleep(pause_s)`` runs between consecutive requests.  The loop stops
    after ``max_consecutive_errors`` transport errors in a row, and as soon
    as the source reports ``unsupported`` (it cannot serve history, so the
    remaining dates would fail the same way).  ``end`` is clamped to
    ``today`` (default: the UTC date) so a range running into the future
    does not send one doomed request per future date.  Returns one outcome
    per date visited, skipped dates included.
    """
    key = _etf_key(etf)
    if end < start:
        raise ValueError(f"backfill end {end.isoformat()} is before start {start.isoformat()}")
    today = today or utc_today()
    if end > today:
        log.warning("%s: backfill end %s is in the future, clamped to %s", key, end.isoformat(), today.isoformat())
        end = today
    outcomes: list[IngestOutcome] = []
    consecutive_errors = 0
    requests_made = 0
    day = start
    while day <= end:
        if not market_calendar.is_trading_day(day):
            day = market_calendar.next_trading_day(day)
            continue
        if has_snapshot(root, key, day) and not force:
            outcomes.append(IngestOutcome(key, day, False, REASON_ALREADY_STORED))
            log.debug("%s %s: already stored, skipped", key, day.isoformat())
            day = market_calendar.next_trading_day(day)
            continue
        if requests_made and pause_s > 0:
            sleep(pause_s)
        requests_made += 1
        result = fetch_one(key, day, sources=sources, http_get=http_get)
        outcome = ingest(root, result, force=force, now=now)
        if outcome.as_of is None:
            outcome.as_of = day
        elif outcome.as_of != day:
            log.warning(
                "%s: asked for %s, source answered with holdings as of %s (%s)",
                key,
                day.isoformat(),
                outcome.as_of.isoformat(),
                outcome.reason,
            )
        outcomes.append(outcome)
        _log_outcome(outcome)
        if outcome.status == STATUS_ERROR:
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                log.error(
                    "%s: %d consecutive errors, stopping backfill at %s",
                    key,
                    consecutive_errors,
                    day.isoformat(),
                )
                break
        else:
            consecutive_errors = 0
        if outcome.status == STATUS_UNSUPPORTED:
            log.warning("%s: source cannot serve history, stopping backfill at %s", key, day.isoformat())
            break
        day = market_calendar.next_trading_day(day)
    return outcomes


def report_universe(root: str | os.PathLike[str], requested: Iterable[str]) -> list[str]:
    """ETFs that the analysis and the report files must cover.

    Fetching may be restricted (``--etf SOXX``), but ``reports/latest.*``
    describe every tracked ETF, so a restricted run must never shrink them.
    The universe is every :data:`DEFAULT_ETFS` member that already has a
    stored snapshot under ``root`` plus whatever was requested, in
    :data:`DEFAULT_ETFS` order followed by any extra requested tickers.
    """
    req = [_etf_key(e) for e in requested]
    universe = [e for e in DEFAULT_ETFS if e in req or _has_any_snapshot(root, e)]
    universe.extend(e for e in req if e not in universe)
    return universe


def _has_any_snapshot(root: str | os.PathLike[str], etf: str) -> bool:
    try:
        return bool(list_snapshots(root, etf))
    except (OSError, ValueError):
        return False


def run_analysis(
    root: str | os.PathLike[str],
    etfs: Iterable[str],
    today: date | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """:func:`etf_tracker.analysis.analyze_all` for the given ETFs."""
    return analyze_all(root, list(etfs), today, now=now)


def write_reports(root: str | os.PathLike[str], analysis: Mapping[str, Any]) -> list[Path]:
    """Delegate to ``etf_tracker.report.write_reports(root, analysis)``.

    The report module is optional: when it cannot be imported a warning is
    logged and no files are written.  Exceptions it raises propagate.  After
    the reports, ``reports/history.json`` and ``reports/summary.json`` are
    refreshed best effort (logged, never raised); the returned paths are the
    report module's only.
    """
    try:
        report = importlib.import_module("etf_tracker.report")
    except ImportError as exc:
        log.warning("report module unavailable (%s); no reports written", exc)
        return []
    writer = getattr(report, "write_reports", None)
    if not callable(writer):
        log.warning("etf_tracker.report has no write_reports(); no reports written")
        return []
    paths = writer(Path(root), dict(analysis))
    _write_history(root, analysis)
    _write_summary(root, analysis)
    return [Path(p) for p in (paths or [])]


def _write_history(root: str | os.PathLike[str], analysis: Mapping[str, Any]) -> Path | None:
    """Refresh ``reports/history.json`` alongside the reports (best effort).

    The history covers every ETF in the analysis plus the default set, so a
    restricted run never shrinks it.  A failure is logged, never raised: the
    daily reports are already written at this point.
    """
    try:
        from etf_tracker.history import write_history

        etfs = list(DEFAULT_ETFS) + [e for e in analysis.get("etfs", {}) if e not in DEFAULT_ETFS]
        # Stamp the history with the analysis time so identical inputs give identical files.
        stamp = analysis.get("generated_utc")
        now = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if stamp else None
        return write_history(root, etfs, now=now)
    except Exception as exc:  # noqa: BLE001 - history is a convenience layer
        log.warning("history not written: %s: %s", type(exc).__name__, exc)
        return None


def _write_summary(root: str | os.PathLike[str], analysis: Mapping[str, Any]) -> Path | None:
    """Refresh ``reports/summary.json`` alongside the reports (best effort).

    The summary is a pure function of the analysis and stamped with its
    ``generated_utc`` only, so identical inputs give identical bytes.  A
    failure is logged, never raised: the daily reports are already written.
    """
    try:
        from etf_tracker.summary import write_summary

        return write_summary(root, analysis)
    except Exception as exc:  # noqa: BLE001 - the summary is a convenience layer
        log.warning("summary not written: %s: %s", type(exc).__name__, exc)
        return None


def _latest_report_paths(root: str | os.PathLike[str], analysis: Mapping[str, Any]) -> list[Path]:
    """The ``latest`` report files, as the report module lays them out.

    Falls back to ``<root>/reports/latest.{json,md}`` when the report
    module (or its ``report_paths``) is unavailable or rejects ``today``.
    """
    fallback = [Path(root) / "reports" / "latest.json", Path(root) / "reports" / "latest.md"]
    try:
        report = importlib.import_module("etf_tracker.report")
    except ImportError:
        return fallback
    locate = getattr(report, "report_paths", None)
    if not callable(locate):
        return fallback
    try:
        paths = locate(Path(root), analysis.get("today"))
        return [Path(paths["latest_json"]), Path(paths["latest_md"])]
    except Exception as exc:  # noqa: BLE001 - layout lookup must not sink the run
        log.debug("report_paths unavailable (%s); using the default layout", exc)
        return fallback


def reports_needed(
    root: str | os.PathLike[str],
    analysis: Mapping[str, Any],
    stored_etfs: Iterable[str],
    force: bool = False,
) -> tuple[bool, str]:
    """Decide whether :func:`run_daily` should (re)write the reports.

    Returns ``(needed, reason)``; see the module docstring for the policy.
    ``reports/summary.json`` counts as a report file: the dashboard reads it,
    so a run that finds it missing rewrites the set even without new data.
    """
    stored = [e for e in stored_etfs]
    if force:
        return True, "forced"
    if stored:
        return True, "new snapshot stored for " + ", ".join(stored)
    expected = _latest_report_paths(root, analysis) + [summary_path(root)]
    missing = [p for p in expected if not p.is_file()]
    if missing:
        return True, "no report on disk: " + ", ".join(p.name for p in missing)
    return False, "no new snapshot; existing reports kept"


# --------------------------------------------------------------------------
# Daily run
# --------------------------------------------------------------------------


def run_daily(
    root: str | os.PathLike[str],
    etfs: Iterable[str] = DEFAULT_ETFS,
    as_of: date | None = None,
    force: bool = False,
    today: date | None = None,
    *,
    sources: SourceMap | None = None,
    http_get: HttpGet | None = None,
    now: datetime | None = None,
    reports: bool = True,
) -> DailyOutcome:
    """Fetch, analyse and report; never raises for a single ETF's failure.

    Only ``etfs`` are fetched, but the analysis and the report files cover
    :func:`report_universe` (every tracked ETF with stored data plus
    ``etfs``), so ``--etf SOXX`` never shrinks ``reports/latest.*``.  One
    summary line per analysed ETF is logged.  See the module docstring for
    the exit codes.
    """
    etf_list = [_etf_key(e) for e in etfs]
    outcome = DailyOutcome()
    outcome.fetch_outcomes = run_fetch(root, etf_list, as_of, force, sources, http_get, now=now)

    failed: list[str] = []
    for o in outcome.fetch_outcomes:
        if o.failed:
            failed.append(o.etf)
            outcome.errors.append(f"{o.etf}: fetch {o.reason}")

    analysis_etfs = report_universe(root, etf_list)
    outcome.analysis = run_analysis(root, analysis_etfs, today, now=now)
    for etf, message in outcome.analysis.get("errors", {}).items():
        if etf not in etf_list:
            # Not requested in this run: keep it out of the exit code, but say so.
            log.warning("%s: analysis skipped (%s)", etf, message)
            continue
        if etf not in failed:
            failed.append(etf)
        outcome.errors.append(f"{etf}: analysis {message}")

    if reports and outcome.analysis.get("etfs"):
        needed, outcome.report_reason = reports_needed(root, outcome.analysis, outcome.stored_etfs, force)
        if needed:
            try:
                outcome.report_paths = write_reports(root, outcome.analysis)
                outcome.reports_written = bool(outcome.report_paths)
            except Exception as exc:  # noqa: BLE001 - data is already stored; report it, do not crash
                log.exception("report writing failed")
                outcome.errors.append(f"report: {type(exc).__name__}: {exc}")
        else:
            log.info("reports left untouched: %s", outcome.report_reason)
    elif reports:
        outcome.report_reason = "nothing analysed; no report written"

    outcome.failed_etfs = failed
    if not outcome.analysis.get("etfs"):
        outcome.exit_code = EXIT_NOTHING_ANALYSED
    elif outcome.errors:
        outcome.exit_code = EXIT_FAILED
    else:
        outcome.exit_code = EXIT_OK

    _log_summary(outcome, analysis_etfs)
    return outcome


def _log_summary(outcome: DailyOutcome, etfs: list[str]) -> None:
    fetched = {o.etf: o for o in outcome.fetch_outcomes}
    analysed = outcome.analysis.get("etfs", {})
    errors = outcome.analysis.get("errors", {})
    for etf in etfs:
        f = fetched.get(etf)
        fetch_part = f"fetch {f.status} ({f.reason})" if f else "fetch -"
        if etf in analysed:
            a = analysed[etf]
            caps = a.get("caps", {})
            analysis_part = (
                f"analysis as_of {a.get('as_of')} prev {a.get('prev_as_of') or '-'} "
                f"event {caps.get('event_type')} sellers {len(caps.get('forced_sellers', []))} "
                f"breaches {len(caps.get('breaches', []))} warnings {len(a.get('warnings', []))}"
            )
        else:
            analysis_part = f"analysis failed ({errors.get(etf, 'unknown')})"
        log.info("%s: %s; %s", etf, fetch_part, analysis_part)
    log.info(
        "daily run finished: exit %d, stored %s, failed %s, reports %s (%s)",
        outcome.exit_code,
        ",".join(outcome.stored_etfs) or "-",
        ",".join(outcome.failed_etfs) or "-",
        "written" if outcome.reports_written else "untouched",
        outcome.report_reason or "-",
    )
