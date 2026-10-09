"""Day-over-day decomposition of ETF weight changes.

Given two :class:`~etf_tracker.holdings.Snapshot` objects of the same ETF
(``prev`` and ``curr``) the weight change of every constituent is split into

* **drift** -- the part explained by prices moving while the share counts
  stay put (``w_cf - w_prev``), and
* **trade** -- the part explained by share counts changing
  (``w_curr - w_cf``, including the small interaction term),

where ``w_cf`` is the *counterfactual* weight: yesterday's shares, scaled by
the fund-flow factor ``k`` and valued at today's prices, normalized over the
union of both universes.  Because every weight vector is normalized to sum to
1 over the same universe, ``sum(drift) == sum(trade) == 0`` up to rounding.

Flow normalization
------------------
Creations and redemptions are in kind, so they scale *every* share count by
roughly the same factor.  ``k`` is the *lower* median
(:func:`statistics.median_low`) of ``shares_curr / shares_prev`` over the
common equity holdings, so it is always a ratio actually observed on some
holding: with an even number of names the plain median would average the two
middle ratios and, when they differ, flag every single name as a trade.
Names recognized as splits are excluded and the median re-estimated until
the set is stable.  A name whose share count moved by more than ``k``
(relative change above ``active_trade_threshold``) is flagged as an active
trade.  ``k`` cancels in the weight normalization, so it only affects the
per-name share-count statistics, never drift or trade.

Corporate actions
-----------------
A stock split multiplies the share count and divides the price without any
economic change.  When the k-adjusted share ratio is within
``split_tolerance`` of a ratio in :data:`SPLIT_RATIOS` *and* the price moved
by about the inverse factor, the name is classified
``'corporate_action_suspect'`` and its previous shares/price are restated by
the split ratio, so its trade is ~0 and its price return is the economic one.

Two modes
---------
* :func:`decompose` -- mode A, ``shares_and_prices``: full decomposition
  from snapshots that carry share counts.
* :func:`decompose_from_weights` -- mode B, ``weights_and_returns``: for
  sources that only publish weights.  With ``w_cf_i = w_prev_i (1 + r_i) /
  sum_j w_prev_j (1 + r_j)`` it reproduces mode A exactly when the returns
  are derived from the same prices (tested).

Only :mod:`etf_tracker.holdings` is imported from the package.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from statistics import median_low
from typing import Any

from etf_tracker.holdings import EQUITY, Holding, Snapshot, latest_two

__all__ = [
    "DEFAULT_ACTIVE_TRADE_THRESHOLD",
    "DEFAULT_SPLIT_TOLERANCE",
    "DEFAULT_SPLIT_PRICE_TOLERANCE",
    "SPLIT_RATIOS",
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
    "decompose",
    "decompose_from_weights",
    "decompose_latest",
    "estimate_scale_factor",
    "match_split_ratio",
    "sort_changes",
]

#: |rel_share_change| above this is an active trade (0.5%).
DEFAULT_ACTIVE_TRADE_THRESHOLD = 0.005
#: Relative tolerance for matching the share ratio to a split ratio (1%).
DEFAULT_SPLIT_TOLERANCE = 0.01
#: Tolerance on ``share_ratio * price_ratio`` being 1 for a split (the price
#: also carries the ordinary daily move, so this is looser).
DEFAULT_SPLIT_PRICE_TOLERANCE = 0.05
#: Share ratios recognized as splits (forward) and reverse splits.
SPLIT_RATIOS: tuple[float, ...] = (
    1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0,
    1 / 1.5, 0.5, 1 / 3, 0.25, 0.2, 1 / 6, 1 / 7, 0.125, 0.1, 1 / 15, 0.05, 0.04, 1 / 30, 0.025, 0.02,
)

CLASS_FLOW_ONLY = "flow_only"
CLASS_ACTIVE_TRADE = "active_trade"
CLASS_ENTRY = "entry"
CLASS_EXIT = "exit"
CLASS_CORPORATE_ACTION = "corporate_action_suspect"
CLASSIFICATIONS: tuple[str, ...] = (
    CLASS_FLOW_ONLY,
    CLASS_ACTIVE_TRADE,
    CLASS_ENTRY,
    CLASS_EXIT,
    CLASS_CORPORATE_ACTION,
)

MODE_SHARES_AND_PRICES = "shares_and_prices"
MODE_WEIGHTS_AND_RETURNS = "weights_and_returns"

# Flags attached to individual TickerChange objects.
FLAG_EXIT_PRICE_ASSUMED = "exit_price_assumed"
#: A held name had no usable price on one side; the other side's price was used.
FLAG_PRICE_CARRIED = "price_carried_forward"
FLAG_SHARES_MISSING = "shares_missing"
FLAG_PRICE_MISSING = "price_missing"
FLAG_UNPRICED = "unpriced"
FLAG_DUPLICATES_MERGED = "duplicate_lines_merged"
FLAG_RETURN_MISSING = "return_missing"
FLAG_NON_EQUITY = "non_equity"


# --------------------------------------------------------------------------
# Result dataclasses
# --------------------------------------------------------------------------


@dataclass
class TickerChange:
    """Decomposition of one constituent's weight change (all weights are
    fractions of the fund).

    ``drift = w_cf - w_prev`` (price effect), ``trade = w_curr - w_cf``
    (share-count effect), so ``w_curr - w_prev = drift + trade``.

    ``shares_prev_adjusted = shares_prev * scale_factor * split_ratio`` is
    what the holding "should" be if nothing but flows (and a detected split)
    happened; ``rel_share_change = shares_curr / shares_prev_adjusted - 1``.
    ``price_return`` is split-adjusted.  Mode B fills the share and price
    fields with ``None`` except ``price_return`` (the supplied return).
    """

    ticker: str
    name: str
    w_prev: float
    w_cf: float
    w_curr: float
    drift: float
    trade: float
    shares_prev: float | None
    shares_curr: float | None
    shares_prev_adjusted: float | None
    rel_share_change: float | None
    price_prev: float | None
    price_curr: float | None
    price_return: float | None
    classification: str
    asset_class: str = EQUITY
    split_ratio: float | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def total_change(self) -> float:
        """``w_curr - w_prev`` (== drift + trade)."""
        return self.w_curr - self.w_prev

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dictionary."""
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["flags"] = list(self.flags)
        data["total_change"] = self.total_change
        return _jsonable(data)


