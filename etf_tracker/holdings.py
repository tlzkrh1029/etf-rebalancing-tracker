"""Normalized ETF holdings data model and JSON snapshot persistence.

Standard library only.

Every data source (issuer CSV/XLS downloads, index vendor files, ...) is
reduced to the same two dataclasses so the rest of the package never has to
know where a number came from:

* :class:`Holding` -- one line of an ETF's holdings file (an equity, a cash
  line, a futures contract, ...).
* :class:`Snapshot` -- all lines of one ETF on one ``as_of`` date plus a free
  form ``meta`` dictionary (shares outstanding, NAV, download URL, ...).

Conventions
-----------
* Weights are **fractions** (0.0-1.0) everywhere.  Formatting as percent is
  a reporting concern.
* Dates are ``datetime.date``.  JSON files store them as ISO ``YYYY-MM-DD``.
* Prices and market values are floats in the holding's ``currency`` (USD for
  the three ETFs tracked here).
* ``asset_class`` is one of :data:`ASSET_CLASSES`.
* Snapshot files live at ``<root>/data/normalized/<ETF>/<YYYY-MM-DD>.json``
  (see :func:`snapshot_path`).
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "ASSET_CLASSES",
    "EQUITY",
    "CASH",
    "DERIVATIVE",
    "OTHER",
    "WEIGHT_SUM_TOLERANCE",
    "MARKET_VALUE_TOLERANCE",
    "WEIGHT_CONSISTENCY_TOLERANCE",
    "Holding",
    "Snapshot",
    "save_snapshot",
    "load_snapshot",
    "snapshot_path",
    "list_snapshots",
    "latest_two",
    "write_snapshot_csv",
]

#: Allowed values of :attr:`Holding.asset_class`.
EQUITY = "equity"
CASH = "cash"
DERIVATIVE = "derivative"
OTHER = "other"
ASSET_CLASSES: tuple[str, ...] = (EQUITY, CASH, DERIVATIVE, OTHER)

#: ``validate()`` warns when the provided weights do not sum to 1 within
#: this absolute tolerance (0.5 percentage points).
WEIGHT_SUM_TOLERANCE = 0.005
#: ``validate()`` warns when ``market_value`` differs from ``shares * price``
#: by more than this relative amount.
MARKET_VALUE_TOLERANCE = 0.01
#: ``validate()`` warns when a provided weight differs from
#: ``market_value / total_market_value`` by more than this absolute amount
#: (0.25 percentage points; issuer files round weights to 2 decimals of %).
WEIGHT_CONSISTENCY_TOLERANCE = 0.0025

_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    """Return True for a finite int/float (bool excluded)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


_NULL_STRINGS = frozenset({"", "nan", "none", "null", "-", "--", "n/a", "na"})
_TRUE_STRINGS = frozenset({"1", "true", "t", "yes", "y"})
_FALSE_STRINGS = frozenset({"0", "false", "f", "no", "n"})


def _to_float(value: Any) -> float | None:
    """Coerce JSON scalars to float; ``None``, empty strings and non-finite
    numbers (NaN, +/-inf) become ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value) if _is_number(value) else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.lower() in _NULL_STRINGS:
            return None
        number = float(text)
        return number if math.isfinite(number) else None
    raise TypeError(f"cannot convert {value!r} to float")


def _to_bool(value: Any) -> bool | None:
    """Coerce JSON/CSV scalars to bool; ``None``/empty strings become ``None``.

    Unlike ``bool()``, the strings ``"false"``, ``"0"``, ``"no"`` map to
    ``False``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _NULL_STRINGS:
            return None
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        raise ValueError(f"cannot interpret {value!r} as a boolean")
    raise TypeError(f"cannot convert {value!r} to bool")


