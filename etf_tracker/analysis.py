"""Daily analysis of the stored snapshots as one JSON-ready dictionary.

Standard library only.

:func:`analyze_etf` reads the newest one or two snapshots of an ETF from
``<root>/data/normalized/<ETF>/`` and combines the analysis core:

* :func:`etf_tracker.decompose.decompose` -- price drift versus share-count
  trade between the previous and the current snapshot (``None`` when only
  one snapshot exists);
* :func:`etf_tracker.bridge.apply_caps_to_snapshot` and
  ``IndexRules.check_constraints`` -- pro-forma capping of today's weights
  under the rules of the *next* scheduled event, the constraints the
  current weights already breach, and the resulting forced sellers/buyers;
* ``QQQRules.special_rebalance_triggered`` -- the Nasdaq-100 special
  rebalance triggers (QQQ only);
* ``IndexRules.next_events`` with trading-day countdowns from ``today``.

The result follows the contract documented on :func:`analyze_etf` (fixed
top-level keys, ISO dates, plain floats/ints, ``None`` instead of NaN) and
is guaranteed ``json.dumps``-able as is.  :func:`analyze_all` runs it for
several ETFs and collects per-ETF failures instead of raising.

Weights are fractions (0.0-1.0) throughout; the report layer formats them.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from etf_tracker import market_calendar
from etf_tracker.bridge import apply_caps_to_snapshot, constituents_from_snapshot
from etf_tracker.decompose import decompose as decompose_snapshots
from etf_tracker.holdings import EQUITY, Snapshot, list_snapshots, load_snapshot, snapshot_path
from etf_tracker.rules import INDEX_FOR_ETF, RebalanceEvent, rules_for

__all__ = [
    "ANALYSIS_KEYS",
    "CAPS_KEYS",
    "FUND_KEYS",
    "HOLDING_KEYS",
    "EVENT_KEYS",
    "DEFAULT_EVENT_COUNT",
    "NoSnapshotError",
    "analyze_etf",
    "analyze_all",
    "utc_today",
    "json_safe",
]

log = logging.getLogger(__name__)

#: Top-level keys of the dictionary :func:`analyze_etf` returns.
ANALYSIS_KEYS: tuple[str, ...] = (
    "etf",
    "index_id",
    "as_of",
    "prev_as_of",
    "source",
    "fund",
    "holdings",
    "decomposition",
    "caps",
    "next_events",
    "warnings",
)
FUND_KEYS: tuple[str, ...] = ("shares_outstanding", "prev_shares_outstanding", "nav", "total_net_assets")
HOLDING_KEYS: tuple[str, ...] = (
    "ticker",
    "name",
    "asset_class",
    "weight",
    "shares",
    "price",
    "market_value",
    "is_adr",
    "company_id",
)
CAPS_KEYS: tuple[str, ...] = (
    "event_type",
    "feasible",
    "binding_constraints",
    "notes",
    "metrics",
    "target_metrics",
    "breaches",
    "forced_sellers",
    "forced_buyers",
    "special_rebalance",
)
EVENT_KEYS: tuple[str, ...] = (
    "kind",
    "reference_date",
    "announcement_date",
    "effective_trade_date",
    "effective_date",
    "trading_days_to_reference",
    "trading_days_to_trade",
    "notes",
)
#: How many upcoming events ``next_events`` lists.
DEFAULT_EVENT_COUNT = 3

#: Deltas smaller than this (in weight fraction) are not forced trades.
_DELTA_EPS = 1e-9
#: Bound on free-text warnings copied from snapshot metadata.
_MAX_META_WARNINGS = 50
#: ``Snapshot.validate()`` note about a price or market value it derived.
_DERIVED_NOTE_RE = re.compile(r"^(?P<ticker>\S+): derived (?:price|market_value) \S+ from ")


class NoSnapshotError(LookupError):
    """Raised by :func:`analyze_etf` when the ETF has no stored snapshot."""


# --------------------------------------------------------------------------
# Serialisation helpers
# --------------------------------------------------------------------------


def utc_today(now: datetime | None = None) -> date:
    """Today's date in UTC (the daily job runs on a UTC schedule)."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.date()


def json_safe(obj: Any) -> Any:
    """Recursively convert to plain JSON types.

    Dates become ISO strings, tuples/sets become lists, dataclass-like
    objects with ``to_dict`` are expanded, non-finite floats become
    ``None`` and anything else unknown is stringified.
    """
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return json_safe(to_dict())
    return str(obj)


