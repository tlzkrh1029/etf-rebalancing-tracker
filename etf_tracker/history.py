"""Compact per-day history for dashboards: ``reports/history.json``.

The daily report (``reports/latest.json``) describes one day in full.  A
chart of how a constituent drifts toward its cap needs the same few numbers
for every stored day, so this module walks ``data/normalized/<ETF>/`` and
writes one small file with parallel arrays per ETF:

- ``dates``: ISO dates of the stored snapshots, oldest first
- ``metrics``: the index-specific cap metrics computed by
  :func:`etf_tracker.bridge.apply_caps_to_snapshot` for every day
  (same definitions as the daily report)
- ``weights``: equity-renormalised weights (fractions) of the tickers worth
  charting: the largest names on the latest day plus every name that ever
  exceeded the index's single-name cap inside the window
- ``shares_outstanding`` and ``total_net_assets`` per day
- ``limits``: the cap levels of the index, taken from the rules classes

Stdlib only.  Weights are fractions; ``null`` marks a day on which a ticker
was not held.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from etf_tracker.bridge import apply_caps_to_snapshot
from etf_tracker.holdings import list_snapshots, load_snapshot, snapshot_path
from etf_tracker.rules import INDEX_FOR_ETF, rules_for

log = logging.getLogger(__name__)

DEFAULT_ETFS: tuple[str, ...] = ("SOXX", "QQQ", "IGV")
TOP_N = 12
MAX_TICKERS = 24
HISTORY_RELPATH = Path("reports") / "history.json"

# Attribute names on the rules classes -> keys in the ``limits`` block.
_LIMIT_ATTRS = {
    "SINGLE_CAP": "single_cap",
    "OUTSIDE_TOP5_CAP": "outside_top5_cap",
    "ADR_CAP": "adr_cap",
    "COMPANY_TRIGGER": "company_trigger",
    "COMPANY_CAP": "company_cap",
    "COHORT_THRESHOLD": "cohort_threshold",
    "COHORT_TRIGGER": "cohort_trigger",
    "COHORT_TARGET": "cohort_target",
    "COHORT_CAP": "cohort_cap",
    "SECURITY_TRIGGER": "security_trigger",
    "SECURITY_CAP": "security_cap",
}


def history_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / HISTORY_RELPATH


def index_limits(index_id: str) -> dict[str, float]:
    """Cap levels of ``index_id`` as plain floats, read off the rules class."""
    rules = rules_for(index_id)
    out: dict[str, float] = {}
    for attr, key in _LIMIT_ATTRS.items():
        value = getattr(rules, attr, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(value)
    return out


def _single_cap(limits: dict[str, float]) -> float | None:
    for key in ("single_cap", "company_trigger"):
        if key in limits:
            return limits[key]
    return None


def build_etf_history(root: str | os.PathLike[str], etf: str, *, top_n: int = TOP_N, max_days: int | None = None) -> dict[str, Any] | None:
    """History block for one ETF, or ``None`` when it has no snapshots."""
    etf = etf.upper()
    index_id = INDEX_FOR_ETF.get(etf, etf)
    dates = list_snapshots(root, etf)
    if max_days:
        dates = dates[-max_days:]
    if not dates:
        return None

    rows: list[dict[str, Any]] = []
    for d in dates:
        snap = load_snapshot(snapshot_path(root, etf, d))
        adr = [h.ticker for h in snap.equities() if h.is_adr]
        caps = apply_caps_to_snapshot(snap, index_id, as_of=d, adr_tickers=adr)
        meta = snap.meta or {}
        rows.append(
            {
                "date": d.isoformat(),
                "metrics": {k: float(v) for k, v in caps.metrics.items() if isinstance(v, (int, float))},
                "weights": {t: float(w) for t, w in caps.current_weights.items()},
                "shares_outstanding": _num(meta.get("shares_outstanding")),
                "total_net_assets": _num(snap.total_market_value()),
            }
        )

    limits = index_limits(index_id)
    cap = _single_cap(limits)
    latest = rows[-1]["weights"]
    chosen = [t for t, _ in sorted(latest.items(), key=lambda kv: -kv[1])[:top_n]]
    if cap is not None:
        ever_over = {t for row in rows for t, w in row["weights"].items() if w > cap}
        chosen += sorted(ever_over - set(chosen), key=lambda t: -max(r["weights"].get(t, 0.0) for r in rows))
    chosen = chosen[:MAX_TICKERS]

    metric_keys = sorted({k for row in rows for k in row["metrics"]})
    return {
        "index_id": index_id,
        "dates": [row["date"] for row in rows],
        "metrics": {k: [row["metrics"].get(k) for row in rows] for k in metric_keys},
        "tickers": chosen,
        "weights": {t: [row["weights"].get(t) for row in rows] for t in chosen},
        "shares_outstanding": [row["shares_outstanding"] for row in rows],
        "total_net_assets": [row["total_net_assets"] for row in rows],
        "limits": limits,
    }


def build_history(root: str | os.PathLike[str], etfs: Iterable[str] = DEFAULT_ETFS, *, top_n: int = TOP_N, max_days: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, Any] = {"generated_utc": stamp, "etfs": {}}
    for etf in etfs:
        block = build_etf_history(root, etf, top_n=top_n, max_days=max_days)
        if block is not None:
            out["etfs"][etf.upper()] = block
    return out


def write_history(root: str | os.PathLike[str], etfs: Iterable[str] = DEFAULT_ETFS, **kwargs: Any) -> Path:
    """Build and write ``reports/history.json``; returns its path."""
    data = build_history(root, etfs, **kwargs)
    path = history_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    log.info("wrote %s (%d ETFs)", path, len(data["etfs"]))
    return path


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None
