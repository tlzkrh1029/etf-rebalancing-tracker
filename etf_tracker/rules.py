"""Index capping rules and rebalance schedules for SOXX, QQQ and IGV.

This module answers two questions for each tracked ETF:

1. *What would the index do to today's weights at the next rebalance?*
   :meth:`IndexRules.apply_caps` re-applies the index's capping methodology
   to a set of constituents and returns target weights, per-ticker deltas
   (``target - current``; a negative delta means forced selling pressure) and
   the constraints that bind.
2. *When does it do it?*  :meth:`IndexRules.schedule` and
   :meth:`IndexRules.next_events` give the reference, announcement and
   effective dates of each rebalance / reconstitution.

All weights inside this module are fractions (``0.08`` means 8%).  Input
weights are normalised to sum to one before capping (ETF holdings files carry
cash lines that are not index constituents), and every feasible result sums
to one within ``1e-9``.

Redistribution engine
---------------------
Every rule below is expressed with one primitive, :func:`_fill_to_target`:
given initial weights, a per-name cap and a target total, all *uncapped*
names share one adjustment factor ``AF`` and each final weight is
``min(cap, AF * initial)``, chosen so the totals match.  This is exactly the
procedure in Nasdaq's "Nasdaq Index Weight Calculations" (20 May 2026,
https://indexes.nasdaqomx.com/docs/Nasdaq_Index_Weight_Calculations.pdf):
"within a capping level all uncapped Index Securities share an adjustment
factor", ``w* = min(C, AF * w)`` and ``sum(w*) = sum(w)``.  It is also the
fixed point of the iterative "cap and redistribute the excess pro rata to the
names below their cap" wording used by the ICE/NYSE and S&P methodologies, so
one engine serves all three indices.

Infeasible caps
---------------
When the caps cannot absorb the weight (e.g. fewer than 13 names with an 8%
cap, or an aggregate rule that has no eligible recipients left) the engine
stops with every name at its cap, the result's ``feasible`` flag is
``False``, the shortfall is written to ``notes`` and the weights sum to less
than one.  The methodologies do not specify what happens in this situation
(it cannot arise with real index memberships) so no renormalisation is
invented.

Sources (cited again on each rules class)
-----------------------------------------
* SOXX / NYSE Semiconductor Index: iShares 497 prospectus supplement, June
  2021, https://www.sec.gov/Archives/edgar/data/1100663/000119312521126827/d82642d497.htm
  (index renamed from ICE Semiconductor Index effective 2023-11-03; current
  ICE methodology document not publicly verified -- **to re-verify**).
* QQQ / Nasdaq-100: Nasdaq-100 Index Methodology dated 2026-04-30,
  https://indexes.nasdaqomx.com/docs/methodology_NDX.pdf, and "Nasdaq Index
  Weight Calculations" (link above).
* IGV / S&P North American Expanded Technology Software Index: BofA Finance
  424B2 pricing supplement dated 2026-05-11,
  https://www.sec.gov/Archives/edgar/data/0001682472/000191870426012955/form424b2.htm
  (**to re-verify against the S&P DJI methodology**, which was not reachable).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

__all__ = [
    "Breach",
    "CapResult",
    "Constituent",
    "EVENT_KINDS",
    "IGVRules",
    "INDEX_FOR_ETF",
    "IndexRules",
    "QQQRules",
    "RebalanceEvent",
    "SHARE_CLASS_GROUPS",
    "SOXXRules",
    "TriggerCheck",
    "aggregate_by_company",
    "rules_for",
]

# Tolerance used for every "exceeds the cap" comparison, so that a weight that
# sits exactly on a cap after a previous pass is not treated as a new breach.
_EPS = 1e-12
_MAX_ITER = 200

#: ETF ticker -> index identifier used by :func:`rules_for`.
INDEX_FOR_ETF: dict[str, str] = {"SOXX": "SOXX", "QQQ": "QQQ", "IGV": "IGV"}

#: Share classes of the same company that index methodologies aggregate at
#: the company level (Nasdaq-100 "company-level" constraints).  Map each
#: ticker to a stable company identifier; extend the dict to add groups.
SHARE_CLASS_GROUPS: dict[str, str] = {
    "GOOGL": "ALPHABET",
    "GOOG": "ALPHABET",
    "FOXA": "FOX_CORP",
    "FOX": "FOX_CORP",
}

#: Valid ``kind`` values of :class:`RebalanceEvent` / ``event_type`` arguments.
EVENT_KINDS: tuple[str, ...] = (
    "quarterly_rebalance",
    "annual_reconstitution",
    "semiannual_reconstitution",
    "special_rebalance",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Constituent:
    """One index/ETF constituent with its current weight (fraction).

    ``company_id`` defaults to the ticker unless the ticker appears in
    :data:`SHARE_CLASS_GROUPS`, in which case all share classes of the company
    get the same id (GOOGL and GOOG -> ``"ALPHABET"``).
    """

    ticker: str
    weight: float
    company_id: str | None = None
    is_adr: bool = False
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.ticker:
            raise ValueError("Constituent.ticker must be a non-empty string")
        if self.weight < 0:
            raise ValueError(f"{self.ticker}: weight must be >= 0, got {self.weight}")
        if self.company_id is None:
            object.__setattr__(
                self, "company_id", SHARE_CLASS_GROUPS.get(self.ticker.upper(), self.ticker)
            )


@dataclass(frozen=True)
class Breach:
    """A constraint that the *current* weights violate."""

    rule: str
    description: str
    tickers: tuple[str, ...]
    value: float
    limit: float


@dataclass(frozen=True)
class TriggerCheck:
    """Outcome of a trigger test such as the Nasdaq-100 special rebalance.

    Truthy when ``triggered`` is ``True`` so it can be used like a ``bool``.
    """

    triggered: bool
    reasons: tuple[str, ...]
    metrics: dict[str, float]

    def __bool__(self) -> bool:
        return self.triggered


@dataclass
class CapResult:
    """Result of re-applying an index's capping rules to a set of weights.

    Attributes:
        index_id: ``'SOXX'``, ``'QQQ'`` or ``'IGV'``.
        event_type: the event kind whose rules were applied.
        as_of: the date the input weights refer to.
        current_weights: input weights normalised to sum to one.
        target_weights: weights after capping (sum to one when ``feasible``).
        deltas: ``target - current`` per ticker; negative = forced selling.
        binding_constraints: human-readable list of the constraints that
            changed at least one weight.
        metrics: aggregate metrics computed on the *current* weights, plus
            ``turnover`` (one-way, ``sum(|delta|)/2``) and ``n_constituents``.
        target_metrics: the same index-specific metrics on the target weights.
        feasible: ``False`` when the caps could not absorb all weight.
        notes: free-text remarks (normalisation, infeasibility, approximations).
    """

    index_id: str
    event_type: str
    as_of: date
    current_weights: dict[str, float]
    target_weights: dict[str, float]
    deltas: dict[str, float]
    binding_constraints: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    target_metrics: dict[str, float] = field(default_factory=dict)
    feasible: bool = True
    notes: list[str] = field(default_factory=list)

    def sorted_deltas(self) -> list[tuple[str, float]]:
        """Return ``(ticker, delta)`` pairs, most negative (forced selling) first."""
        return sorted(self.deltas.items(), key=lambda kv: (kv[1], kv[0]))

    @property
    def forced_sellers(self) -> list[str]:
        """Tickers whose target weight is below their current weight."""
        return [t for t, d in self.sorted_deltas() if d < -_EPS]

    @property
    def forced_buyers(self) -> list[str]:
        """Tickers whose target weight is above their current weight."""
        return [t for t, d in reversed(self.sorted_deltas()) if d > _EPS]


@dataclass(frozen=True)
class RebalanceEvent:
    """One scheduled index event.

    ``effective_trade_date`` is the session at whose close index funds trade
    (the third Friday, or the previous session when that Friday is a market
    holiday); ``effective_date`` is the first session on which the new weights
    are live.  ``announcement_date`` is ``None`` when the methodology does not
    give a deterministic rule.
    """

    index_id: str
    kind: str
    reference_date: date
    announcement_date: date | None
    effective_trade_date: date
    effective_date: date
    notes: str = ""


# ---------------------------------------------------------------------------
# Shared numerical helpers
# ---------------------------------------------------------------------------


def _normalise(constituents: Sequence[Constituent]) -> tuple[dict[str, float], float]:
    """Return ``{ticker: weight / total}`` and the original total.

    Raises ``ValueError`` on duplicate tickers or a non-positive total.
    """
    weights: dict[str, float] = {}
    for c in constituents:
        if c.ticker in weights:
            raise ValueError(f"duplicate ticker {c.ticker!r}")
        weights[c.ticker] = float(c.weight)
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("constituent weights must sum to a positive number")
    return {t: w / total for t, w in weights.items()}, total


def _fill_to_target(
    weights: Mapping[str, float],
    caps: Mapping[str, float],
    target: float,
) -> tuple[dict[str, float], set[str], float]:
    """Scale ``weights`` to sum to ``target`` while honouring per-name caps.

    All uncapped names share one adjustment factor; a name whose scaled weight
    would exceed its cap is fixed at the cap and the factor is recomputed
    (classic water-filling).  Returns ``(final_weights, capped_names,
    shortfall)`` where ``shortfall > 0`` means every name is at its cap and
    ``target - sum(final)`` could not be placed (infeasible caps).
    """
    if not weights:
        return {}, set(), target
    capped: set[str] = set()
    final: dict[str, float] = {}
    for _ in range(len(weights) + 1):
        uncapped = [t for t in weights if t not in capped]
        free = target - sum(caps[t] for t in capped)
        denom = sum(weights[t] for t in uncapped)
        if not uncapped or denom <= 0.0:
            final = {t: (caps[t] if t in capped else 0.0) for t in weights}
            return final, capped, target - sum(final.values())
        factor = free / denom
        newly = {t for t in uncapped if factor * weights[t] > caps[t] + _EPS}
        if not newly:
            final = {t: (caps[t] if t in capped else factor * weights[t]) for t in weights}
            return final, capped, 0.0
        capped |= newly
    raise RuntimeError("water-filling did not converge")  # pragma: no cover


def _fmt_pct(x: float) -> str:
    """Format a fraction as a percent string for human-readable notes."""
    return f"{x * 100:.4g}%"


def _rank_desc(weights: Mapping[str, float]) -> list[str]:
    """Tickers sorted by weight descending, ties broken alphabetically."""
    return sorted(weights, key=lambda t: (-weights[t], t))


def _one_way_turnover(deltas: Mapping[str, float]) -> float:
    return sum(abs(d) for d in deltas.values()) / 2.0


def aggregate_by_company(constituents: Sequence[Constituent]) -> dict[str, float]:
    """Sum raw (un-normalised) weights per ``company_id``."""
    out: dict[str, float] = {}
    for c in constituents:
        out[c.company_id or c.ticker] = out.get(c.company_id or c.ticker, 0.0) + float(c.weight)
    return out


def _company_weights(
    constituents: Sequence[Constituent], weights: Mapping[str, float]
) -> tuple[dict[str, float], dict[str, str]]:
    """Return ``({company_id: weight}, {ticker: company_id})`` from normalised weights."""
    company_of = {c.ticker: (c.company_id or c.ticker) for c in constituents}
    comp: dict[str, float] = {}
    for t, w in weights.items():
        cid = company_of[t]
        comp[cid] = comp.get(cid, 0.0) + w
    return comp, company_of


# ---------------------------------------------------------------------------
# Calendar access (lazy, so capping never depends on the calendar module)
# ---------------------------------------------------------------------------


def _cal():
    """Import :mod:`etf_tracker.market_calendar` on first use."""
    from etf_tracker import market_calendar

    return market_calendar


def _first_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def _trade_session_for_third_friday(year: int, month: int) -> date:
    """The session at whose close a third-Friday rebalance trades.

    Normally the third Friday itself; when that day is a market holiday (Good
    Friday 2008-03-21, Juneteenth 2027-06-18) the previous session is used.
    """
    cal = _cal()
    return cal.prev_trading_day(cal.third_friday(year, month), include=True)


def _nth_trading_day_before(d: date, n: int) -> date:
    """The ``n``-th trading day strictly before ``d``."""
    cal = _cal()
    for _ in range(n):
        d = cal.prev_trading_day(d)
    return d


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class IndexRules(ABC):
    """Capping rules and calendar of one index.  Subclasses set ``index_id``."""

    index_id: str = ""
    #: Months of the quarterly cycle (all three indices use the March cycle).
    QUARTER_MONTHS: tuple[int, ...] = (3, 6, 9, 12)

    # -- capping ----------------------------------------------------------

    @abstractmethod
    def apply_caps(
        self,
        constituents: Sequence[Constituent],
        as_of: date,
        event_type: str = "quarterly_rebalance",
    ) -> CapResult:
        """Re-apply the index capping rules to ``constituents``."""

    @abstractmethod
    def check_constraints(
        self,
        constituents: Sequence[Constituent],
        event_type: str = "quarterly_rebalance",
    ) -> list[Breach]:
        """List the capping constraints the current weights breach."""

    @abstractmethod
    def compute_metrics(self, constituents: Sequence[Constituent]) -> dict[str, float]:
        """Index-specific aggregate metrics of the (normalised) current weights."""

    # -- calendar ---------------------------------------------------------

    @abstractmethod
    def schedule(self, year: int) -> list[RebalanceEvent]:
        """All scheduled events whose effective trade date falls in ``year``."""

    def next_events(self, today: date, n: int = 4) -> list[RebalanceEvent]:
        """The next ``n`` events whose trade date is on or after ``today``."""
        out: list[RebalanceEvent] = []
        year = today.year
        while len(out) < n and year <= today.year + 5:
            for ev in self.schedule(year):
                if ev.effective_trade_date >= today:
                    out.append(ev)
            year += 1
        out.sort(key=lambda e: (e.effective_trade_date, e.kind))
        return out[:n]

    def event_type_for(self, as_of: date) -> str:
        """Kind of the next scheduled event (used to choose which rules apply).

        When a rebalance and a reconstitution share a trade date, the
        reconstitution wins because it applies the superset of rules.
        """
        events = self.next_events(as_of, n=2)
        if not events:
            return "quarterly_rebalance"
        same_day = [e for e in events if e.effective_trade_date == events[0].effective_trade_date]
        for e in same_day:
            if e.kind.endswith("reconstitution"):
                return e.kind
        return same_day[0].kind

    # -- shared plumbing --------------------------------------------------

    def _result(
        self,
        constituents: Sequence[Constituent],
        as_of: date,
        event_type: str,
        current: dict[str, float],
        target: dict[str, float],
        total_in: float,
        binding: list[str],
        feasible: bool,
        notes: list[str],
    ) -> CapResult:
        deltas = {t: target[t] - current[t] for t in current}
        if abs(total_in - 1.0) > 1e-6:
            notes.insert(0, f"input weights summed to {total_in:.6f}; normalised to 1.0")
        metrics = self._metrics_from_weights(constituents, current)
        metrics["turnover"] = _one_way_turnover(deltas)
        metrics["n_constituents"] = float(len(current))
        target_metrics = self._metrics_from_weights(constituents, target)
        return CapResult(
            index_id=self.index_id,
            event_type=event_type,
            as_of=as_of,
            current_weights=current,
            target_weights=target,
            deltas=deltas,
            binding_constraints=binding,
            metrics=metrics,
            target_metrics=target_metrics,
            feasible=feasible,
            notes=notes,
        )

    def compute_metrics(self, constituents: Sequence[Constituent]) -> dict[str, float]:
        current, _ = _normalise(constituents)
        return self._metrics_from_weights(constituents, current)

    @abstractmethod
    def _metrics_from_weights(
        self, constituents: Sequence[Constituent], weights: Mapping[str, float]
    ) -> dict[str, float]:
        """Metrics for an arbitrary weight vector over ``constituents``."""


# ---------------------------------------------------------------------------
# SOXX -- NYSE Semiconductor Index
# ---------------------------------------------------------------------------


class SOXXRules(IndexRules):
    """NYSE Semiconductor Index (formerly ICE Semiconductor Index) rules.

    Source: iShares 497 prospectus supplement, June 2021
    (https://www.sec.gov/Archives/edgar/data/1100663/000119312521126827/d82642d497.htm).
    Third-party 2024-2025 sources still cite the same caps; the current ICE
    methodology document has not been verified -- **to re-verify**.

    Weighting: 30 largest US-listed semiconductor companies by float-adjusted
    market cap, subject to

    (a) every constituent capped at 8%, excess redistributed pro rata to the
        constituents below their cap;
    (b) constituents outside the *initial* five largest (ranked by uncapped
        weight) capped at 4%, excess redistributed to the five largest below
        8% and to the other constituents below 4%;
    (c) cumulative ADR weight capped at 10%, the reduction applied
        proportionally across ADRs and redistributed to non-ADRs among the
        five largest below 8% and the other non-ADRs below 4%;

    iterated until all limits hold.  (a) and (b) together are one
    water-filling pass with per-name caps; (c) is applied afterwards and the
    pass is repeated until nothing changes.

    Calendar: annual reconstitution in September (eligibility data as of the
    last trading day of July, weights as of the last trading day of August,
    announced after the close of the first Friday of September, effective
    after the close of the third Friday of September); quarterly rebalances
    after the close of the third Friday of March, June and December with
    data as of the last trading day of the preceding month and announcement
    after the close of the first Friday of the rebalance month.  Share
    updates above 10% are implemented monthly (not modelled as events).
    """

    index_id = "SOXX"
    SINGLE_CAP = 0.08
    OUTSIDE_TOP5_CAP = 0.04
    TOP_N = 5
    ADR_CAP = 0.10

    def initial_top5(self, weights: Mapping[str, float]) -> list[str]:
        """The five largest names by uncapped weight (ties broken alphabetically)."""
        return _rank_desc(weights)[: self.TOP_N]

    def _caps(self, weights: Mapping[str, float], top5: Iterable[str]) -> dict[str, float]:
        top = set(top5)
        return {t: (self.SINGLE_CAP if t in top else self.OUTSIDE_TOP5_CAP) for t in weights}

    def apply_caps(
        self,
        constituents: Sequence[Constituent],
        as_of: date,
        event_type: str = "quarterly_rebalance",
    ) -> CapResult:
        current, total_in = _normalise(constituents)
        adrs = {c.ticker for c in constituents if c.is_adr}
        top5 = self.initial_top5(current)
        caps = self._caps(current, top5)

        w = dict(current)
        binding: list[str] = []
        notes: list[str] = []
        feasible = True
        converged = False
        for _ in range(_MAX_ITER):
            # (a) + (b): single-name caps, pro rata redistribution to names below cap.
            filled, capped, shortfall = _fill_to_target(w, caps, 1.0)
            if shortfall > _EPS:
                feasible = False
                notes.append(
                    f"single-name caps infeasible: {_fmt_pct(shortfall)} could not be placed"
                )
            w = filled
            over8 = sorted(t for t in capped if t in top5)
            over4 = sorted(t for t in capped if t not in top5)
            for label, names in (
                (f"SOXX 8% single-name cap: {', '.join(over8)}", over8),
                (f"SOXX 4% cap outside initial top-5: {', '.join(over4)}", over4),
            ):
                if names and label not in binding:
                    binding.append(label)

            # (c): aggregate ADR cap.
            adr_sum = sum(w[t] for t in adrs)
            if adr_sum > self.ADR_CAP + _EPS:
                scale = self.ADR_CAP / adr_sum
                for t in adrs:
                    w[t] *= scale
                non_adr = {t: w[t] for t in w if t not in adrs}
                filled, _, shortfall = _fill_to_target(
                    non_adr, caps, 1.0 - self.ADR_CAP
                )
                w.update(filled)
                label = f"SOXX 10% aggregate ADR cap: {', '.join(sorted(adrs))}"
                if label not in binding:
                    binding.append(label)
                if shortfall > _EPS:
                    feasible = False
                    notes.append(
                        f"ADR cap infeasible: {_fmt_pct(shortfall)} could not be placed on non-ADRs"
                    )
                    converged = True
                    break
                continue  # re-check the single-name caps after the ADR pass
            converged = True
            break
        if not converged:
            feasible = False
            notes.append("capping did not converge within the iteration limit")
        return self._result(
            constituents, as_of, event_type, current, w, total_in, binding, feasible, notes
        )

    def check_constraints(
        self,
        constituents: Sequence[Constituent],
        event_type: str = "quarterly_rebalance",
    ) -> list[Breach]:
        current, _ = _normalise(constituents)
        adrs = {c.ticker for c in constituents if c.is_adr}
        top5 = set(self.initial_top5(current))
        breaches: list[Breach] = []
        over8 = tuple(t for t in _rank_desc(current) if current[t] > self.SINGLE_CAP + _EPS)
        if over8:
            breaches.append(
                Breach(
                    "soxx_single_cap_8",
                    "constituent above the 8% single-name cap",
                    over8,
                    max(current[t] for t in over8),
                    self.SINGLE_CAP,
                )
            )
        over4 = tuple(
            t
            for t in _rank_desc(current)
            if t not in top5 and current[t] > self.OUTSIDE_TOP5_CAP + _EPS
        )
        if over4:
            breaches.append(
                Breach(
                    "soxx_outside_top5_cap_4",
                    "constituent outside the initial top-5 above the 4% cap",
                    over4,
                    max(current[t] for t in over4),
                    self.OUTSIDE_TOP5_CAP,
                )
            )
        adr_sum = sum(current[t] for t in adrs)
        if adr_sum > self.ADR_CAP + _EPS:
            breaches.append(
                Breach(
                    "soxx_adr_aggregate_10",
                    "cumulative ADR weight above 10%",
                    tuple(sorted(adrs)),
                    adr_sum,
                    self.ADR_CAP,
                )
            )
        return breaches

    def _metrics_from_weights(
        self, constituents: Sequence[Constituent], weights: Mapping[str, float]
    ) -> dict[str, float]:
        adrs = {c.ticker for c in constituents if c.is_adr}
        ranked = _rank_desc(weights)
        top5 = ranked[: self.TOP_N]
        return {
            "top5_sum": sum(weights[t] for t in top5),
            "adr_sum": sum(weights[t] for t in adrs),
            "names_over_8": float(sum(1 for t in weights if weights[t] > self.SINGLE_CAP + _EPS)),
            "names_over_4_outside_top5": float(
                sum(1 for t in ranked[self.TOP_N :] if weights[t] > self.OUTSIDE_TOP5_CAP + _EPS)
            ),
            "max_weight": weights[ranked[0]] if ranked else 0.0,
        }

    def schedule(self, year: int) -> list[RebalanceEvent]:
        cal = _cal()
        events: list[RebalanceEvent] = []
        for month in self.QUARTER_MONTHS:
            trade = _trade_session_for_third_friday(year, month)
            ref_year, ref_month = (year, month - 1) if month > 1 else (year - 1, 12)
            reference = cal.last_trading_day_of_month(ref_year, ref_month)
            announcement = cal.prev_trading_day(_first_friday(year, month), include=True)
            effective = cal.next_trading_day(trade)
            if month == 9:
                eligibility = cal.last_trading_day_of_month(year, 7)
                events.append(
                    RebalanceEvent(
                        self.index_id,
                        "annual_reconstitution",
                        reference,
                        announcement,
                        trade,
                        effective,
                        notes=(
                            f"Annual reconstitution; eligibility data as of {eligibility}, "
                            "weights as of the last trading day of August; announced after the "
                            "close of the first Friday of September; effective after the close of "
                            "the third Friday."
                        ),
                    )
                )
            else:
                events.append(
                    RebalanceEvent(
                        self.index_id,
                        "quarterly_rebalance",
                        reference,
                        announcement,
                        trade,
                        effective,
                        notes=(
                            "Quarterly re-capping, no constituent changes; input data as of the "
                            "last trading day of the preceding month; announced after the close "
                            "of the first Friday of the rebalance month."
                        ),
                    )
                )
        return events


# ---------------------------------------------------------------------------
# QQQ -- Nasdaq-100 Index
# ---------------------------------------------------------------------------


class QQQRules(IndexRules):
    """Nasdaq-100 Index (NDX) rules.

    Sources: Nasdaq-100 Index Methodology dated 2026-04-30
    (https://indexes.nasdaqomx.com/docs/methodology_NDX.pdf) and "Nasdaq
    Index Weight Calculations" dated 20 May 2026
    (https://indexes.nasdaqomx.com/docs/Nasdaq_Index_Weight_Calculations.pdf).

    Modified market-cap weighting with constraints applied at the *company*
    level (share classes aggregated, GOOGL+GOOG = Alphabet):

    * Stage 1: if any company's initial weight exceeds 24%, weights are
      adjusted so that no company exceeds 20%.
    * Stage 2: company weights above 4.5% are summed; if the sum is 48% or
      more, that cohort's aggregate weight is adjusted down to 40%.  To
      preserve the rank order of the initial company weights, companies
      initially below 4.5% may also be adjusted downward.
    * The two stages repeat until both constraints hold.

    At the December annual reconstitution the resulting *security* weights
    are further constrained: Stage 1: if any security exceeds 15%, adjust so
    none exceeds 14%; Stage 2: if the five largest securities sum to 40% or
    more, that group's aggregate is adjusted down to 38.5% and any security
    outside the five largest is capped at the lesser of 4.4% or the weight
    of the fifth-largest security; repeat until both hold.

    Implementation of the "aggregate down to X%" steps follows the Weight
    Calculations document: the cohort is a capping level whose members share
    one adjustment factor (``X / cohort_sum``, which preserves their rank
    order), and the remaining names share another factor, each capped at
    ``min(threshold, smallest cohort weight after adjustment)`` so that no
    outsider can overtake a cohort member -- this is the "downward
    adjustment" of names below the threshold that the methodology mentions.
    For the security stage the methodology states that cap explicitly
    (lesser of 4.4% and the fifth-largest security); for the company stage
    the same construction with 4.5% is used by analogy -- the document does
    not spell out the number, so this detail is an interpretation.

    The two levels are applied strictly in sequence: the security-level pass
    starts from the company-level result and the company level is *not*
    revisited afterwards.  The Weight Calculations document states that
    "weight adjustments are generally not made in a back-and-forth iterative
    fashion between levels, unless otherwise noted in a given Index's
    methodology", and the Nasdaq-100 methodology notes nothing of the sort.
    Because the 14% security cap redistributes its excess pro rata to every
    other security, it can push the company cohort above 4.5% back to 48% or
    more (or, in theory, a company above 24%); when that happens the result
    keeps the methodology's weights and records the re-breach in ``notes``
    so that callers are not misled into treating the target as satisfying
    every constraint.  Such a target is therefore not a fixed point of
    :meth:`apply_caps`.

    Calendar: annual reconstitution with reference date the last trading day
    of November, quarterly rebalances with reference dates the last trading
    day of February, May, August and November; announcement after the close
    on the sixth trading day prior to the effective date; effective at market
    open on the first trading day following the third Friday of March, June,
    September and December (trades happen at the third-Friday close).  A
    special rebalance may be triggered if, on end-of-day values, any
    company exceeds 24% or the companies above 4.5% together exceed 48%.
    """

    index_id = "QQQ"
    COMPANY_TRIGGER = 0.24
    COMPANY_CAP = 0.20
    COHORT_THRESHOLD = 0.045
    COHORT_TRIGGER = 0.48
    COHORT_TARGET = 0.40
    SECURITY_TRIGGER = 0.15
    SECURITY_CAP = 0.14
    TOP5_TRIGGER = 0.40
    TOP5_TARGET = 0.385
    OUTSIDE_TOP5_CAP = 0.044

    # -- company level ----------------------------------------------------

    def _company_level(
        self, comp: dict[str, float], binding: list[str], notes: list[str]
    ) -> tuple[dict[str, float], bool]:
        """Apply the two-stage company constraints until both hold."""
        feasible = True
        for _ in range(_MAX_ITER):
            changed = False
            # Stage 1 -- 24% trigger, 20% cap on every company.
            if comp and max(comp.values()) > self.COMPANY_TRIGGER + _EPS:
                caps = {c: self.COMPANY_CAP for c in comp}
                filled, capped, shortfall = _fill_to_target(comp, caps, 1.0)
                comp = filled
                changed = True
                label = f"QQQ company 20% cap (24% trigger): {', '.join(sorted(capped))}"
                if label not in binding:
                    binding.append(label)
                if shortfall > _EPS:
                    feasible = False
                    notes.append(
                        f"company 20% cap infeasible: {_fmt_pct(shortfall)} could not be placed"
                    )
                    return comp, feasible
            # Stage 2 -- >4.5% cohort summing to >= 48% is taken down to 40%.
            cohort = {c: w for c, w in comp.items() if w > self.COHORT_THRESHOLD + _EPS}
            cohort_sum = sum(cohort.values())
            if cohort and cohort_sum >= self.COHORT_TRIGGER - _EPS:
                factor = self.COHORT_TARGET / cohort_sum
                new = {c: w * factor for c, w in cohort.items()}
                outsider_cap = min(self.COHORT_THRESHOLD, min(new.values()))
                rest = {c: w for c, w in comp.items() if c not in cohort}
                caps = {c: outsider_cap for c in rest}
                filled, _, shortfall = _fill_to_target(rest, caps, 1.0 - self.COHORT_TARGET)
                new.update(filled)
                comp = new
                changed = True
                label = (
                    f"QQQ aggregate >4.5% cohort cut to 40% (48% trigger): "
                    f"{', '.join(sorted(cohort))}"
                )
                if label not in binding:
                    binding.append(label)
                if shortfall > _EPS:
                    feasible = False
                    notes.append(
                        f"cohort redistribution infeasible: {_fmt_pct(shortfall)} could not be placed"
                    )
                    return comp, feasible
            if not changed:
                return comp, feasible
        notes.append("company-level capping did not converge within the iteration limit")
        return comp, False

    # -- security level (December) ----------------------------------------

    def _security_level(
        self, sec: dict[str, float], binding: list[str], notes: list[str]
    ) -> tuple[dict[str, float], bool]:
        """Apply the two-stage security constraints until both hold."""
        feasible = True
        for _ in range(_MAX_ITER):
            changed = False
            # Stage 1 -- 15% trigger, 14% cap on every security.
            if sec and max(sec.values()) > self.SECURITY_TRIGGER + _EPS:
                caps = {t: self.SECURITY_CAP for t in sec}
                filled, capped, shortfall = _fill_to_target(sec, caps, 1.0)
                sec = filled
                changed = True
                label = f"QQQ security 14% cap (15% trigger): {', '.join(sorted(capped))}"
                if label not in binding:
                    binding.append(label)
                if shortfall > _EPS:
                    feasible = False
                    notes.append(
                        f"security 14% cap infeasible: {_fmt_pct(shortfall)} could not be placed"
                    )
                    return sec, feasible
            # Stage 2 -- five largest summing to >= 40% taken down to 38.5%.
            ranked = _rank_desc(sec)
            top5 = ranked[:5]
            top5_sum = sum(sec[t] for t in top5)
            if len(ranked) > 5 and top5_sum >= self.TOP5_TRIGGER - _EPS:
                factor = self.TOP5_TARGET / top5_sum
                new = {t: sec[t] * factor for t in top5}
                outsider_cap = min(self.OUTSIDE_TOP5_CAP, min(new.values()))
                rest = {t: sec[t] for t in ranked[5:]}
                caps = {t: outsider_cap for t in rest}
                filled, _, shortfall = _fill_to_target(rest, caps, 1.0 - self.TOP5_TARGET)
                new.update(filled)
                sec = new
                changed = True
                label = f"QQQ top-5 securities cut to 38.5% (40% trigger): {', '.join(sorted(top5))}"
                if label not in binding:
                    binding.append(label)
                if shortfall > _EPS:
                    feasible = False
                    notes.append(
                        f"top-5 redistribution infeasible: {_fmt_pct(shortfall)} could not be placed"
                    )
                    return sec, feasible
            if not changed:
                return sec, feasible
        notes.append("security-level capping did not converge within the iteration limit")
        return sec, False

    def apply_caps(
        self,
        constituents: Sequence[Constituent],
        as_of: date,
        event_type: str = "quarterly_rebalance",
    ) -> CapResult:
        current, total_in = _normalise(constituents)
        comp0, company_of = _company_weights(constituents, current)
        binding: list[str] = []
        notes: list[str] = []

        comp1, feasible = self._company_level(dict(comp0), binding, notes)
        # Push company weights back to securities pro rata to their initial split.
        sec = {
            t: (current[t] * comp1[company_of[t]] / comp0[company_of[t]] if comp0[company_of[t]] > 0 else 0.0)
            for t in current
        }
        if event_type == "annual_reconstitution":
            sec, ok = self._security_level(sec, binding, notes)
            feasible = feasible and ok
            # The company level is not revisited (Nasdaq Index Weight
            # Calculations: no back-and-forth between levels); report when the
            # security pass left a company-level constraint breached.
            comp_after, _ = _company_weights(constituents, sec)
            for reason in self._company_breach_reasons(comp_after):
                notes.append(
                    "company-level constraint breached after the security-level pass "
                    f"({reason}); the methodology does not revisit the company level"
                )
        return self._result(
            constituents, as_of, event_type, current, sec, total_in, binding, feasible, notes
        )

    def _company_breach_reasons(self, comp: Mapping[str, float]) -> list[str]:
        """Human-readable company-level breaches (24% single, 48% cohort) of ``comp``."""
        reasons: list[str] = []
        if comp:
            ranked = _rank_desc(comp)
            if comp[ranked[0]] > self.COMPANY_TRIGGER + _EPS:
                reasons.append(f"company {ranked[0]} at {_fmt_pct(comp[ranked[0]])} exceeds 24%")
        cohort_sum = sum(w for w in comp.values() if w > self.COHORT_THRESHOLD + _EPS)
        if cohort_sum >= self.COHORT_TRIGGER - _EPS:
            reasons.append(f"companies above 4.5% sum to {_fmt_pct(cohort_sum)}, 48% or more")
        return reasons

    def special_rebalance_triggered(self, constituents: Sequence[Constituent]) -> TriggerCheck:
        """Test the special-rebalance triggers on end-of-day company weights.

        Triggers (methodology, "Special Rebalance Schedule"): a company above
        24%, or the companies above 4.5% together above 48%.
        """
        current, _ = _normalise(constituents)
        comp, _ = _company_weights(constituents, current)
        ranked = _rank_desc(comp)
        max_c = ranked[0] if ranked else ""
        max_w = comp[max_c] if ranked else 0.0
        cohort = [c for c in ranked if comp[c] > self.COHORT_THRESHOLD + _EPS]
        cohort_sum = sum(comp[c] for c in cohort)
        reasons: list[str] = []
        if max_w > self.COMPANY_TRIGGER + _EPS:
            reasons.append(f"company {max_c} weight {_fmt_pct(max_w)} exceeds 24%")
        if cohort_sum > self.COHORT_TRIGGER + _EPS:
            reasons.append(
                f"companies above 4.5% ({', '.join(cohort)}) sum to {_fmt_pct(cohort_sum)}, "
                "exceeding 48%"
            )
        return TriggerCheck(
            bool(reasons),
            tuple(reasons),
            {"max_company_weight": max_w, "sum_over_4_5": cohort_sum},
        )

    def check_constraints(
        self,
        constituents: Sequence[Constituent],
        event_type: str = "quarterly_rebalance",
    ) -> list[Breach]:
        current, _ = _normalise(constituents)
        comp, _ = _company_weights(constituents, current)
        ranked_c = _rank_desc(comp)
        breaches: list[Breach] = []
        over24 = tuple(c for c in ranked_c if comp[c] > self.COMPANY_TRIGGER + _EPS)
        if over24:
            breaches.append(
                Breach(
                    "qqq_company_24",
                    "company weight above the 24% trigger (would be capped at 20%)",
                    over24,
                    comp[over24[0]],
                    self.COMPANY_TRIGGER,
                )
            )
        cohort = tuple(c for c in ranked_c if comp[c] > self.COHORT_THRESHOLD + _EPS)
        cohort_sum = sum(comp[c] for c in cohort)
        if cohort and cohort_sum >= self.COHORT_TRIGGER - _EPS:
            breaches.append(
                Breach(
                    "qqq_cohort_48",
                    "companies above 4.5% sum to 48% or more (cohort would be cut to 40%)",
                    cohort,
                    cohort_sum,
                    self.COHORT_TRIGGER,
                )
            )
        if event_type == "annual_reconstitution":
            ranked_s = _rank_desc(current)
            over15 = tuple(t for t in ranked_s if current[t] > self.SECURITY_TRIGGER + _EPS)
            if over15:
                breaches.append(
                    Breach(
                        "qqq_security_15",
                        "security weight above the 15% trigger (would be capped at 14%)",
                        over15,
                        current[over15[0]],
                        self.SECURITY_TRIGGER,
                    )
                )
            top5 = tuple(ranked_s[:5])
            top5_sum = sum(current[t] for t in top5)
            if len(ranked_s) > 5 and top5_sum >= self.TOP5_TRIGGER - _EPS:
                breaches.append(
                    Breach(
                        "qqq_top5_40",
                        "five largest securities sum to 40% or more (would be cut to 38.5%)",
                        top5,
                        top5_sum,
                        self.TOP5_TRIGGER,
                    )
                )
        return breaches

    def _metrics_from_weights(
        self, constituents: Sequence[Constituent], weights: Mapping[str, float]
    ) -> dict[str, float]:
        comp, _ = _company_weights(constituents, weights)
        ranked_s = _rank_desc(weights)
        return {
            "max_company_weight": max(comp.values()) if comp else 0.0,
            "sum_over_4_5": sum(w for w in comp.values() if w > self.COHORT_THRESHOLD + _EPS),
            "names_over_4_5": float(sum(1 for w in comp.values() if w > self.COHORT_THRESHOLD + _EPS)),
            "top5_security_sum": sum(weights[t] for t in ranked_s[:5]),
            "max_security_weight": weights[ranked_s[0]] if ranked_s else 0.0,
            "n_companies": float(len(comp)),
        }

    def schedule(self, year: int) -> list[RebalanceEvent]:
        cal = _cal()
        events: list[RebalanceEvent] = []
        for month in self.QUARTER_MONTHS:
            trade = _trade_session_for_third_friday(year, month)
            effective = cal.next_trading_day(trade)
            reference = cal.last_trading_day_of_month(year, month - 1)
            announcement = _nth_trading_day_before(effective, 6)
            kind = "annual_reconstitution" if month == 12 else "quarterly_rebalance"
            note = (
                "Annual reconstitution and rebalance: company-level (24%/20%, 4.5%/48%/40%) "
                "then security-level (15%/14%, top-5 40%/38.5%) constraints"
                if month == 12
                else "Quarterly rebalance: company-level constraints (24%/20%, 4.5%/48%/40%)"
            )
            events.append(
                RebalanceEvent(
                    self.index_id,
                    kind,
                    reference,
                    announcement,
                    trade,
                    effective,
                    notes=(
                        f"{note}; announced after the close on the sixth trading day prior to "
                        "the effective date; effective at market open on the first trading day "
                        "following the third Friday (trades at the third-Friday close)."
                    ),
                )
            )
        return events


# ---------------------------------------------------------------------------
# IGV -- S&P North American Expanded Technology Software Index
# ---------------------------------------------------------------------------


class IGVRules(IndexRules):
    """S&P North American Expanded Technology Software Index rules.

    Source: BofA Finance 424B2 pricing supplement dated 2026-05-11
    (https://www.sec.gov/Archives/edgar/data/0001682472/000191870426012955/form424b2.htm).
    The S&P DJI methodology PDF was not reachable -- **to re-verify against
    the S&P DJI methodology**.

    Float-adjusted market-cap weights at each quarterly rebalance with

    (a) no single company above 8.5%; excess redistributed proportionally to
        all uncapped constituents, repeated until none breaches;
    (b) companies above 4.5% may not together exceed 45%; if breached, rank
        by weight descending and reduce the lowest-weighted company that
        causes the breach (the one at which the cumulative weight first
        crosses 45%) until the rule is met or its weight reaches 4.5%;
        redistribute the excess proportionally to companies below 4.5% such
        that no recipient breaches 4.5%; repeat until the aggregate rule holds
        or all stocks are at or above 4.5% (then the rule is left unsatisfied
        and the result is flagged ``feasible=False``).

    Calendar: capping applied quarterly after the close on the third Friday
    of March, June, September and December with pricing reference date the
    Thursday before the second Friday of the rebalance month; membership
    reconstitution semi-annual, effective with the June and December
    rebalances, reference date the last trading day of the prior month.
    S&P does not publish a deterministic announcement rule, so
    ``announcement_date`` is ``None``.  Index shares are fixed between
    rebalances so actual weights drift.
    """

    index_id = "IGV"
    SINGLE_CAP = 0.085
    COHORT_THRESHOLD = 0.045
    COHORT_CAP = 0.45

    def apply_caps(
        self,
        constituents: Sequence[Constituent],
        as_of: date,
        event_type: str = "quarterly_rebalance",
    ) -> CapResult:
        current, total_in = _normalise(constituents)
        # Company-level aggregation is a no-op unless share classes are present.
        comp0, company_of = _company_weights(constituents, current)
        binding: list[str] = []
        notes: list[str] = []
        feasible = True

        # (a) single-company cap with proportional redistribution.
        caps = {c: self.SINGLE_CAP for c in comp0}
        comp, capped, shortfall = _fill_to_target(comp0, caps, 1.0)
        if capped:
            binding.append(f"IGV 8.5% single-company cap: {', '.join(sorted(capped))}")
        if shortfall > _EPS:
            feasible = False
            notes.append(f"8.5% cap infeasible: {_fmt_pct(shortfall)} could not be placed")

        # (b) aggregate 4.5% / 45% rule.
        reduced: list[str] = []
        for _ in range(_MAX_ITER):
            ranked = _rank_desc(comp)
            cohort = [c for c in ranked if comp[c] > self.COHORT_THRESHOLD + _EPS]
            cohort_sum = sum(comp[c] for c in cohort)
            if cohort_sum <= self.COHORT_CAP + _EPS:
                break
            cumulative = 0.0
            culprit = cohort[-1]
            for c in cohort:
                cumulative += comp[c]
                if cumulative > self.COHORT_CAP + _EPS:
                    culprit = c
                    break
            needed = cumulative - self.COHORT_CAP
            reduction = min(needed, comp[culprit] - self.COHORT_THRESHOLD)
            recipients = {c: w for c, w in comp.items() if w < self.COHORT_THRESHOLD - _EPS}
            capacity = sum(self.COHORT_THRESHOLD - w for w in recipients.values())
            absorbed = min(reduction, capacity)
            if absorbed <= _EPS:
                feasible = False
                notes.append(
                    "IGV aggregate 45% rule left unsatisfied: no company below 4.5% can "
                    f"absorb weight (cohort sum {_fmt_pct(cohort_sum)})"
                )
                break
            comp[culprit] -= absorbed
            rcaps = {c: self.COHORT_THRESHOLD for c in recipients}
            filled, _, _ = _fill_to_target(recipients, rcaps, sum(recipients.values()) + absorbed)
            comp.update(filled)
            if culprit not in reduced:
                reduced.append(culprit)
            if absorbed < reduction - _EPS:
                feasible = False
                notes.append(
                    "IGV aggregate 45% rule left unsatisfied: recipients below 4.5% exhausted"
                )
                break
        else:
            feasible = False
            notes.append("IGV aggregate rule did not converge within the iteration limit")
        if reduced:
            binding.append(f"IGV aggregate 45% rule (companies >4.5%): reduced {', '.join(reduced)}")

        target = {
            t: (current[t] * comp[company_of[t]] / comp0[company_of[t]] if comp0[company_of[t]] > 0 else 0.0)
            for t in current
        }
        return self._result(
            constituents, as_of, event_type, current, target, total_in, binding, feasible, notes
        )

    def check_constraints(
        self,
        constituents: Sequence[Constituent],
        event_type: str = "quarterly_rebalance",
    ) -> list[Breach]:
        current, _ = _normalise(constituents)
        comp, _ = _company_weights(constituents, current)
        ranked = _rank_desc(comp)
        breaches: list[Breach] = []
        over = tuple(c for c in ranked if comp[c] > self.SINGLE_CAP + _EPS)
        if over:
            breaches.append(
                Breach(
                    "igv_single_cap_8_5",
                    "company above the 8.5% single-company cap",
                    over,
                    comp[over[0]],
                    self.SINGLE_CAP,
                )
            )
        cohort = tuple(c for c in ranked if comp[c] > self.COHORT_THRESHOLD + _EPS)
        cohort_sum = sum(comp[c] for c in cohort)
        if cohort_sum > self.COHORT_CAP + _EPS:
            breaches.append(
                Breach(
                    "igv_cohort_45",
                    "companies above 4.5% together exceed 45%",
                    cohort,
                    cohort_sum,
                    self.COHORT_CAP,
                )
            )
        return breaches

    def _metrics_from_weights(
        self, constituents: Sequence[Constituent], weights: Mapping[str, float]
    ) -> dict[str, float]:
        comp, _ = _company_weights(constituents, weights)
        return {
            "max_weight": max(comp.values()) if comp else 0.0,
            "sum_over_4_5": sum(w for w in comp.values() if w > self.COHORT_THRESHOLD + _EPS),
            "names_over_4_5": float(sum(1 for w in comp.values() if w > self.COHORT_THRESHOLD + _EPS)),
            "names_over_8_5": float(sum(1 for w in comp.values() if w > self.SINGLE_CAP + _EPS)),
        }

    def schedule(self, year: int) -> list[RebalanceEvent]:
        cal = _cal()
        events: list[RebalanceEvent] = []
        for month in self.QUARTER_MONTHS:
            trade = _trade_session_for_third_friday(year, month)
            effective = cal.next_trading_day(trade)
            pricing_ref = cal.prev_trading_day(
                cal.thursday_before_second_friday(year, month), include=True
            )
            events.append(
                RebalanceEvent(
                    self.index_id,
                    "quarterly_rebalance",
                    pricing_ref,
                    None,
                    trade,
                    effective,
                    notes=(
                        "Quarterly re-capping (8.5% single, 4.5%/45% aggregate); index shares "
                        "computed from closing prices on the Thursday before the second Friday; "
                        "applied after the close on the third Friday. Announcement rule not "
                        "published."
                    ),
                )
            )
            if month in (6, 12):
                membership_ref = cal.last_trading_day_of_month(year, month - 1)
                events.append(
                    RebalanceEvent(
                        self.index_id,
                        "semiannual_reconstitution",
                        membership_ref,
                        None,
                        trade,
                        effective,
                        notes=(
                            "Semi-annual membership reconstitution; eligibility as of the last "
                            "trading day of the prior month; effective with the quarterly "
                            f"rebalance (pricing reference {pricing_ref})."
                        ),
                    )
                )
        return events


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_RULES: dict[str, type[IndexRules]] = {
    "SOXX": SOXXRules,
    "QQQ": QQQRules,
    "IGV": IGVRules,
}


def rules_for(index_id: str) -> IndexRules:
    """Return the rules object for an index id or ETF ticker (case-insensitive)."""
    key = index_id.upper()
    key = INDEX_FOR_ETF.get(key, key)
    try:
        return _RULES[key]()
    except KeyError:
        raise KeyError(f"no rules for index/ETF {index_id!r}; known: {sorted(_RULES)}") from None
