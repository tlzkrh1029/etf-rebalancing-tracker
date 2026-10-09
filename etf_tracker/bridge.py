"""Adapters between the holdings data model and the index capping rules.

:mod:`etf_tracker.holdings` describes what an ETF *holds* (shares, prices,
cash lines, published weights) while :mod:`etf_tracker.rules` reasons about
*index constituents* (ticker, weight fraction, company id, ADR flag).  This
module converts the former into the latter so a daily snapshot can be run
through ``IndexRules.apply_caps`` / ``check_constraints`` without the caller
re-implementing the mapping.

Conventions
-----------
* Weights are fractions (0.0-1.0).  The constituents returned by
  :func:`constituents_from_snapshot` always sum to one over the equity lines
  (cash, derivatives and 'other' lines are excluded, as the capping rules
  require).
* Share classes of one company (``GOOGL``/``GOOG``) share a ``company_id``
  for indices whose caps are defined at the company level
  (:data:`COMPANY_LEVEL_INDICES`: Nasdaq-100 and the S&P software index).
  For the NYSE Semiconductor Index caps apply per security, so
  ``company_id`` is the ticker.  An explicit ``Holding.company_id`` always
  wins.
* ``is_adr`` is copied when the holding knows it; an unknown flag
  (``None``) becomes ``False``.  ``adr_tickers`` lets the caller supply the
  ADR list when the data source does not (iShares files do not flag ADRs).

This module imports :mod:`etf_tracker.holdings` and :mod:`etf_tracker.rules`
and nothing else from the package.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from etf_tracker.holdings import Snapshot
from etf_tracker.rules import SHARE_CLASS_GROUPS, CapResult, Constituent, IndexRules, rules_for

__all__ = [
    "COMPANY_LEVEL_INDICES",
    "resolve_index_id",
    "constituents_from_snapshot",
    "apply_caps_to_snapshot",
]

#: Indices whose capping rules aggregate the share classes of one company.
#: For these, tickers listed in :data:`etf_tracker.rules.SHARE_CLASS_GROUPS`
#: receive the group's company id; other indices keep ``company_id == ticker``.
COMPANY_LEVEL_INDICES: frozenset[str] = frozenset({"QQQ", "IGV"})


def resolve_index_id(index_id: str) -> str:
    """Return the canonical index id for an index id or ETF ticker.

    Case-insensitive; raises ``KeyError`` for an unknown identifier, exactly
    like :func:`etf_tracker.rules.rules_for`.
    """
    return rules_for(index_id).index_id


def _rules_for_snapshot(snapshot: Snapshot, index_id: str | None) -> IndexRules:
    return rules_for(snapshot.etf if index_id is None else index_id)


def constituents_from_snapshot(
    snapshot: Snapshot,
    index_id: str | None = None,
    *,
    adr_tickers: Iterable[str] | None = None,
) -> list[Constituent]:
    """Build the :class:`~etf_tracker.rules.Constituent` list of a snapshot.

    Args:
        snapshot: the ETF holdings on one date.
        index_id: index id or ETF ticker (``'QQQ'``, ``'soxx'``...).  Defaults
            to ``snapshot.etf``.  ``KeyError`` when unknown.
        adr_tickers: tickers to mark as ADRs in addition to holdings whose
            ``is_adr`` is ``True`` (matched case-insensitively).

    Returns:
        Constituents in file order of first appearance, one per ticker.
        Weights are equity-only fractions that sum to one.  Duplicate ticker
        lines are aggregated (name and flags from the first line).  Lines
        with a non-positive weight (zero or short) are dropped and the rest
        re-normalised.  Non-equity lines are excluded.  An empty list is
        returned when no equity line carries a usable weight.
    """
    index = _rules_for_snapshot(snapshot, index_id).index_id
    group_share_classes = index in COMPANY_LEVEL_INDICES
    adr_override = {t.strip().upper() for t in (adr_tickers or ()) if t and t.strip()}

    weights = snapshot.normalized_weights(include_cash=False)
    positive = {ticker: w for ticker, w in weights.items() if w > 0.0}
    total = sum(positive.values())
    if total <= 0.0:
        return []

    out: list[Constituent] = []
    seen: set[str] = set()
    for holding in snapshot.equities():
        ticker = holding.ticker
        if ticker in seen or ticker not in positive:
            continue
        seen.add(ticker)

        company_id = holding.company_id
        if company_id is None:
            upper = ticker.upper()
            company_id = SHARE_CLASS_GROUPS.get(upper, ticker) if group_share_classes else ticker

        is_adr = bool(holding.is_adr) if holding.is_adr is not None else False
        if ticker.strip().upper() in adr_override:
            is_adr = True

        out.append(
            Constituent(
                ticker=ticker,
                weight=positive[ticker] / total,
                company_id=company_id,
                is_adr=is_adr,
                name=holding.name or None,
            )
        )
    return out


def apply_caps_to_snapshot(
    snapshot: Snapshot,
    index_id: str | None = None,
    event_type: str | None = None,
    *,
    as_of: date | None = None,
    adr_tickers: Iterable[str] | None = None,
) -> CapResult:
    """Run a snapshot through its index's capping rules.

    ``event_type`` defaults to the kind of the next scheduled event after
    ``as_of`` (``IndexRules.event_type_for``), so a December Nasdaq-100 run
    applies the annual-reconstitution security caps automatically.  ``as_of``
    defaults to ``snapshot.as_of``.  Raises ``ValueError`` when the snapshot
    has no equity line with a positive weight.
    """
    rules = _rules_for_snapshot(snapshot, index_id)
    constituents = constituents_from_snapshot(snapshot, rules.index_id, adr_tickers=adr_tickers)
    if not constituents:
        raise ValueError(
            f"{snapshot.etf} {snapshot.as_of.isoformat()}: no equity line with a positive weight"
        )
    effective_as_of = snapshot.as_of if as_of is None else as_of
    kind = rules.event_type_for(effective_as_of) if event_type is None else event_type
    return rules.apply_caps(constituents, effective_as_of, event_type=kind)