@dataclass
class DecompositionResult:
    """Full day-over-day decomposition of one ETF.

    ``entries`` / ``exits`` list tickers whose weight went from 0 to non-zero
    (resp. non-zero to 0), which includes names present in only one snapshot.
    ``scale_factor`` is ``None`` in mode B (not identifiable from weights).
    ``summary`` holds totals: ``gross_trade = sum |trade| / 2``,
    ``gross_drift``, ``largest_drift_up/down``, ``largest_trade_up/down``
    (equity lines), ``active_trade_tickers`` / ``n_active_trades`` (equity
    lines only; cash lines keep their own classification but are excluded
    here), ``classification_counts`` (all lines), ``portfolio_return`` and,
    in mode A, ``fund_flow`` with the implied creation/redemption.
    """

    etf: str
    prev_as_of: date | None
    curr_as_of: date | None
    scale_factor: float | None
    changes: list[TickerChange]
    entries: list[str]
    exits: list[str]
    warnings: list[str]
    summary: dict[str, Any]
    mode: str = MODE_SHARES_AND_PRICES

    def get(self, ticker: str) -> TickerChange | None:
        """The change for ``ticker`` or ``None``."""
        for change in self.changes:
            if change.ticker == ticker:
                return change
        return None

    def by_ticker(self) -> dict[str, TickerChange]:
        """Map ticker -> change."""
        return {c.ticker: c for c in self.changes}

    def sorted(
        self,
        by: str = "trade",
        reverse: bool = True,
        absolute: bool = False,
        equities_only: bool = False,
    ) -> list[TickerChange]:
        """Changes sorted by a :class:`TickerChange` attribute.

        ``by`` may be any numeric attribute (``'trade'``, ``'drift'``,
        ``'w_curr'``, ``'rel_share_change'``, ``'total_change'``, ...).
        ``absolute=True`` sorts by magnitude.  ``None`` values sort last.
        """
        return sort_changes(self.changes, by=by, reverse=reverse, absolute=absolute, equities_only=equities_only)

    def with_classification(self, *classifications: str) -> list[TickerChange]:
        """Changes whose classification is one of ``classifications``."""
        wanted = set(classifications)
        return [c for c in self.changes if c.classification in wanted]

    def active_trades(self) -> list[TickerChange]:
        """Equity names flagged ``'active_trade'``, largest |trade| first."""
        return sort_changes(
            [c for c in self.changes if c.classification == CLASS_ACTIVE_TRADE],
            by="trade",
            absolute=True,
            equities_only=True,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dictionary (dates as ISO strings)."""
        return {
            "etf": self.etf,
            "mode": self.mode,
            "prev_as_of": self.prev_as_of.isoformat() if self.prev_as_of else None,
            "curr_as_of": self.curr_as_of.isoformat() if self.curr_as_of else None,
            "scale_factor": self.scale_factor,
            "entries": list(self.entries),
            "exits": list(self.exits),
            "warnings": list(self.warnings),
            "summary": _jsonable(self.summary),
            "changes": [c.to_dict() for c in self.changes],
        }


#: Attribute names ``sort_changes`` accepts (dataclass fields plus properties).
_TICKER_CHANGE_ATTRIBUTES = frozenset(f.name for f in fields(TickerChange)) | {"total_change"}


def sort_changes(
    changes: Iterable[TickerChange],
    by: str = "trade",
    reverse: bool = True,
    absolute: bool = False,
    equities_only: bool = False,
) -> list[TickerChange]:
    """Sort ``changes`` by attribute ``by`` (see :meth:`DecompositionResult.sorted`)."""
    if by not in _TICKER_CHANGE_ATTRIBUTES and not hasattr(TickerChange, by):
        raise AttributeError(f"TickerChange has no attribute {by!r}")
    items = [c for c in changes if not equities_only or c.asset_class == EQUITY]

    def key(change: TickerChange) -> tuple[int, float, str]:
        value = getattr(change, by)
        if value is None:
            # None always last, whatever the direction.
            return (1, 0.0, change.ticker)
        value = float(value)
        if absolute:
            value = abs(value)
        return (0, -value if reverse else value, change.ticker)

    return sorted(items, key=key)


def _jsonable(obj: Any) -> Any:
    """Recursively convert dates/tuples/sets so ``json.dumps`` succeeds."""
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


# --------------------------------------------------------------------------
# Internal line representation
# --------------------------------------------------------------------------


@dataclass
class _Line:
    """A merged, normalized holdings line used internally."""

    ticker: str
    name: str
    asset_class: str
    shares: float | None
    price: float | None
    market_value: float | None
    weight: float | None
    flags: list[str] = field(default_factory=list)

    def value_at(self, price: float | None) -> float | None:
        """``shares * price`` when both known, else the line's market value."""
        if self.shares is not None and price is not None:
            return self.shares * price
        return self.market_value


def _merge_into(line: _Line, holding: Holding) -> None:
    """Add a duplicate ``holding`` line to ``line`` (sum shares/MV/weight).

    Shares are summed only when every merged line has a share count.
    Otherwise the total share count is unknown and ``shares`` becomes
    ``None`` (the decomposition then falls back to market values): pairing a
    partial share count with the full market value would fabricate a price,
    which can even masquerade as a stock split.
    """
    line.flags.append(FLAG_DUPLICATES_MERGED)
    if line.shares is not None and holding.shares is not None:
        line.shares = line.shares + holding.shares
    else:
        line.shares = None
    mv = holding.derived_market_value()
    if mv is not None:
        line.market_value = (line.market_value or 0.0) + mv
    if holding.weight is not None:
        line.weight = (line.weight or 0.0) + holding.weight
    if line.shares and line.market_value is not None:
        line.price = line.market_value / line.shares
    elif line.price is None:
        line.price = holding.price


def _normalize_line(line: _Line) -> None:
    """Fill derivable fields; cash-like lines without shares get price 1.0
    and ``shares = market_value`` so they participate in the share math."""
    if line.market_value is None and line.shares is not None and line.price is not None:
        line.market_value = line.shares * line.price
    if line.price is None and line.shares and line.market_value is not None:
        line.price = line.market_value / line.shares
    if line.asset_class != EQUITY:
        if line.shares is None:
            line.price = 1.0
            line.shares = line.market_value  # may stay None
        elif line.price is None:
            line.price = 1.0
            line.market_value = line.shares
        line.flags.append(FLAG_NON_EQUITY)


def _lines_from_snapshot(snapshot: Snapshot, warnings: list[str]) -> dict[str, _Line]:
    """Ticker -> merged normalized line, in file order."""
    lines: dict[str, _Line] = {}
    for h in snapshot.holdings:
        ticker = (h.ticker or "").strip()
        if not ticker:
            warnings.append(f"{snapshot.as_of.isoformat()}: holding {h.name!r} without ticker skipped")
            continue
        if ticker in lines:
            _merge_into(lines[ticker], h)
            continue
        lines[ticker] = _Line(
            ticker=ticker,
            name=h.name,
            asset_class=h.asset_class,
            shares=h.shares,
            price=h.price,
            market_value=h.derived_market_value(),
            weight=h.weight,
        )
    for line in lines.values():
        if FLAG_DUPLICATES_MERGED in line.flags:
            warnings.append(f"{snapshot.as_of.isoformat()}: duplicate lines for {line.ticker} merged")
        _normalize_line(line)
    return lines


# --------------------------------------------------------------------------
# Scale factor and split detection
# --------------------------------------------------------------------------


def match_split_ratio(
    share_ratio: float,
    tolerance: float = DEFAULT_SPLIT_TOLERANCE,
    ratios: Iterable[float] = SPLIT_RATIOS,
) -> float | None:
    """Return the split ratio ``share_ratio`` is within ``tolerance``
    (relative) of, or ``None``.  A ratio of ~1 never matches."""
    if not math.isfinite(share_ratio) or share_ratio <= 0:
        return None
    best: float | None = None
    best_err = tolerance
    for ratio in ratios:
        err = abs(share_ratio / ratio - 1.0)
        if err <= best_err:
            best, best_err = ratio, err
    return best


def _looks_like_split(
    share_ratio: float,
    price_prev: float | None,
    price_curr: float | None,
    split_tolerance: float,
    price_tolerance: float,
) -> float | None:
    """Split ratio when shares and price moved by inverse factors, else None."""
    ratio = match_split_ratio(share_ratio, split_tolerance)
    if ratio is None or price_prev is None or price_curr is None:
        return None
    if price_prev <= 0 or price_curr <= 0:
        return None
    product = share_ratio * (price_curr / price_prev)
    if abs(product - 1.0) <= price_tolerance:
        return ratio
    return None


def _share_ratios(prev: Mapping[str, _Line], curr: Mapping[str, _Line]) -> dict[str, float]:
    """``shares_curr / shares_prev`` over common equity lines with positive shares."""
    ratios: dict[str, float] = {}
    for ticker, cur_line in curr.items():
        prev_line = prev.get(ticker)
        if prev_line is None or cur_line.asset_class != EQUITY or prev_line.asset_class != EQUITY:
            continue
        if not prev_line.shares or not cur_line.shares:
            continue
        if prev_line.shares > 0 and cur_line.shares > 0:
            ratios[ticker] = cur_line.shares / prev_line.shares
    return ratios


def estimate_scale_factor(prev: Snapshot, curr: Snapshot) -> float:
    """Lower median of ``shares_curr / shares_prev`` over common equity
    holdings with positive share counts on both sides (1.0 when there is
    none).  Entries, exits, zero or missing share counts and non-equity lines
    never enter the median.  Convenience wrapper around the logic used by
    :func:`decompose` (without the split exclusion pass)."""
    warnings: list[str] = []
    ratios = _share_ratios(_lines_from_snapshot(prev, warnings), _lines_from_snapshot(curr, warnings))
    return float(median_low(ratios.values())) if ratios else 1.0


def _scale_factor_and_splits(
    prev: Mapping[str, _Line],
    curr: Mapping[str, _Line],
    detect_splits: bool,
    split_tolerance: float,
    price_tolerance: float,
    warnings: list[str],
) -> tuple[float, dict[str, float]]:
    """Iterative estimate: lower-median ratio, exclude split suspects,
    re-median until the suspect set is stable (bounded number of passes)."""
    ratios = _share_ratios(prev, curr)
    if not ratios:
        warnings.append("no common equity holdings with share counts; scale_factor set to 1.0")
        return 1.0, {}

    def detect(k: float) -> dict[str, float]:
        if not detect_splits:
            return {}
        found: dict[str, float] = {}
        for ticker, ratio in ratios.items():
            split = _looks_like_split(
                ratio / k, prev[ticker].price, curr[ticker].price, split_tolerance, price_tolerance
            )
            if split is not None:
                found[ticker] = split
        return found

    k = float(median_low(ratios.values()))
    splits = detect(k)
    for _ in range(4):
        remaining = [r for t, r in ratios.items() if t not in splits]
        k_next = float(median_low(remaining)) if remaining else k
        splits_next = detect(k_next)
        if k_next == k and splits_next == splits:
            break
        k, splits = k_next, splits_next
    if k <= 0 or not math.isfinite(k):
        warnings.append(f"implausible scale_factor {k!r}; reset to 1.0")
        k = 1.0
    return k, splits


# --------------------------------------------------------------------------
# Mode A: shares and prices
# --------------------------------------------------------------------------


def _meta_number(meta: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.replace(",", ""))
            except ValueError:
                continue
    return None


def _fund_flow(
    prev: Snapshot,
    curr: Snapshot,
    k: float,
    total_prev_at_curr_prices: float,
    portfolio_return: float,
) -> dict[str, Any]:
    """Implied fund flow from ``k`` and, when ``meta`` allows, from shares
    outstanding / net assets."""
    flow: dict[str, Any] = {
        "scale_factor": k,
        "flow_pct_from_scale_factor": k - 1.0,
        "implied_flow_usd_from_scale_factor": (k - 1.0) * total_prev_at_curr_prices,
    }
    so_prev = _meta_number(prev.meta, "shares_outstanding")
    so_curr = _meta_number(curr.meta, "shares_outstanding")
    if so_prev and so_curr:
        flow["shares_outstanding_prev"] = so_prev
        flow["shares_outstanding_curr"] = so_curr
        flow["flow_shares"] = so_curr - so_prev
        flow["flow_pct_from_shares_outstanding"] = so_curr / so_prev - 1.0
        nav = _meta_number(curr.meta, "nav", "nav_per_share")
        if nav:
            flow["implied_flow_usd_from_shares_outstanding"] = (so_curr - so_prev) * nav
    tna_prev = _meta_number(prev.meta, "total_net_assets", "net_assets")
    tna_curr = _meta_number(curr.meta, "total_net_assets", "net_assets")
    if tna_prev and tna_curr:
        flow["net_assets_prev"] = tna_prev
        flow["net_assets_curr"] = tna_curr
        flow["portfolio_return"] = portfolio_return
        implied = tna_curr - tna_prev * (1.0 + portfolio_return)
        flow["implied_flow_usd_from_net_assets"] = implied
        flow["flow_pct_from_net_assets"] = implied / tna_prev
    return flow


def _build_summary(changes: list[TickerChange], k: float | None) -> dict[str, Any]:
    """Totals and extremes over ``changes``."""
    equities = [c for c in changes if c.asset_class == EQUITY]

    def extreme(attr: str, largest: bool) -> dict[str, Any] | None:
        pool = equities or changes
        if not pool:
            return None
        pick = max(pool, key=lambda c: getattr(c, attr)) if largest else min(pool, key=lambda c: getattr(c, attr))
        return {"ticker": pick.ticker, "name": pick.name, "value": getattr(pick, attr)}

    counts = {name: 0 for name in CLASSIFICATIONS}
    for c in changes:
        counts[c.classification] = counts.get(c.classification, 0) + 1
    equity_counts = {name: 0 for name in CLASSIFICATIONS}
    for c in equities:
        equity_counts[c.classification] = equity_counts.get(c.classification, 0) + 1

    # Active-trade statistics are restricted to equity lines: cash balances
    # move every day for reasons (dividends, payables) that are not trades.
    active = [
        c
        for c in sort_changes(changes, by="trade", absolute=True, equities_only=True)
        if c.classification == CLASS_ACTIVE_TRADE
    ]

    return {
        "scale_factor": k,
        "n_changes": len(changes),
        "n_equities": len(equities),
        "n_prev": sum(1 for c in changes if c.w_prev != 0),
        "n_curr": sum(1 for c in changes if c.w_curr != 0),
        "n_entries": counts[CLASS_ENTRY],
        "n_exits": counts[CLASS_EXIT],
        "n_active_trades": len(active),
        "n_corporate_action_suspects": counts[CLASS_CORPORATE_ACTION],
        "classification_counts": counts,
        "classification_counts_equity": equity_counts,
        "sum_drift": sum(c.drift for c in changes),
        "sum_trade": sum(c.trade for c in changes),
        "gross_drift": sum(abs(c.drift) for c in changes) / 2.0,
        "gross_trade": sum(abs(c.trade) for c in changes) / 2.0,
        "gross_total_change": sum(abs(c.total_change) for c in changes) / 2.0,
        "largest_drift_up": extreme("drift", True),
        "largest_drift_down": extreme("drift", False),
        "largest_trade_up": extreme("trade", True),
        "largest_trade_down": extreme("trade", False),
        "active_trade_tickers": [c.ticker for c in active],
        "entries": [c.ticker for c in changes if c.classification == CLASS_ENTRY],
        "exits": [c.ticker for c in changes if c.classification == CLASS_EXIT],
    }


def _classify(
    w_prev: float,
    w_curr: float,
    rel_share_change: float | None,
    split_ratio: float | None,
    threshold: float,
) -> str:
    if w_prev == 0 and w_curr != 0:
        return CLASS_ENTRY
    if w_prev != 0 and w_curr == 0:
        return CLASS_EXIT
    if split_ratio is not None:
        return CLASS_CORPORATE_ACTION
    if rel_share_change is not None and abs(rel_share_change) > threshold:
        return CLASS_ACTIVE_TRADE
    return CLASS_FLOW_ONLY


def decompose(
    prev: Snapshot,
    curr: Snapshot,
    *,
    active_trade_threshold: float = DEFAULT_ACTIVE_TRADE_THRESHOLD,
    split_tolerance: float = DEFAULT_SPLIT_TOLERANCE,
    split_price_tolerance: float = DEFAULT_SPLIT_PRICE_TOLERANCE,
    detect_corporate_actions: bool = True,
    fallback_prices: Mapping[str, float] | None = None,
) -> DecompositionResult:
    """Mode A: decompose ``prev -> curr`` weight changes using shares and prices.

    ``fallback_prices`` supplies current prices for names missing one in
    ``curr`` -- notably exits, whose counterfactual value otherwise assumes
    the previous price (flag ``'exit_price_assumed'``).

    Raises ``ValueError`` when the snapshots belong to different ETFs, when
    ``prev.as_of`` is after ``curr.as_of``, or when a side has no positive
    total market value to normalize against.
    """
    if prev.etf.upper() != curr.etf.upper():
        raise ValueError(f"snapshots belong to different ETFs: {prev.etf!r} vs {curr.etf!r}")
    if prev.as_of > curr.as_of:
        raise ValueError(
            f"prev.as_of {prev.as_of.isoformat()} is after curr.as_of {curr.as_of.isoformat()}; swap the arguments"
        )
    warnings: list[str] = []
    if prev.as_of == curr.as_of:
        warnings.append("prev and curr have the same as_of date")

    prev_lines = _lines_from_snapshot(prev, warnings)
    curr_lines = _lines_from_snapshot(curr, warnings)
    fallback_prices = dict(fallback_prices or {})

    k, splits = _scale_factor_and_splits(
        prev_lines, curr_lines, detect_corporate_actions, split_tolerance, split_price_tolerance, warnings
    )

    # Universe: current order first, then names that only exist in prev.
    universe = list(curr_lines) + [t for t in prev_lines if t not in curr_lines]

    mv_prev: dict[str, float] = {}
    mv_cf: dict[str, float] = {}
    mv_curr: dict[str, float] = {}
    unscaled_cf_total = 0.0
    per_ticker: dict[str, dict[str, Any]] = {}

    for ticker in universe:
        p = prev_lines.get(ticker)
        c = curr_lines.get(ticker)
        flags: list[str] = []
        split = splits.get(ticker)
        price_prev = p.price if p else None
        price_curr = c.price if c else None
        if price_curr is None and ticker in fallback_prices:
            price_curr = float(fallback_prices[ticker])
        if price_curr is None and p is not None and c is None and price_prev is not None:
            price_curr = price_prev
            flags.append(FLAG_EXIT_PRICE_ASSUMED)
        # A held name (present on both sides, share count known) with no usable
        # price on one side -- a halted stock, a blank cell -- must not be
        # reported as a full exit or entry: use the other side's price.
        if (
            price_curr is None
            and p is not None
            and c is not None
            and c.shares is not None
            and c.market_value is None
            and price_prev is not None
        ):
            price_curr = price_prev
            flags.extend((FLAG_PRICE_MISSING, FLAG_PRICE_CARRIED))
            warnings.append(f"{ticker}: no usable current price; previous price carried forward")
        if (
            price_prev is None
            and p is not None
            and c is not None
            and p.shares is not None
            and p.market_value is None
            and price_curr is not None
        ):
            price_prev = price_curr
            flags.extend((FLAG_PRICE_MISSING, FLAG_PRICE_CARRIED))
            warnings.append(f"{ticker}: no usable previous price; current price carried back")
        # Split-adjusted previous price: a 2:1 split halves the reference price.
        price_prev_adj = price_prev / split if (price_prev is not None and split) else price_prev

        shares_prev = p.shares if p else None
        shares_curr = c.shares if c else None
        shares_prev_adj = shares_prev * k * (split or 1.0) if shares_prev is not None else None

        # Market values: prefer shares * price so the three vectors agree.
        v_prev: float | None = p.value_at(price_prev) if p is not None else 0.0
        if p is not None and shares_prev is not None and price_curr is not None:
            v_cf_unscaled = shares_prev * (split or 1.0) * price_curr
        elif p is not None and v_prev is not None:
            # No usable share count: carry the market value forward at the price return if known.
            growth = (price_curr / price_prev_adj) if (price_curr and price_prev_adj) else 1.0
            v_cf_unscaled = v_prev * growth
            if p.asset_class == EQUITY:
                flags.append(FLAG_SHARES_MISSING)
        else:
            v_cf_unscaled = 0.0
        v_curr: float | None = c.value_at(price_curr) if c is not None else 0.0

        if v_prev is None:
            flags.append(FLAG_UNPRICED)
            v_prev, v_cf_unscaled = 0.0, 0.0
            warnings.append(f"{ticker}: no usable shares/price/market_value in prev; treated as 0")
        if v_curr is None:
            flags.append(FLAG_UNPRICED)
            v_curr = 0.0
            warnings.append(f"{ticker}: no usable shares/price/market_value in curr; treated as 0")
        if c is not None and c.asset_class == EQUITY and (shares_curr is None):
            flags.append(FLAG_SHARES_MISSING)
        if (p is not None and price_prev is None) or (c is not None and price_curr is None):
            if FLAG_PRICE_MISSING not in flags:
                flags.append(FLAG_PRICE_MISSING)

        mv_prev[ticker] = v_prev
        mv_cf[ticker] = v_cf_unscaled * k
        mv_curr[ticker] = v_curr
        unscaled_cf_total += v_cf_unscaled

        line = c or p
        assert line is not None
        for f in line.flags:
            if f not in flags and f != FLAG_DUPLICATES_MERGED:
                flags.append(f)
        per_ticker[ticker] = {
            "name": line.name,
            "asset_class": line.asset_class,
            "shares_prev": shares_prev,
            "shares_curr": shares_curr,
            "shares_prev_adjusted": shares_prev_adj,
            "price_prev": price_prev_adj,
            "price_curr": price_curr,
            "split": split,
            "flags": flags,
        }

    total_prev = sum(mv_prev.values())
    total_cf = sum(mv_cf.values())
    total_curr = sum(mv_curr.values())
    if total_prev <= 0 or total_cf <= 0 or total_curr <= 0:
        raise ValueError(
            f"cannot normalize weights: totals prev={total_prev}, cf={total_cf}, curr={total_curr}"
        )

    changes: list[TickerChange] = []
    for ticker in universe:
        info = per_ticker[ticker]
        w_prev = mv_prev[ticker] / total_prev
        w_cf = mv_cf[ticker] / total_cf
        w_curr = mv_curr[ticker] / total_curr
        shares_prev_adj = info["shares_prev_adjusted"]
        shares_curr = info["shares_curr"]
        rel: float | None = None
        if shares_prev_adj and shares_curr is not None and w_prev != 0 and w_curr != 0:
            rel = shares_curr / shares_prev_adj - 1.0
        price_prev_adj = info["price_prev"]
        price_curr = info["price_curr"]
        price_return: float | None = None
        if price_prev_adj and price_curr is not None:
            price_return = price_curr / price_prev_adj - 1.0
        classification = _classify(w_prev, w_curr, rel, info["split"], active_trade_threshold)
        changes.append(
            TickerChange(
                ticker=ticker,
                name=info["name"],
                w_prev=w_prev,
                w_cf=w_cf,
                w_curr=w_curr,
                drift=w_cf - w_prev,
                trade=w_curr - w_cf,
                shares_prev=info["shares_prev"],
                shares_curr=shares_curr,
                shares_prev_adjusted=shares_prev_adj,
                rel_share_change=rel,
                price_prev=price_prev_adj,
                price_curr=price_curr,
                price_return=price_return,
                classification=classification,
                asset_class=info["asset_class"],
                split_ratio=info["split"],
                flags=info["flags"],
            )
        )

    for change in changes:
        if change.classification == CLASS_CORPORATE_ACTION:
            warnings.append(
                f"{change.ticker}: share ratio ~{change.split_ratio:g}x with inverse price move; "
                "corporate action suspected and previous shares/price restated"
            )
        if FLAG_EXIT_PRICE_ASSUMED in change.flags:
            warnings.append(f"{change.ticker}: exited; previous price used for its counterfactual value")

    changes = sort_changes(changes, by="w_curr", reverse=True)
    portfolio_return = unscaled_cf_total / total_prev - 1.0
    summary = _build_summary(changes, k)
    summary["total_market_value_prev"] = total_prev
    summary["total_market_value_curr"] = total_curr
    summary["portfolio_return"] = portfolio_return
    summary["fund_flow"] = _fund_flow(prev, curr, k, unscaled_cf_total, portfolio_return)

    return DecompositionResult(
        etf=curr.etf,
        prev_as_of=prev.as_of,
        curr_as_of=curr.as_of,
        scale_factor=k,
        changes=changes,
        entries=summary["entries"],
        exits=summary["exits"],
        warnings=warnings,
        summary=summary,
        mode=MODE_SHARES_AND_PRICES,
    )


def decompose_latest(
    root: str | os.PathLike[str],
    etf: str,
    **kwargs: Any,
) -> DecompositionResult | None:
    """Decompose the two most recent stored snapshots of ``etf`` (see
    :func:`etf_tracker.holdings.latest_two`); ``None`` when fewer than two
    exist.  ``kwargs`` are passed to :func:`decompose`."""
    pair = latest_two(root, etf)
    if pair is None:
        return None
    prev, curr = pair
    return decompose(prev, curr, **kwargs)


# --------------------------------------------------------------------------
# Mode B: weights and returns
# --------------------------------------------------------------------------


def _normalized(weights: Mapping[str, float], label: str, warnings: list[str]) -> dict[str, float]:
    """Strip ticker keys (aggregating collisions), drop ``None`` and
    renormalize to sum to 1."""
    clean: dict[str, float] = {}
    for ticker, w in weights.items():
        if w is None:
            continue
        key = str(ticker).strip()
        clean[key] = clean.get(key, 0.0) + float(w)
    total = sum(clean.values())
    if total <= 0 or not math.isfinite(total):
        raise ValueError(f"{label} weights do not sum to a positive number ({total})")
    if abs(total - 1.0) > 0.01:
        warnings.append(f"{label} weights sum to {total:.4f}; re-normalized to 1")
    return {t: w / total for t, w in clean.items()}


def decompose_from_weights(
    prev_weights: Mapping[str, float],
    curr_weights: Mapping[str, float],
    returns: Mapping[str, float],
    *,
    etf: str = "",
    prev_as_of: date | None = None,
    curr_as_of: date | None = None,
    names: Mapping[str, str] | None = None,
    asset_classes: Mapping[str, str] | None = None,
    active_trade_threshold: float = DEFAULT_ACTIVE_TRADE_THRESHOLD,
) -> DecompositionResult:
    """Mode B: decompose from weights (fractions) and per-ticker price returns.

    ``w_cf_i = w_prev_i (1 + r_i) / sum_j w_prev_j (1 + r_j)``; both weight
    vectors are re-normalized to sum to 1 over their own universe first.
    Tickers without a return are treated as ``r = 0`` and flagged
    ``'return_missing'``.  ``rel_share_change`` is the implied relative share
    change ``(w_curr / w_cf) / m - 1`` with ``m`` the lower median implied
    ratio over common equity names, which reproduces mode A's statistic
    exactly (same estimator, same universe).
    """
    warnings: list[str] = []
    w_prev_map = _normalized(prev_weights, "prev", warnings)
    w_curr_map = _normalized(curr_weights, "curr", warnings)
    # Keys of every side mapping are stripped like the weight keys so that
    # " AAPL" and "AAPL" refer to the same name.
    returns = {str(t).strip(): r for t, r in returns.items()}
    names = {str(t).strip(): v for t, v in (names or {}).items()}
    asset_classes = {str(t).strip(): v for t, v in (asset_classes or {}).items()}

    universe = list(w_curr_map) + [t for t in w_prev_map if t not in w_curr_map]

    growth: dict[str, float] = {}
    missing: list[str] = []
    for ticker in universe:
        r = returns.get(ticker)
        if r is None or (isinstance(r, float) and not math.isfinite(r)):
            if w_prev_map.get(ticker, 0.0) != 0:
                missing.append(ticker)
            growth[ticker] = 1.0
        else:
            growth[ticker] = 1.0 + float(r)
    if missing:
        warnings.append(f"no return for {len(missing)} ticker(s), treated as 0: {', '.join(missing)}")

    cf_raw = {t: w_prev_map.get(t, 0.0) * growth[t] for t in universe}
    cf_total = sum(cf_raw.values())
    if cf_total <= 0:
        raise ValueError("counterfactual weights do not sum to a positive number")

    w_prev = {t: w_prev_map.get(t, 0.0) for t in universe}
    w_cf = {t: cf_raw[t] / cf_total for t in universe}
    w_curr = {t: w_curr_map.get(t, 0.0) for t in universe}

    implied = {
        t: w_curr[t] / w_cf[t]
        for t in universe
        if w_prev[t] != 0 and w_curr[t] != 0 and w_cf[t] > 0 and asset_classes.get(t, EQUITY) == EQUITY
    }
    m = float(median_low(implied.values())) if implied else 1.0

    changes: list[TickerChange] = []
    for ticker in universe:
        flags: list[str] = []
        if ticker in missing:
            flags.append(FLAG_RETURN_MISSING)
        asset_class = asset_classes.get(ticker, EQUITY)
        if asset_class != EQUITY:
            flags.append(FLAG_NON_EQUITY)
        rel: float | None = None
        if w_prev[ticker] != 0 and w_curr[ticker] != 0 and w_cf[ticker] > 0:
            rel = (w_curr[ticker] / w_cf[ticker]) / m - 1.0
        classification = _classify(w_prev[ticker], w_curr[ticker], rel, None, active_trade_threshold)
        changes.append(
            TickerChange(
                ticker=ticker,
                name=names.get(ticker, ""),
                w_prev=w_prev[ticker],
                w_cf=w_cf[ticker],
                w_curr=w_curr[ticker],
                drift=w_cf[ticker] - w_prev[ticker],
                trade=w_curr[ticker] - w_cf[ticker],
                shares_prev=None,
                shares_curr=None,
                shares_prev_adjusted=None,
                rel_share_change=rel,
                price_prev=None,
                price_curr=None,
                price_return=growth[ticker] - 1.0,
                classification=classification,
                asset_class=asset_class,
                split_ratio=None,
                flags=flags,
            )
        )

    changes = sort_changes(changes, by="w_curr", reverse=True)
    summary = _build_summary(changes, None)
    summary["portfolio_return"] = cf_total - 1.0
    summary["median_implied_share_ratio"] = m
    return DecompositionResult(
        etf=etf,
        prev_as_of=prev_as_of,
        curr_as_of=curr_as_of,
        scale_factor=None,
        changes=changes,
        entries=summary["entries"],
        exits=summary["exits"],
        warnings=warnings,
        summary=summary,
        mode=MODE_WEIGHTS_AND_RETURNS,
    )
