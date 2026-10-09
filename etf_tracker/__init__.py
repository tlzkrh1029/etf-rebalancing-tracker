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

The main entry points are re-exported here.  Note that the *function*
``etf_tracker.decompose.decompose`` is exposed at the top level as
:func:`decompose_snapshots` so that ``etf_tracker.decompose`` keeps naming
the submodule.
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

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # submodules
    "market_calendar",
    "holdings",
    "rules",
    "decompose",
    "bridge",
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
]