def _jsonable(obj: Any) -> Any:
    """Recursively convert dates, Paths, tuples and sets (and drop non-finite
    floats to ``None``) so ``json.dumps`` succeeds without a ``default`` hook."""
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _to_date(value: Any) -> date:
    """Accept a ``date``, ``datetime`` or ISO string and return a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip()[:10])
    raise TypeError(f"cannot convert {value!r} to a date")


def _json_default(obj: Any) -> Any:
    """``json.dumps`` fallback: ISO dates, Paths as strings, sets as lists."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Holding:
    """One line of an ETF holdings file.

    ``shares``, ``price``, ``market_value`` and ``weight`` are all optional
    because sources differ in what they publish.  :meth:`derived_price` and
    :meth:`derived_market_value` fill the gaps arithmetically when possible.

    ``weight`` is a fraction of the fund (0.0-1.0), as published by the
    source (it is *not* re-normalized here; see
    :meth:`Snapshot.normalized_weights`).
    """

    etf: str
    as_of: date
    ticker: str
    name: str
    asset_class: str
    shares: float | None
    price: float | None
    market_value: float | None
    weight: float | None
    currency: str = "USD"
    sector: str | None = None
    is_adr: bool | None = None
    company_id: str | None = None
    source: str = ""

    def is_equity(self) -> bool:
        """True when the line is a stock (as opposed to cash/derivatives)."""
        return self.asset_class == EQUITY

    def derived_price(self) -> float | None:
        """``price`` if present, else ``market_value / shares`` when possible."""
        if self.price is not None:
            return self.price
        if (
            self.market_value is not None
            and self.shares is not None
            and self.shares != 0
        ):
            return self.market_value / self.shares
        return None

    def derived_market_value(self) -> float | None:
        """``market_value`` if present, else ``shares * price`` when possible."""
        if self.market_value is not None:
            return self.market_value
        if self.shares is not None and self.price is not None:
            return self.shares * self.price
        return None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dictionary (``as_of`` as ISO string)."""
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["as_of"] = self.as_of.isoformat()
        return _jsonable(data)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Holding":
        """Inverse of :meth:`to_dict`; unknown keys are ignored.  Numeric and
        boolean fields accept the string spellings found in CSV exports
        (``"1,000"``, ``""``, ``"false"``)."""
        is_adr = _to_bool(data.get("is_adr"))
        return cls(
            etf=str(data["etf"]),
            as_of=_to_date(data["as_of"]),
            ticker=str(data["ticker"]),
            name=str(data.get("name") or ""),
            asset_class=str(data.get("asset_class") or EQUITY),
            shares=_to_float(data.get("shares")),
            price=_to_float(data.get("price")),
            market_value=_to_float(data.get("market_value")),
            weight=_to_float(data.get("weight")),
            currency=str(data.get("currency") or "USD"),
            sector=data.get("sector"),
            is_adr=is_adr,
            company_id=data.get("company_id"),
            source=str(data.get("source") or ""),
        )


@dataclass
class Snapshot:
    """All holdings of one ETF on one date.

    ``meta`` is free-form; keys other modules look for (all optional):
    ``shares_outstanding``, ``total_net_assets`` (or ``net_assets``),
    ``nav`` (per share), ``url``, ``downloaded_at``.
    """

    etf: str
    as_of: date
    source: str = ""
    holdings: list[Holding] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # -- views ------------------------------------------------------------

    def equities(self) -> list[Holding]:
        """Equity lines only, in file order."""
        return [h for h in self.holdings if h.is_equity()]

    def non_equities(self) -> list[Holding]:
        """Cash, derivative and other lines, in file order."""
        return [h for h in self.holdings if not h.is_equity()]

    def tickers(self) -> list[str]:
        """Tickers in file order (duplicates preserved)."""
        return [h.ticker for h in self.holdings]

    def by_ticker(self) -> dict[str, Holding]:
        """Map ticker -> holding.  On duplicate tickers the first line wins
        (``validate()`` reports duplicates)."""
        result: dict[str, Holding] = {}
        for h in self.holdings:
            result.setdefault(h.ticker, h)
        return result

    def _universe(self, include_cash: bool) -> list[Holding]:
        return list(self.holdings) if include_cash else self.equities()

    def total_market_value(self, include_cash: bool = True) -> float:
        """Sum of (derived) market values over the chosen universe.

        Lines without a derivable market value contribute nothing.
        """
        total = 0.0
        for h in self._universe(include_cash):
            mv = h.derived_market_value()
            if mv is not None:
                total += mv
        return total

    def _informative(self, include_cash: bool) -> list[Holding]:
        """Lines of the universe carrying a derivable market value or a weight."""
        return [
            h
            for h in self._universe(include_cash)
            if h.derived_market_value() is not None or h.weight is not None
        ]

    def weight_basis(self, include_cash: bool = True) -> str:
        """Which inputs :meth:`normalized_weights` would use.

        Lines with neither a derivable market value nor a weight carry no
        information and are ignored.  Returns ``'market_value'`` when every
        informative line has a derivable market value, ``'weight'`` when every
        informative line has a published weight, and, for a mixed universe,
        the basis that covers more lines (ties go to market values, so a cash
        line published with a weight only never swamps priced equities).
        ``'none'`` when nothing is usable.
        """
        informative = self._informative(include_cash)
        if not informative:
            return "none"
        n_mv = sum(1 for h in informative if h.derived_market_value() is not None)
        n_w = sum(1 for h in informative if h.weight is not None)
        if n_mv == len(informative):
            return "market_value"
        if n_w == len(informative):
            return "weight"
        return "market_value" if n_mv >= n_w else "weight"

    def normalized_weights(self, include_cash: bool = True) -> dict[str, float]:
        """Weights (fractions) re-normalized to sum to 1 over the universe.

        Market values are used when every informative line in the universe
        has one (given or derivable from shares * price); otherwise the
        published weights are used (see :meth:`weight_basis` for mixed
        universes).  Lines without the chosen input count as 0.  Duplicate
        tickers are aggregated.  ``include_cash=False`` restricts the
        universe to equity lines (cash, derivatives and 'other' excluded).
        Returns an empty dict when nothing usable is available or the total
        is zero.
        """
        universe = self._universe(include_cash)
        basis = self.weight_basis(include_cash)
        raw: dict[str, float] = {}
        if basis == "market_value":
            for h in universe:
                mv = h.derived_market_value()
                raw[h.ticker] = raw.get(h.ticker, 0.0) + (mv or 0.0)
        elif basis == "weight":
            for h in universe:
                raw[h.ticker] = raw.get(h.ticker, 0.0) + (h.weight or 0.0)
        else:
            return {}
        total = sum(raw.values())
        if total == 0 or not math.isfinite(total):
            return {}
        return {t: v / total for t, v in raw.items()}

    # -- validation -------------------------------------------------------

    def fill_derived_fields(self) -> list[str]:
        """Fill ``price`` from ``market_value / shares`` and ``market_value``
        from ``shares * price`` where they are missing.  Returns one note per
        field filled.  Mutates the holdings in place."""
        notes: list[str] = []
        for h in self.holdings:
            if h.price is None:
                derived = h.derived_price()
                if derived is not None:
                    h.price = derived
                    notes.append(
                        f"{h.ticker}: derived price {derived:.6g} from market_value/shares"
                    )
            if h.market_value is None:
                derived_mv = h.derived_market_value()
                if derived_mv is not None:
                    h.market_value = derived_mv
                    notes.append(
                        f"{h.ticker}: derived market_value {derived_mv:.6g} from shares*price"
                    )
        return notes

    def validate(self) -> list[str]:
        """Sanity-check the snapshot and return human-readable warnings.

        Side effect: missing ``price``/``market_value`` fields that can be
        derived arithmetically are filled in (and reported).  An empty list
        means the snapshot looks internally consistent.
        """
        warnings: list[str] = []
        if not self.holdings:
            warnings.append("snapshot has no holdings")
            return warnings

        for h in self.holdings:
            if h.asset_class not in ASSET_CLASSES:
                warnings.append(
                    f"{h.ticker}: unknown asset_class {h.asset_class!r} "
                    f"(expected one of {', '.join(ASSET_CLASSES)})"
                )
            if h.etf != self.etf:
                warnings.append(f"{h.ticker}: holding.etf {h.etf!r} != snapshot.etf {self.etf!r}")
            if h.as_of != self.as_of:
                warnings.append(
                    f"{h.ticker}: holding.as_of {h.as_of.isoformat()} != "
                    f"snapshot.as_of {self.as_of.isoformat()}"
                )
            if not h.ticker or not h.ticker.strip():
                warnings.append(f"holding {h.name!r} has an empty ticker")

        seen: dict[str, int] = {}
        for h in self.holdings:
            seen[h.ticker] = seen.get(h.ticker, 0) + 1
        for ticker, count in seen.items():
            if count > 1:
                warnings.append(f"duplicate ticker {ticker!r} appears {count} times")

        warnings.extend(self.fill_derived_fields())

        for h in self.equities():
            if h.shares is not None and h.shares < 0:
                warnings.append(f"{h.ticker}: negative shares {h.shares}")
            if h.weight is not None and h.weight < 0:
                warnings.append(f"{h.ticker}: negative weight {h.weight}")
            if h.price is not None and h.price <= 0:
                warnings.append(f"{h.ticker}: non-positive price {h.price}")
            if h.shares is not None and h.price is None:
                warnings.append(f"{h.ticker}: shares given but price missing and not derivable")
            if h.market_value is None and h.weight is None:
                warnings.append(f"{h.ticker}: neither market_value nor weight available")

        for h in self.holdings:
            if (
                h.shares is not None
                and h.price is not None
                and h.market_value is not None
                and h.market_value != 0
            ):
                implied = h.shares * h.price
                rel = abs(implied - h.market_value) / abs(h.market_value)
                if rel > MARKET_VALUE_TOLERANCE:
                    warnings.append(
                        f"{h.ticker}: market_value {h.market_value:.6g} differs from "
                        f"shares*price {implied:.6g} by {rel:.2%}"
                    )

        weighted = [h for h in self.holdings if h.weight is not None]
        if weighted:
            total_w = sum(h.weight for h in weighted if h.weight is not None)
            if 50.0 <= total_w <= 150.0:
                warnings.append(
                    f"weights sum to {total_w:.4g}: they look like percents, "
                    "fractions (0-1) are expected"
                )
            elif abs(total_w - 1.0) > WEIGHT_SUM_TOLERANCE:
                missing = len(self.holdings) - len(weighted)
                suffix = f" ({missing} holdings have no weight)" if missing else ""
                warnings.append(f"weights sum to {total_w:.6f}, expected 1 +/- {WEIGHT_SUM_TOLERANCE}{suffix}")

        basis = self.weight_basis(include_cash=True)
        if basis == "market_value":
            for h in self.holdings:
                if h.derived_market_value() is None and h.weight is not None:
                    warnings.append(
                        f"{h.ticker}: has a weight but no market value; it counts as 0 in "
                        "market-value based normalized weights"
                    )
        elif basis == "weight":
            for h in self.holdings:
                if h.weight is None and h.derived_market_value() is not None:
                    warnings.append(
                        f"{h.ticker}: has a market value but no weight; it counts as 0 in "
                        "weight based normalized weights"
                    )

        total_mv = self.total_market_value(include_cash=True)
        if total_mv > 0 and basis == "market_value":
            worst: tuple[float, str] | None = None
            count = 0
            for h in self.holdings:
                mv = h.derived_market_value()
                if h.weight is None or mv is None:
                    continue
                diff = abs(mv / total_mv - h.weight)
                if diff > WEIGHT_CONSISTENCY_TOLERANCE:
                    count += 1
                    if worst is None or diff > worst[0]:
                        worst = (diff, h.ticker)
            if worst is not None:
                warnings.append(
                    f"{count} holding(s) have weight inconsistent with market_value/total "
                    f"by more than {WEIGHT_CONSISTENCY_TOLERANCE:.4f} (worst {worst[1]}: {worst[0]:.4f})"
                )
        return warnings

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dictionary (dates as ISO strings, also inside ``meta``).

        ``json.loads(json.dumps(snapshot.to_dict()))`` round-trips through
        :meth:`from_dict`; note that a date stored in ``meta`` comes back as
        its ISO string.
        """
        return {
            "etf": self.etf,
            "as_of": self.as_of.isoformat(),
            "source": self.source,
            "meta": _jsonable(dict(self.meta)),
            "holdings": [h.to_dict() for h in self.holdings],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Snapshot":
        """Inverse of :meth:`to_dict`."""
        return cls(
            etf=str(data["etf"]),
            as_of=_to_date(data["as_of"]),
            source=str(data.get("source") or ""),
            holdings=[Holding.from_dict(h) for h in (data.get("holdings") or [])],
            meta=dict(data.get("meta") or {}),
        )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def snapshot_path(root: str | os.PathLike[str], etf: str, as_of: date) -> Path:
    """``<root>/data/normalized/<ETF>/<YYYY-MM-DD>.json`` (ETF upper-cased)."""
    return Path(root) / "data" / "normalized" / etf.upper() / f"{_to_date(as_of).isoformat()}.json"


def save_snapshot(snapshot: Snapshot, path: str | os.PathLike[str]) -> Path:
    """Write ``snapshot`` as pretty-printed JSON to ``path`` (parents created).

    The write is atomic (temp file + ``os.replace``) so a crash never leaves a
    truncated snapshot behind.  Returns the path written.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    text = json.dumps(snapshot.to_dict(), indent=2, sort_keys=False, default=_json_default)
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def load_snapshot(path: str | os.PathLike[str]) -> Snapshot:
    """Read a snapshot written by :func:`save_snapshot`."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return Snapshot.from_dict(data)


def list_snapshots(root: str | os.PathLike[str], etf: str) -> list[date]:
    """Sorted ``as_of`` dates of the snapshots stored under ``root`` for ``etf``.

    Only files named ``YYYY-MM-DD.json`` count; anything else in the directory
    (temp files, notes) is ignored.  Missing directory -> empty list.
    """
    directory = snapshot_path(root, etf, date(2000, 1, 1)).parent
    if not directory.is_dir():
        return []
    dates: list[date] = []
    for entry in directory.iterdir():
        match = _DATE_FILE_RE.match(entry.name)
        if match and entry.is_file():
            try:
                dates.append(date.fromisoformat(match.group(1)))
            except ValueError:
                continue
    return sorted(dates)


def latest_two(root: str | os.PathLike[str], etf: str) -> tuple[Snapshot, Snapshot] | None:
    """Load the two most recent snapshots as ``(prev, curr)``.

    Returns ``None`` when fewer than two snapshots exist.
    """
    dates = list_snapshots(root, etf)
    if len(dates) < 2:
        return None
    prev_date, curr_date = dates[-2], dates[-1]
    return (
        load_snapshot(snapshot_path(root, etf, prev_date)),
        load_snapshot(snapshot_path(root, etf, curr_date)),
    )


_CSV_COLUMNS = (
    "etf",
    "as_of",
    "ticker",
    "name",
    "asset_class",
    "shares",
    "price",
    "market_value",
    "weight",
    "currency",
    "sector",
    "is_adr",
    "company_id",
    "source",
)


def write_snapshot_csv(snapshot: Snapshot, path: str | os.PathLike[str]) -> Path:
    """Write the holdings as a flat CSV for human inspection (weights stay
    fractions).  Returns the path written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for h in snapshot.holdings:
            row = h.to_dict()
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in _CSV_COLUMNS})
    return target