def _number(value: Any) -> float | None:
    """A finite float or ``None`` (strings with thousands separators accepted)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _meta_number(meta: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        number = _number(meta.get(key))
        if number is not None:
            return number
    return None


def _strings(values: Any, prefix: str = "") -> list[str]:
    """Flatten a list of strings from free-form metadata, bounded."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            out.append(f"{prefix}{value.strip()}")
        if len(out) >= _MAX_META_WARNINGS:
            break
    return out


# --------------------------------------------------------------------------
# Snapshot access
# --------------------------------------------------------------------------


def _load_latest_pair(root: str | os.PathLike[str], etf: str) -> tuple[Snapshot | None, Snapshot]:
    """``(prev, curr)`` from disk; ``prev`` is ``None`` with one snapshot."""
    dates = list_snapshots(root, etf)
    if not dates:
        raise NoSnapshotError(f"{etf}: no snapshot stored under {snapshot_path(root, etf, date(2000, 1, 1)).parent}")
    curr = load_snapshot(snapshot_path(root, etf, dates[-1]))
    prev = load_snapshot(snapshot_path(root, etf, dates[-2])) if len(dates) >= 2 else None
    return prev, curr


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------


def _fund_block(curr: Snapshot, prev: Snapshot | None) -> dict[str, float | None]:
    """Fund-level numbers from snapshot metadata.

    ``total_net_assets`` falls back to the sum of the holdings' market
    values (cash included) when the source does not publish it, so the
    report can turn weight deltas into dollar amounts for every ETF.
    """
    tna = _meta_number(curr.meta, "total_net_assets", "net_assets", "shareclass_total_net_assets")
    if tna is None:
        total_mv = curr.total_market_value(include_cash=True)
        tna = total_mv if total_mv > 0 else None
    return {
        "shares_outstanding": _meta_number(curr.meta, "shares_outstanding"),
        "prev_shares_outstanding": _meta_number(prev.meta, "shares_outstanding") if prev else None,
        "nav": _meta_number(curr.meta, "nav"),
        "total_net_assets": tna,
    }


def _holdings_block(curr: Snapshot) -> list[dict[str, Any]]:
    """All holdings, largest weight first (lines without a weight last)."""
    fallback = curr.normalized_weights(include_cash=True)
    rows: list[dict[str, Any]] = []
    for h in curr.holdings:
        weight = h.weight if h.weight is not None else fallback.get(h.ticker)
        rows.append(
            {
                "ticker": h.ticker,
                "name": h.name,
                "asset_class": h.asset_class,
                "weight": _number(weight),
                "shares": _number(h.shares),
                "price": _number(h.derived_price()),
                "market_value": _number(h.derived_market_value()),
                "is_adr": bool(h.is_adr) if h.is_adr is not None else False,
                "company_id": h.company_id,
            }
        )
    rows.sort(key=lambda r: (r["weight"] is None, -(r["weight"] or 0.0), r["ticker"]))
    return rows


def _decomposition_block(prev: Snapshot | None, curr: Snapshot, warnings: list[str]) -> dict[str, Any] | None:
    if prev is None:
        warnings.append("no previous snapshot: drift/trade decomposition skipped")
        return None
    try:
        result = decompose_snapshots(prev, curr)
    except ValueError as exc:
        warnings.append(f"decomposition failed: {exc}")
        return None
    warnings.extend(f"decomposition: {w}" for w in result.warnings)
    return json_safe(result.to_dict())


