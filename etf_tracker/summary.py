"""Compact daily summary for dashboards: ``reports/summary.json``.

``reports/latest.json`` carries every holding and every decomposition row
(about 220 KB), more than a page that reads repository files through a
GitHub connector can take in one piece.  This module reduces the analysis
dictionary of :func:`etf_tracker.analysis.analyze_all` to what a dashboard
renders.  :func:`build_summary` is a pure function of that dictionary (no
disk reads, nothing time-dependent beyond the analysis's own
``generated_utc``), so the same analysis always yields a byte-identical
file.

Shape (keys fixed; ``null`` where a value is unavailable)::

    {"generated_utc", "today", "errors",
     "etfs": {"<ETF>": {
        "etf", "index_id", "as_of", "prev_as_of", "source",
        "fund":          as in the analysis,
        "equity_count":  number of equity holdings,
        "equities":      [{"ticker", "name", "weight_eq", "weight_fund", "is_adr",
                           }, ...]  weight_eq desc,
        "caps":          as in the analysis,
        "next_events":   as in the analysis,
        "decomposition": null | {"prev_as_of", "curr_as_of", "mode", "scale_factor",
                                 "summary":     as in the analysis,
                                 "top_changes": up to TOP_CHANGES equity change rows (CHANGE_KEYS fields only),
                                 "notable":     change rows worth attention,
                                 "n_changes":   number of change rows},
        "warnings":      as in the analysis}}}

``weight_fund`` is the holding's weight as published.  ``weight_eq`` is its
share of the equity sleeve on the basis the capping rules use
(:meth:`etf_tracker.holdings.Snapshot.normalized_weights`: market values
when every equity line has one and they add up to a positive total,
otherwise the published weights), so it is comparable with ``caps.metrics``
and sums to one per ETF.  As there, a line without the chosen input counts
as 0; ``weight_eq`` is ``null`` only when the whole sleeve has no usable
basis.  Rows are ordered by ``weight_eq`` descending, then ticker, then name,
so the order depends on the rows' content only.

``top_changes`` are the equity rows of the decomposition ordered by
``|trade|`` rounded to :data:`TRADE_DECIMALS` decimals descending, then
``|total_change|`` descending: a rebalance day leads with the real trades,
a quiet day with the largest price moves.  The rows are the
``TickerChange`` dictionaries unchanged.

``notable`` keeps the changes whose classification is not ``flow_only``,
with one exception: the ``active_trade`` label on cash and derivative lines.
On a rebalance day almost every constituent is an active trade, so the list
is capped at :data:`NOTABLE_MAX` rows: entries, exits and corporate-action
suspects are always kept, then the active trades with the largest rounded
``|trade|`` (then ``|total_change|``); the kept rows stay in source order
and ``n_notable`` counts the rows that qualified before the cap.
Those balances move every day (dividends, payables, margin) for reasons that
are not trades, which is why :mod:`etf_tracker.decompose` leaves them out of
its own active-trade statistics; listing them would make every quiet day
look busy.  Entries, exits and corporate-action suspects of any asset class
stay (a futures roll shows up as an exit and an entry).

Weights are fractions, dates ISO strings, floats plain floats (non-finite
values become ``null``).  Stdlib only.
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from etf_tracker.analysis import json_safe
from etf_tracker.decompose import CLASS_ACTIVE_TRADE, CLASS_FLOW_ONLY
from etf_tracker.holdings import EQUITY

__all__ = [
    "SUMMARY_RELPATH",
    "TOP_CHANGES",
    "NOTABLE_MAX",
    "TRADE_DECIMALS",
    "TOP_LEVEL_KEYS",
    "ETF_KEYS",
    "EQUITY_KEYS",
    "DECOMPOSITION_KEYS",
    "summary_path",
    "build_summary",
    "to_json",
    "write_summary",
]

log = logging.getLogger(__name__)

SUMMARY_RELPATH = Path("reports") / "summary.json"
#: How many equity change rows ``decomposition.top_changes`` holds at most.
TOP_CHANGES = 10
#: How many rows ``decomposition.notable`` holds at most (see the module
#: docstring for which rows survive the cap).
NOTABLE_MAX = 30
#: ``|trade|`` is rounded to this many decimals (of weight fraction) before
#: ordering ``top_changes``, so sub-basis-point noise does not outrank a
#: larger price move.
TRADE_DECIMALS = 4

TOP_LEVEL_KEYS: tuple[str, ...] = ("generated_utc", "today", "errors", "etfs")
ETF_KEYS: tuple[str, ...] = (
    "etf",
    "index_id",
    "as_of",
    "prev_as_of",
    "source",
    "fund",
    "equity_count",
    "equities",
    "caps",
    "next_events",
    "decomposition",
    "warnings",
)
EQUITY_KEYS: tuple[str, ...] = ("ticker", "name", "weight_eq", "weight_fund", "is_adr")
#: Fields kept per change row in ``top_changes`` / ``notable`` (the dashboard needs no more;
#: the full TickerChange rows stay in reports/latest.json).
CHANGE_KEYS: tuple[str, ...] = (
    "ticker", "name", "asset_class", "classification", "w_prev", "w_curr", "total_change",
    "drift", "trade", "shares_prev", "shares_curr", "rel_share_change", "price_return",
)
DECOMPOSITION_KEYS: tuple[str, ...] = (
    "prev_as_of",
    "curr_as_of",
    "mode",
    "scale_factor",
    "summary",
    "top_changes",
    "notable",
    "n_notable",
    "n_changes",
)


def summary_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / SUMMARY_RELPATH


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    """``value`` as a finite float, else ``None`` (bools and strings are not numbers)."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    """The mapping items of a list-like value (anything else is skipped)."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _is_equity(row: Mapping[str, Any]) -> bool:
    # A row without an asset class is an equity line (same reading as the report).
    return row.get("asset_class", EQUITY) == EQUITY


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------


