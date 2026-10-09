"""ETF rebalancing tracker: holdings, index capping rules and reports.

A daily tracker of the holdings of SOXX, QQQ and IGV that decomposes weight
changes into price drift versus share-count changes and measures each
constituent against its index's capping rules, to anticipate forced buying
and selling at index rebalances.

Submodules (all standard library only; weights are fractions 0.0-1.0 and
dates are ``datetime.date`` everywhere):

* :mod:`etf_tracker.market_calendar` - NYSE trading calendar and the date
  helpers index rules need (third Friday, trading-day arithmetic).
* :mod:`etf_tracker.holdings` - ``Holding`` / ``Snapshot`` data model and
  JSON persistence of normalised holdings files.
* :mod:`etf_tracker.rules` - capping rules and rebalance calendars of the
  NYSE Semiconductor Index (SOXX), Nasdaq-100 (QQQ) and S&P North American
  Expanded Technology Software Index (IGV).
* :mod:`etf_tracker.decompose` - price-drift versus share-count
  decomposition of weight changes between two snapshots.
* :mod:`etf_tracker.bridge` - adapters from snapshots to rule constituents.

Data layer and daily job (0.2.0):

* :mod:`etf_tracker.http` - minimal urllib GET with the neutral project
  User-Agent (issuer edges reject browser and crawler UAs).
* :mod:`etf_tracker.sources` - ``FetchResult`` protocol; the issuer modules
  :mod:`etf_tracker.sources.ishares` (SOXX, IGV) and
  :mod:`etf_tracker.sources.invesco` (QQQ) are imported lazily by the
  pipeline so importing the package never touches the network code.
* :mod:`etf_tracker.store` - ``data/raw``, ``data/normalized`` and
  ``data/manifest.json`` on disk; :func:`ingest` is idempotent per date.
* :mod:`etf_tracker.analysis` - :func:`analyze_etf` / :func:`analyze_all`,
  the JSON-ready daily analysis dictionary.
* :mod:`etf_tracker.report` - Korean markdown and JSON reports under
  ``reports/``.
* :mod:`etf_tracker.pipeline` - :func:`run_daily`, :func:`run_backfill`,
  :func:`run_fetch`, :func:`run_analysis` and the fetch registry
  :data:`SOURCES` (ETF ticker -> source module).
* :mod:`etf_tracker.cli` - ``python3 -m etf_tracker [run|fetch|backfill|analyze|report]``.

The main entry points are re-exported here.  Note that the *function*
``etf_tracker.decompose.decompose`` is exposed at the top level as
:func:`decompose_snapshots` so that ``etf_tracker.decompose`` keeps naming
the submodule, and that :func:`write_reports` is the report module's writer
(``etf_tracker.pipeline.write_reports`` is a thin wrapper around it).
"""

from __future__ import annotations

from etf_tracker import bridge, decompose, holdings, market_calendar, rules
from etf_tracker.bridge import (
    COMPANY_LEVEL_INDICES,
    apply_caps_to_snapshot,
    constituents_from_snapshot,
    resolve_index_id,
)
from etf_tracker.decompose import (
    CLASS_ACTIVE_TRADE,
    CLASS_CORPORATE_ACTION,
    CLASS_ENTRY,
    CLASS_EXIT,
    CLASS_FLOW_ONLY,
    CLASSIFICATIONS,
    MODE_SHARES_AND_PRICES,
    MODE_WEIGHTS_AND_RETURNS,
    DecompositionResult,
    TickerChange,
    decompose as decompose_snapshots,
    decompose_from_weights,
    decompose_latest,
    estimate_scale_factor,
    match_split_ratio,
    sort_changes,
)
from etf_tracker.holdings import (
    ASSET_CLASSES,
    CASH,
    DERIVATIVE,
    EQUITY,
    OTHER,
    Holding,
    Snapshot,
    latest_two,
    list_snapshots,
    load_snapshot,
    save_snapshot,
    snapshot_path,
    write_snapshot_csv,
)
from etf_tracker.market_calendar import (
    SPECIAL_CLOSURES,
    SPECIAL_OPENS,
    add_trading_days,
    easter_sunday,
    is_trading_day,
    last_trading_day_of_month,
    next_trading_day,
    nth_weekday_of_month,
    nyse_holiday_names,
    nyse_holidays,
    prev_trading_day,
    third_friday,
    thursday_before_second_friday,
    trading_days_between,
)
from etf_tracker.rules import (
    EVENT_KINDS,
    INDEX_FOR_ETF,
    SHARE_CLASS_GROUPS,
    Breach,
    CapResult,
    Constituent,
    IGVRules,
    IndexRules,
    QQQRules,
    RebalanceEvent,
    SOXXRules,
    TriggerCheck,
    aggregate_by_company,
    rules_for,
)