def _caps_block(curr: Snapshot, index_id: str, today: date, warnings: list[str]) -> dict[str, Any]:
    """Pro-forma caps, current breaches and (QQQ) special-rebalance check.

    The event whose rules apply is the next one scheduled after ``today``
    (not after the snapshot date): holdings as of a trade date already
    reflect that day's rebalance, so the following event is the one to
    anticipate.
    """
    rules = rules_for(index_id)
    event_type = rules.event_type_for(today)
    adr_tickers = [h.ticker for h in curr.equities() if h.is_adr]
    block: dict[str, Any] = {
        "event_type": event_type,
        "feasible": False,
        "binding_constraints": [],
        "notes": [],
        "metrics": {},
        "target_metrics": {},
        "breaches": [],
        "forced_sellers": [],
        "forced_buyers": [],
        "special_rebalance": None,
    }

    constituents = constituents_from_snapshot(curr, index_id, adr_tickers=adr_tickers)
    if not constituents:
        warnings.append("caps skipped: no equity line with a positive weight")
        block["notes"].append("no equity line with a positive weight; caps not applied")
        return block

    try:
        cap = apply_caps_to_snapshot(curr, index_id, event_type, adr_tickers=adr_tickers)
    except ValueError as exc:
        warnings.append(f"caps failed: {exc}")
        block["notes"].append(str(exc))
        return block

    deltas = cap.sorted_deltas()
    block.update(
        {
            "feasible": bool(cap.feasible),
            "binding_constraints": list(cap.binding_constraints),
            "notes": list(cap.notes),
            "metrics": {k: _number(v) for k, v in cap.metrics.items()},
            "target_metrics": {k: _number(v) for k, v in cap.target_metrics.items()},
            "forced_sellers": [[t, d] for t, d in deltas if d < -_DELTA_EPS],
            "forced_buyers": [[t, d] for t, d in reversed(deltas) if d > _DELTA_EPS],
        }
    )
    if not cap.feasible:
        warnings.append(f"caps infeasible under {event_type}: {'; '.join(cap.notes) or 'see notes'}")

    try:
        breaches = rules.check_constraints(constituents, event_type=event_type)
    except ValueError as exc:
        warnings.append(f"constraint check failed: {exc}")
        breaches = []
    block["breaches"] = [
        {
            "rule": b.rule,
            "description": b.description,
            "tickers": list(b.tickers),
            "value": _number(b.value),
            "limit": _number(b.limit),
        }
        for b in breaches
    ]

    trigger_check = getattr(rules, "special_rebalance_triggered", None)
    if callable(trigger_check):
        check = trigger_check(constituents)
        block["special_rebalance"] = {
            "triggered": bool(check.triggered),
            "reasons": list(check.reasons),
            "metrics": {k: _number(v) for k, v in check.metrics.items()},
        }
        if check.triggered:
            warnings.append("special rebalance trigger breached: " + "; ".join(check.reasons))
    return block


def _event_dict(event: RebalanceEvent, today: date) -> dict[str, Any]:
    return {
        "kind": event.kind,
        "reference_date": event.reference_date.isoformat(),
        "announcement_date": event.announcement_date.isoformat() if event.announcement_date else None,
        "effective_trade_date": event.effective_trade_date.isoformat(),
        "effective_date": event.effective_date.isoformat(),
        "trading_days_to_reference": market_calendar.trading_days_between(today, event.reference_date),
        "trading_days_to_trade": market_calendar.trading_days_between(today, event.effective_trade_date),
        "notes": event.notes,
    }


def _events_block(index_id: str, today: date, n: int) -> list[dict[str, Any]]:
    return [_event_dict(ev, today) for ev in rules_for(index_id).next_events(today, n)]


def _validation_warnings(curr: Snapshot) -> list[str]:
    """``curr.validate()`` without the derivation notes of non-equity lines.

    Sources such as Invesco publish no per-line price for cash, collateral
    and futures lines, and iShares futures lines carry a notional instead of
    a market value, so ``validate()`` deriving those fields is expected and
    not a data-quality signal.  The same note for an *equity* line is kept:
    there it means the source left out a price the rules depend on.
    """
    non_equity = {h.ticker for h in curr.holdings if h.asset_class != EQUITY}
    out: list[str] = []
    for note in curr.validate():
        match = _DERIVED_NOTE_RE.match(note)
        if match and match.group("ticker") in non_equity:
            continue
        out.append(note)
    return out