def _equity_shares(rows: Sequence[Mapping[str, Any]]) -> list[float] | None:
    """Each row's share of the equity sleeve, or ``None`` when nothing is usable.

    Market values are the basis when every row has one and they add up to a
    positive finite total; otherwise the published weights, under the same
    condition (:meth:`etf_tracker.holdings.Snapshot.normalized_weights`).  A
    row without the chosen input counts as 0.
    """
    market_values = [_finite(h.get("market_value")) for h in rows]
    weights = [_finite(h.get("weight")) for h in rows]
    candidates = [market_values] if rows and all(mv is not None for mv in market_values) else []
    candidates.append(weights)
    for values in candidates:
        total = sum(v for v in values if v is not None)
        if math.isfinite(total) and total > 0:
            return [(v or 0.0) / total for v in values]
    return None


def _equities_block(holdings: Any) -> list[dict[str, Any]]:
    """Equity holdings with their share of the equity sleeve, largest first."""
    rows = [h for h in _rows(holdings) if _is_equity(h)]
    shares = _equity_shares(rows)

    out: list[dict[str, Any]] = []
    for index, holding in enumerate(rows):
        out.append(
            {
                "ticker": holding.get("ticker"),
                "name": holding.get("name"),
                "weight_eq": shares[index] if shares is not None else None,
                "weight_fund": _finite(holding.get("weight")),
                "is_adr": bool(holding.get("is_adr")),
            }
        )
    out.sort(key=lambda r: (r["weight_eq"] is None, -(r["weight_eq"] or 0.0), str(r["ticker"]), str(r["name"])))
    return out


def _total_change(change: Mapping[str, Any]) -> float:
    total = _finite(change.get("total_change"))
    if total is None:
        w_prev, w_curr = _finite(change.get("w_prev")), _finite(change.get("w_curr"))
        total = (w_curr - w_prev) if w_prev is not None and w_curr is not None else 0.0
    return total


def _top_change_key(change: Mapping[str, Any]) -> tuple[float, float, str]:
    trade = _finite(change.get("trade")) or 0.0
    return (-round(abs(trade), TRADE_DECIMALS), -abs(_total_change(change)), str(change.get("ticker")))


def _is_notable(change: Mapping[str, Any]) -> bool:
    classification = change.get("classification")
    if classification is None or classification == CLASS_FLOW_ONLY:
        return False
    if _is_equity(change):
        return True
    # Cash and derivative balances move every day; only their entries, exits
    # and corporate-action suspects are worth a dashboard's attention.
    return classification != CLASS_ACTIVE_TRADE


def _notable_key(change: Mapping[str, Any]) -> tuple[int, float, float]:
    """Rank for the cap: entries/exits/corporate actions first, then the biggest trades."""
    active = 1 if change.get("classification") == CLASS_ACTIVE_TRADE else 0
    trade = _finite(change.get("trade")) or 0.0
    total = _finite(change.get("total_change")) or 0.0
    return (active, -round(abs(trade), TRADE_DECIMALS), -abs(total))