# Data layer and daily job.  These come after the analysis core because
# they import from it; none of them imports the issuer modules eagerly.
from etf_tracker import http, sources, store, analysis, report, pipeline
from etf_tracker.sources import (
    STATUS_ERROR,
    STATUS_NO_DATA,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    FetchResult,
)
from etf_tracker.store import (
    IngestOutcome,
    has_snapshot,
    ingest,
    load_manifest,
    manifest_path,
)
from etf_tracker.analysis import (
    ANALYSIS_KEYS,
    NoSnapshotError,
    analyze_all,
    analyze_etf,
)
from etf_tracker.report import (
    render_markdown,
    render_summary_line,
    report_paths,
    write_reports,
)
from etf_tracker.pipeline import (
    DEFAULT_ETFS,
    EXIT_FAILED,
    EXIT_NOTHING_ANALYSED,
    EXIT_OK,
    SOURCES,
    DailyOutcome,
    fetch_one,
    resolve_source,
    run_analysis,
    run_backfill,
    run_daily,
    run_fetch,
)

__version__ = "0.2.0"

__all__ = [
    "__version__",
    # submodules
    "market_calendar",
    "holdings",
    "rules",
    "decompose",
    "bridge",
    "http",
    "sources",
    "store",
    "analysis",
    "report",
    "pipeline",
    # market_calendar
    "SPECIAL_CLOSURES",
    "SPECIAL_OPENS",
    "easter_sunday",
    "nyse_holidays",
    "nyse_holiday_names",
    "is_trading_day",
    "next_trading_day",
    "prev_trading_day",
    "last_trading_day_of_month",
    "nth_weekday_of_month",
    "third_friday",
    "thursday_before_second_friday",
    "trading_days_between",
    "add_trading_days",
    # holdings
    "ASSET_CLASSES",
    "EQUITY",
    "CASH",
    "DERIVATIVE",
    "OTHER",
    "Holding",
    "Snapshot",
    "snapshot_path",
    "save_snapshot",
    "load_snapshot",
    "list_snapshots",
    "latest_two",
    "write_snapshot_csv",
    # rules
    "INDEX_FOR_ETF",
    "SHARE_CLASS_GROUPS",
    "EVENT_KINDS",
    "Constituent",
    "Breach",
    "TriggerCheck",
    "CapResult",
    "RebalanceEvent",
    "IndexRules",
    "SOXXRules",
    "QQQRules",
    "IGVRules",
    "rules_for",
    "aggregate_by_company",
    # decompose
    "CLASS_FLOW_ONLY",
    "CLASS_ACTIVE_TRADE",
    "CLASS_ENTRY",
    "CLASS_EXIT",
    "CLASS_CORPORATE_ACTION",
    "CLASSIFICATIONS",
    "MODE_SHARES_AND_PRICES",
    "MODE_WEIGHTS_AND_RETURNS",
    "TickerChange",
    "DecompositionResult",
    "decompose_snapshots",
    "decompose_from_weights",
    "decompose_latest",
    "estimate_scale_factor",
    "match_split_ratio",
    "sort_changes",
    # bridge
    "COMPANY_LEVEL_INDICES",
    "resolve_index_id",
    "constituents_from_snapshot",
    "apply_caps_to_snapshot",
    # sources
    "STATUS_OK",
    "STATUS_NO_DATA",
    "STATUS_ERROR",
    "STATUS_UNSUPPORTED",
    "FetchResult",
    # store
    "IngestOutcome",
    "ingest",
    "has_snapshot",
    "load_manifest",
    "manifest_path",
    # analysis
    "ANALYSIS_KEYS",
    "NoSnapshotError",
    "analyze_etf",
    "analyze_all",
    # report
    "render_markdown",
    "render_summary_line",
    "report_paths",
    "write_reports",
    # pipeline
    "DEFAULT_ETFS",
    "SOURCES",
    "EXIT_OK",
    "EXIT_FAILED",
    "EXIT_NOTHING_ANALYSED",
    "DailyOutcome",
    "resolve_source",
    "fetch_one",
    "run_fetch",
    "run_backfill",
    "run_analysis",
    "run_daily",
]