def _data_quality_notes(curr: Snapshot, today: date, index_id: str | None = None) -> list[str]:
    """Staleness, ADR-flag provenance and source warnings.

    The ADR notes are only relevant to an index with an ADR rule (SOXX's
    10% aggregate cap); for the others the flag is never read, so the
    heuristic's guesses about Canadian ordinary shares in IGV stay silent.
    """
    notes: list[str] = []
    expected = market_calendar.prev_trading_day(today)
    if curr.as_of < expected:
        lag = market_calendar.trading_days_between(curr.as_of, expected)
        notes.append(
            f"snapshot as of {curr.as_of.isoformat()} is {lag} trading day(s) older than the "
            f"last completed session {expected.isoformat()}"
        )
    if curr.as_of > today:
        notes.append(f"snapshot as of {curr.as_of.isoformat()} is after today {today.isoformat()}")
    uses_adr = index_id is None or getattr(rules_for(index_id), "ADR_CAP", None) is not None
    heuristic = curr.meta.get("adr_heuristic")
    if uses_adr and isinstance(heuristic, (list, tuple)) and heuristic:
        notes.append("ADR flag inferred from listing location for: " + ", ".join(str(t) for t in heuristic[:20]))
    overrides = curr.meta.get("adr_overrides")
    if uses_adr and isinstance(overrides, (list, tuple)) and overrides:
        notes.append(
            "ADR flag overridden to False by configuration for: " + ", ".join(str(t) for t in overrides[:20])
        )
    for key in ("validation_warnings", "parse_warnings", "warnings"):
        notes.extend(_strings(curr.meta.get(key), prefix=f"source {key}: "))
    return notes


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def analyze_etf(
    root: str | os.PathLike[str],
    etf: str,
    today: date | None = None,
    *,
    n_events: int = DEFAULT_EVENT_COUNT,
) -> dict[str, Any]:
    """Analyse the newest stored snapshot(s) of ``etf``.

    Returns a dictionary with exactly the keys in :data:`ANALYSIS_KEYS`::

        etf, index_id, as_of, prev_as_of (or None), source,
        fund          {shares_outstanding, prev_shares_outstanding, nav, total_net_assets}
        holdings      [{ticker, name, asset_class, weight, shares, price,
                        market_value, is_adr, company_id}, ...]  weight desc
        decomposition DecompositionResult.to_dict() or None
        caps          {event_type, feasible, binding_constraints, notes, metrics,
                       target_metrics, breaches, forced_sellers, forced_buyers,
                       special_rebalance (QQQ only, else None)}
        next_events   [{kind, reference_date, announcement_date, effective_trade_date,
                        effective_date, trading_days_to_reference,
                        trading_days_to_trade, notes}, ...]
        warnings      [str, ...]

    ``today`` (default: the current UTC date) drives the event countdowns
    and the choice of event rules.  All dates are ISO strings, numbers are
    plain floats/ints and there is no NaN.  Raises :class:`NoSnapshotError`
    when nothing is stored for ``etf``.
    """
    etf_key = str(etf).strip().upper()
    index_id = INDEX_FOR_ETF.get(etf_key, etf_key)
    index_id = rules_for(index_id).index_id  # KeyError for an unknown ETF
    today = today or utc_today()

    prev, curr = _load_latest_pair(root, etf_key)
    warnings: list[str] = _validation_warnings(curr)
    warnings.extend(_data_quality_notes(curr, today, index_id))

    result: dict[str, Any] = {
        "etf": etf_key,
        "index_id": index_id,
        "as_of": curr.as_of.isoformat(),
        "prev_as_of": prev.as_of.isoformat() if prev else None,
        "source": curr.source,
        "fund": _fund_block(curr, prev),
        "holdings": _holdings_block(curr),
        "decomposition": _decomposition_block(prev, curr, warnings),
        "caps": _caps_block(curr, index_id, today, warnings),
        "next_events": _events_block(index_id, today, n_events),
        "warnings": warnings,
    }
    safe = json_safe(result)
    # The contract promises a json.dumps-able dict without NaN; prove it here
    # rather than in the report layer.
    json.dumps(safe, allow_nan=False)
    return safe


def analyze_all(
    root: str | os.PathLike[str],
    etfs: Iterable[str],
    today: date | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run :func:`analyze_etf` for each ETF, collecting failures.

    Returns ``{"generated_utc", "today", "etfs": {etf: analysis},
    "errors": {etf: message}}``.  An ETF that cannot be analysed (no
    snapshot, unknown ticker, unexpected exception) lands in ``errors`` and
    the others are still produced.
    """
    moment = now or datetime.now(timezone.utc)
    today = today or utc_today(moment)
    out: dict[str, Any] = {
        "generated_utc": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if moment.tzinfo
        else moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today": today.isoformat(),
        "etfs": {},
        "errors": {},
    }
    for etf in etfs:
        key = str(etf).strip().upper()
        try:
            out["etfs"][key] = analyze_etf(root, key, today)
        except NoSnapshotError as exc:
            out["errors"][key] = str(exc)
            log.warning("%s", exc)
        except Exception as exc:  # noqa: BLE001 - one ETF must not sink the others
            out["errors"][key] = f"{type(exc).__name__}: {exc}"
            log.exception("%s: analysis failed", key)
    return out