def _notable_rows(changes: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], int]:
    """The notable rows in source order, capped at :data:`NOTABLE_MAX`, and the uncapped count."""
    qualifying = [c for c in changes if _is_notable(c)]
    if len(qualifying) <= NOTABLE_MAX:
        return qualifying, len(qualifying)
    keep = {id(c) for c in sorted(qualifying, key=_notable_key)[:NOTABLE_MAX]}
    return [c for c in qualifying if id(c) in keep], len(qualifying)


def _change_row(change: Mapping[str, Any]) -> dict[str, Any]:
    """A change row reduced to :data:`CHANGE_KEYS` (non-finite numbers become null)."""
    out: dict[str, Any] = {}
    for key in CHANGE_KEYS:
        value = change.get(key)
        out[key] = _finite(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
    return out


def _decomposition_block(decomposition: Any) -> dict[str, Any] | None:
    if not isinstance(decomposition, Mapping):
        return None
    changes = _rows(decomposition.get("changes"))
    top = sorted((c for c in changes if _is_equity(c)), key=_top_change_key)[:TOP_CHANGES]
    notable, n_notable = _notable_rows(changes)
    return {
        "prev_as_of": decomposition.get("prev_as_of"),
        "curr_as_of": decomposition.get("curr_as_of"),
        "mode": decomposition.get("mode"),
        "scale_factor": decomposition.get("scale_factor"),
        "summary": decomposition.get("summary"),
        "top_changes": [_change_row(c) for c in top],
        "notable": [_change_row(c) for c in notable],
        "n_notable": n_notable,
        "n_changes": len(changes),
    }


def _etf_block(key: str, entry: Any) -> dict[str, Any]:
    entry = _mapping(entry)
    equities = _equities_block(entry.get("holdings"))
    return {
        "etf": entry.get("etf", key),
        "index_id": entry.get("index_id"),
        "as_of": entry.get("as_of"),
        "prev_as_of": entry.get("prev_as_of"),
        "source": entry.get("source"),
        "fund": entry.get("fund"),
        "equity_count": len(equities),
        "equities": equities,
        "caps": entry.get("caps"),
        "next_events": entry.get("next_events"),
        "decomposition": _decomposition_block(entry.get("decomposition")),
        "warnings": entry.get("warnings"),
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def build_summary(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """The summary dictionary for an ``analyze_all`` result (see the module docstring).

    Pure and deterministic: the same ``analysis`` gives an equal dictionary.
    Missing pieces become ``None`` (or an empty list/dict for collections)
    rather than raising, so a partial analysis still summarises.  The result
    contains JSON-native types only and no NaN (``json.dumps`` with
    ``allow_nan=False`` is proven here).
    """
    if not isinstance(analysis, Mapping):
        raise TypeError(f"analysis must be a mapping, got {type(analysis).__name__}")
    errors = analysis.get("errors")
    out: dict[str, Any] = {
        "generated_utc": analysis.get("generated_utc"),
        "today": analysis.get("today"),
        "errors": errors if isinstance(errors, Mapping) else {},
        "etfs": {str(key): _etf_block(str(key), entry) for key, entry in _mapping(analysis.get("etfs")).items()},
    }
    safe = json_safe(out)
    json.dumps(safe, allow_nan=False)
    return safe


def to_json(summary: Mapping[str, Any]) -> str:
    """The canonical text of ``reports/summary.json`` (sorted keys, indent 1, trailing newline)."""
    return json.dumps(summary, sort_keys=True, indent=1, ensure_ascii=False, allow_nan=False) + "\n"


def write_summary(root: str | os.PathLike[str], analysis: Mapping[str, Any]) -> Path:
    """Build the summary of ``analysis`` and write ``<root>/reports/summary.json`` atomically.

    Returns the path.  The file is stamped with the analysis's
    ``generated_utc`` only, so writing the same analysis twice gives the
    same bytes.
    """
    data = build_summary(analysis)
    path = summary_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(to_json(data), encoding="utf-8")
    os.replace(tmp, path)
    log.info("wrote %s (%d ETFs, %d bytes)", path, len(data["etfs"]), path.stat().st_size)
    return path
