"""Realistic analysis dictionaries for report tests, built with the real core.

``etf_tracker.analysis`` is being written concurrently, so the report tests
cannot import it.  Instead this module constructs two synthetic snapshots per
ETF with :mod:`etf_tracker.holdings`, runs the *real* decomposition
(:func:`etf_tracker.decompose.decompose`), capping rules
(:func:`etf_tracker.bridge.apply_caps_to_snapshot`, ``check_constraints``,
``special_rebalance_triggered``) and calendar (``next_events``), and
assembles the result exactly as the analysis JSON contract specifies.

Public helpers (all deterministic):

* :func:`make_snapshots` -- ``(prev, curr)`` snapshots of one ETF.
* :func:`build_analysis` -- the per-ETF dictionary (``analyze_etf`` shape).
* :func:`build_analysis_all` -- the top-level dictionary (``analyze_all``
  shape) with ``generated_utc``, ``today``, ``etfs`` and ``errors``.

The integration agent can reuse :func:`make_snapshots` to write snapshots to
disk and run the real ``analyze_etf`` against them.

Designed features of the synthetic data (so that every report branch has
something to show):

* SOXX: AMD 9.50% and INTC 8.62% breach the 8% cap, MU and QCOM sit above
  4% outside the top five, ADRs sum to 9.1% (below the 10% cap), AMD and
  TXN are active trades, k = 1.0062 (creations).
* QQQ: no cap breach, no special-rebalance trigger, GOOGL/GOOG share
  classes, an ADR (ASML), a 10:1 split on NFLX (corporate action suspect),
  PLTR active trade, a cash line and an index future, k = 0.9985
  (redemptions).  Because the next event is the December annual
  reconstitution the security-level rows apply.
* IGV: MSFT 8.90% breaches the 8.5% cap and the >4.5% cohort sums above
  45%; GTLB enters, DOCU exits, CRWD is an active trade, k = 1.0.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from etf_tracker.bridge import apply_caps_to_snapshot, constituents_from_snapshot
from etf_tracker.decompose import decompose
from etf_tracker.holdings import CASH, DERIVATIVE, EQUITY, Holding, Snapshot
from etf_tracker.market_calendar import trading_days_between
from etf_tracker.rules import INDEX_FOR_ETF, QQQRules, rules_for

__all__ = [
    "TODAY",
    "AS_OF",
    "PREV_AS_OF",
    "GENERATED_UTC",
    "ETFS",
    "ANALYSIS_KEYS",
    "make_snapshots",
    "build_analysis",
    "build_analysis_all",
]

TODAY = date(2026, 10, 9)
AS_OF = date(2026, 10, 8)
PREV_AS_OF = date(2026, 10, 7)
GENERATED_UTC = "2026-10-09T14:05:00Z"
ETFS: tuple[str, ...] = ("SOXX", "QQQ", "IGV")

#: Exactly the top-level keys of one ETF's analysis dictionary.
ANALYSIS_KEYS: frozenset[str] = frozenset(
    {
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
    }
)

# (ticker, name, weight in percent of the fund, price, is_adr)
_Row = tuple[str, str, float, float, bool]

_SPECS: dict[str, dict[str, Any]] = {
    "SOXX": {
        "source": "ishares-csv",
        "tna": 15_200_000_000.0,
        "shares_outstanding": 82_700_000.0,
        "prev_shares_outstanding": 82_190_000.0,
        "k": 1.0062,
        "seed": 3,
        "equities": [
            ("AMD", "ADVANCED MICRO DEVICES INC", 9.50, 236.40, False),
            ("INTC", "INTEL CORP", 8.62, 37.15, False),
            ("NVDA", "NVIDIA CORP", 7.80, 188.90, False),
            ("AVGO", "BROADCOM INC", 7.40, 352.60, False),
            ("TSM", "TAIWAN SEMICONDUCTOR MANUFACTURING ADR", 5.90, 302.10, True),
            ("MU", "MICRON TECHNOLOGY INC", 4.60, 191.25, False),
            ("QCOM", "QUALCOMM INC", 4.30, 168.40, False),
            ("TXN", "TEXAS INSTRUMENT INC", 3.95, 184.70, False),
            ("AMAT", "APPLIED MATERIAL INC", 3.70, 221.30, False),
            ("LRCX", "LAM RESEARCH CORP", 3.60, 141.80, False),
            ("KLAC", "KLA CORP", 3.50, 1_102.50, False),
            ("ADI", "ANALOG DEVICES INC", 3.40, 246.10, False),
            ("NXPI", "NXP SEMICONDUCTORS NV", 3.10, 224.90, False),
            ("MRVL", "MARVELL TECHNOLOGY INC", 3.00, 88.60, False),
            ("MCHP", "MICROCHIP TECHNOLOGY INC", 2.90, 67.80, False),
            ("ON", "ON SEMICONDUCTOR CORP", 2.70, 52.40, False),
            ("MPWR", "MONOLITHIC POWER SYSTEMS INC", 2.40, 942.30, False),
            ("SWKS", "SKYWORKS SOLUTIONS INC", 2.30, 76.20, False),
            ("ARM", "ARM HOLDINGS AMERICAN DEPOSITARY S", 2.20, 162.70, True),
            ("TER", "TERADYNE INC", 2.20, 139.50, False),
            ("ENTG", "ENTEGRIS INC", 1.90, 92.10, False),
            ("QRVO", "QORVO INC", 1.80, 88.30, False),
            ("COHR", "COHERENT CORP", 1.80, 109.40, False),
            ("LSCC", "LATTICE SEMICONDUCTOR CORP", 1.70, 71.20, False),
            ("ONTO", "ONTO INNOVATION INC", 1.40, 141.60, False),
            ("STM", "STMICROELECTRONICS ADR", 1.00, 29.85, True),
            ("ALAB", "ASTERA LABS INC", 0.90, 173.20, False),
            ("CRUS", "CIRRUS LOGIC INC", 0.80, 124.70, False),
            ("AMKR", "AMKOR TECHNOLOGY INC", 0.70, 28.90, False),
            ("FORM", "FORMFACTOR INC", 0.50, 41.30, False),
        ],
        "cash": ("USD", "USD CASH", 0.43),
        "future": None,
        # relative share-count change beyond the flow factor k
        "trades": {"AMD": 0.020, "TXN": -0.015},
        "splits": {},
        "entries": [],
        "exits": [],
    },
    "QQQ": {
        "source": "invesco-api",
        "tna": 372_500_000_000.0,
        "shares_outstanding": 585_200_000.0,
        "prev_shares_outstanding": 586_080_000.0,
        "k": 0.9985,
        "seed": 5,
        "equities": [
            ("NVDA", "NVIDIA Corp", 9.40, 188.90, False),
            ("AAPL", "Apple Inc", 8.60, 256.40, False),
            ("MSFT", "Microsoft Corp", 8.10, 524.80, False),
            ("AMZN", "Amazon.com Inc", 5.60, 221.70, False),
            ("AVGO", "Broadcom Inc", 5.20, 352.60, False),
            ("META", "Meta Platforms Inc", 4.40, 718.30, False),
            ("TSLA", "Tesla Inc", 3.60, 434.10, False),
            ("NFLX", "Netflix Inc", 3.20, 118.40, False),
            ("COST", "Costco Wholesale Corp", 3.00, 921.60, False),
            ("GOOGL", "Alphabet Inc Class A", 2.90, 245.30, False),
            ("GOOG", "Alphabet Inc Class C", 2.80, 246.10, False),
            ("PLTR", "Palantir Technologies Inc", 2.80, 182.90, False),
            ("TMUS", "T-Mobile US Inc", 2.60, 232.50, False),
            ("CSCO", "Cisco Systems Inc", 2.50, 68.70, False),
            ("ASML", "ASML Holding NV ADR", 2.40, 984.20, True),
            ("LIN", "Linde PLC", 2.30, 462.80, False),
            ("AMD", "Advanced Micro Devices Inc", 2.30, 236.40, False),
            ("ISRG", "Intuitive Surgical Inc", 2.20, 448.90, False),
            ("PEP", "PepsiCo Inc", 2.10, 142.60, False),
            ("INTU", "Intuit Inc", 2.00, 684.30, False),
            ("ADBE", "Adobe Inc", 2.00, 348.70, False),
            ("QCOM", "Qualcomm Inc", 1.90, 168.40, False),
            ("BKNG", "Booking Holdings Inc", 1.90, 5_412.00, False),
            ("TXN", "Texas Instruments Inc", 1.80, 184.70, False),
            ("AMGN", "Amgen Inc", 1.80, 292.30, False),
            ("AMAT", "Applied Materials Inc", 1.70, 221.30, False),
            ("CMCSA", "Comcast Corp", 1.70, 31.20, False),
            ("HON", "Honeywell International Inc", 1.60, 214.80, False),
            ("MU", "Micron Technology Inc", 1.60, 191.25, False),
            ("PANW", "Palo Alto Networks Inc", 1.50, 212.40, False),
            ("SHOP", "Shopify Inc", 1.40, 158.60, False),
            ("ARM", "Arm Holdings PLC ADR", 1.30, 162.70, True),
            ("LRCX", "Lam Research Corp", 1.30, 141.80, False),
        ],
        "cash": ("_CASH_COLLATERAL_USD", "Cash Collateral USD", 0.45),
        "future": ("NQZ6", "NASDAQ 100 E-MINI FUTURE DEC26", 0.05),
        "trades": {"PLTR": 0.012},
        "splits": {"NFLX": 10.0},
        "entries": [],
        "exits": [],
    },
    "IGV": {
        "source": "ishares-csv",
        "tna": 11_800_000_000.0,
        "shares_outstanding": 112_400_000.0,
        "prev_shares_outstanding": 112_400_000.0,
        "k": 1.0,
        "seed": 7,
        "equities": [
            ("MSFT", "MICROSOFT CORP", 8.90, 524.80, False),
            ("ORCL", "ORACLE CORP", 8.40, 291.40, False),
            ("CRM", "SALESFORCE INC", 7.10, 242.60, False),
            ("NOW", "SERVICENOW INC", 6.10, 918.20, False),
            ("ADBE", "ADOBE INC", 5.60, 348.70, False),
            ("INTU", "INTUIT INC", 5.10, 684.30, False),
            ("PLTR", "PALANTIR TECHNOLOGIES INC CLASS A", 4.80, 182.90, False),
            ("PANW", "PALO ALTO NETWORKS INC", 4.40, 212.40, False),
            ("CRWD", "CROWDSTRIKE HOLDINGS INC CLASS A", 4.20, 498.70, False),
            ("SNPS", "SYNOPSYS INC", 3.80, 462.10, False),
            ("CDNS", "CADENCE DESIGN SYSTEMS INC", 3.60, 358.90, False),
            ("ADSK", "AUTODESK INC", 3.30, 312.40, False),
            ("WDAY", "WORKDAY INC CLASS A", 3.00, 236.80, False),
            ("FTNT", "FORTINET INC", 2.80, 82.10, False),
            ("ROP", "ROPER TECHNOLOGIES INC", 2.60, 528.60, False),
            ("TTD", "TRADE DESK INC CLASS A", 2.20, 52.30, False),
            ("DDOG", "DATADOG INC CLASS A", 2.00, 148.20, False),
            ("EA", "ELECTRONIC ARTS INC", 1.90, 201.10, False),
            ("TTWO", "TAKE TWO INTERACTIVE SOFTWARE INC", 1.80, 251.80, False),
            ("ANSS", "ANSYS INC", 1.70, 376.40, False),
            ("ZS", "ZSCALER INC", 1.60, 318.90, False),
            ("HUBS", "HUBSPOT INC", 1.50, 486.20, False),
            ("SHOP", "SHOPIFY INC CLASS A", 1.40, 158.60, False),
            ("OTEX", "OPEN TEXT CORP", 1.20, 34.80, False),
            ("PTC", "PTC INC", 1.10, 214.30, False),
            ("GEN", "GEN DIGITAL INC", 1.00, 31.60, False),
            ("MANH", "MANHATTAN ASSOCIATES INC", 0.90, 208.70, False),
            ("MDB", "MONGODB INC CLASS A", 0.90, 342.10, False),
            ("TYL", "TYLER TECHNOLOGIES INC", 0.90, 548.30, False),
            ("BSY", "BENTLEY SYSTEMS INC CLASS B", 0.80, 52.40, False),
            ("PCTY", "PAYLOCITY HOLDING CORP", 0.80, 168.90, False),
            ("DT", "DYNATRACE INC", 0.80, 48.70, False),
            ("APPF", "APPFOLIO INC CLASS A", 0.70, 238.40, False),
            ("NTNX", "NUTANIX INC CLASS A", 0.70, 72.10, False),
            ("CFLT", "CONFLUENT INC CLASS A", 0.60, 21.40, False),
            ("GTLB", "GITLAB INC CLASS A", 0.60, 48.30, False),
            ("DBX", "DROPBOX INC CLASS A", 0.50, 29.80, False),
            ("BOX", "BOX INC CLASS A", 0.40, 33.20, False),
        ],
        "cash": ("USD", "USD CASH", 0.30),
        "future": None,
        "trades": {"CRWD": -0.030},
        "splits": {},
        # present in curr only
        "entries": ["GTLB"],
        # (ticker, name, shares, price) present in prev only
        "exits": [("DOCU", "DOCUSIGN INC", 1_310_000.0, 71.40)],
    },
}


def _return_for(index: int, seed: int) -> float:
    """Deterministic pseudo-random daily price return in [-2.5%, +2.5%]."""
    return (((index * 7 + seed) % 11) - 5) * 0.005


def _spec(etf: str) -> dict[str, Any]:
    try:
        return _SPECS[etf.upper()]
    except KeyError:
        raise KeyError(f"no synthetic data for {etf!r}; known: {sorted(_SPECS)}") from None


def make_snapshots(etf: str) -> tuple[Snapshot, Snapshot]:
    """Return deterministic ``(prev, curr)`` snapshots of ``etf``.

    Weights are fractions, market values equal ``shares * price`` and the
    fund weights (including cash and derivative lines) sum to one.  The
    previous snapshot is derived from the current one: every share count is
    divided by the flow factor ``k`` (and by any active-trade or split
    factor) and every price by ``1 + return``.
    """
    spec = _spec(etf)
    etf = etf.upper()
    tna: float = spec["tna"]
    k: float = spec["k"]
    seed: int = spec["seed"]
    trades: dict[str, float] = spec["trades"]
    splits: dict[str, float] = spec["splits"]
    entries: set[str] = set(spec["entries"])

    curr_rows: list[Holding] = []
    prev_rows: list[Holding] = []

    for index, (ticker, name, weight_pct, price, is_adr) in enumerate(spec["equities"]):
        weight = weight_pct / 100.0
        market_value = weight * tna
        shares = market_value / price
        curr_rows.append(
            Holding(
                etf=etf,
                as_of=AS_OF,
                ticker=ticker,
                name=name,
                asset_class=EQUITY,
                shares=shares,
                price=price,
                market_value=market_value,
                weight=weight,
                currency="USD",
                sector="Information Technology",
                is_adr=is_adr,
                company_id=None,
                source=spec["source"],
            )
        )
        if ticker in entries:
            continue
        split = splits.get(ticker, 1.0)
        trade = trades.get(ticker, 0.0)
        ret = _return_for(index, seed)
        shares_prev = shares / k / (1.0 + trade) / split
        price_prev = price / (1.0 + ret) * split
        prev_rows.append(
            Holding(
                etf=etf,
                as_of=PREV_AS_OF,
                ticker=ticker,
                name=name,
                asset_class=EQUITY,
                shares=shares_prev,
                price=price_prev,
                market_value=shares_prev * price_prev,
                weight=None,  # filled below once the total is known
                currency="USD",
                sector="Information Technology",
                is_adr=is_adr,
                company_id=None,
                source=spec["source"],
            )
        )

    for exit_ticker, exit_name, exit_shares, exit_price in spec["exits"]:
        prev_rows.append(
            Holding(
                etf=etf,
                as_of=PREV_AS_OF,
                ticker=exit_ticker,
                name=exit_name,
                asset_class=EQUITY,
                shares=exit_shares,
                price=exit_price,
                market_value=exit_shares * exit_price,
                weight=None,
                currency="USD",
                sector="Information Technology",
                is_adr=False,
                company_id=None,
                source=spec["source"],
            )
        )

    future = spec["future"]
    if future is not None:
        f_ticker, f_name, f_weight_pct = future
        f_mv = f_weight_pct / 100.0 * tna
        for rows, as_of, scale in ((curr_rows, AS_OF, 1.0), (prev_rows, PREV_AS_OF, 1.0 / k)):
            mv = f_mv * scale
            rows.append(
                Holding(
                    etf=etf,
                    as_of=as_of,
                    ticker=f_ticker,
                    name=f_name,
                    asset_class=DERIVATIVE,
                    shares=12.0,
                    price=mv / 12.0,
                    market_value=mv,
                    weight=f_weight_pct / 100.0 if as_of == AS_OF else None,
                    currency="USD",
                    sector=None,
                    is_adr=False,
                    company_id=None,
                    source=spec["source"],
                )
            )

    c_ticker, c_name, c_weight_pct = spec["cash"]
    c_mv = c_weight_pct / 100.0 * tna
    for rows, as_of, scale in ((curr_rows, AS_OF, 1.0), (prev_rows, PREV_AS_OF, 1.0 / k)):
        mv = c_mv * scale
        rows.append(
            Holding(
                etf=etf,
                as_of=as_of,
                ticker=c_ticker,
                name=c_name,
                asset_class=CASH,
                shares=mv,
                price=1.0,
                market_value=mv,
                weight=c_weight_pct / 100.0 if as_of == AS_OF else None,
                currency="USD",
                sector=None,
                is_adr=False,
                company_id=None,
                source=spec["source"],
            )
        )

    prev_total = sum(h.market_value or 0.0 for h in prev_rows)
    for h in prev_rows:
        h.weight = (h.market_value or 0.0) / prev_total

    so_curr: float = spec["shares_outstanding"]
    so_prev: float = spec["prev_shares_outstanding"]
    curr = Snapshot(
        etf=etf,
        as_of=AS_OF,
        source=spec["source"],
        holdings=curr_rows,
        meta={
            "shares_outstanding": so_curr,
            "total_net_assets": tna,
            "nav": tna / so_curr,
            "url": f"https://example.invalid/{etf}/{AS_OF.isoformat()}",
        },
    )
    prev = Snapshot(
        etf=etf,
        as_of=PREV_AS_OF,
        source=spec["source"],
        holdings=prev_rows,
        meta={
            "shares_outstanding": so_prev,
            "total_net_assets": prev_total,
            "nav": prev_total / so_prev,
            "url": f"https://example.invalid/{etf}/{PREV_AS_OF.isoformat()}",
        },
    )
    return prev, curr


def _plain(obj: Any) -> Any:
    """Recursively convert to JSON-native types (tuples -> lists, dates -> ISO)."""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def _meta_number(meta: dict[str, Any], key: str) -> float | None:
    value = meta.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def build_analysis(etf: str, *, with_prev: bool = True, today: date = TODAY) -> dict[str, Any]:
    """Build one ETF's analysis dictionary exactly per the JSON contract.

    ``with_prev=False`` mimics the first day of collection: ``prev_as_of``
    and ``decomposition`` are ``None``.  The result has been passed through
    ``json.loads(json.dumps(...))`` so it contains only JSON-native types
    (what a consumer of ``reports/latest.json`` sees).
    """
    etf = etf.upper()
    prev, curr = make_snapshots(etf)
    index_id = INDEX_FOR_ETF[etf]
    rules = rules_for(index_id)

    warnings: list[str] = list(curr.validate())

    decomposition: dict[str, Any] | None = None
    prev_as_of: str | None = None
    if with_prev:
        result = decompose(prev, curr)
        decomposition = result.to_dict()
        warnings.extend(result.warnings)
        prev_as_of = prev.as_of.isoformat()

    cap_result = apply_caps_to_snapshot(curr, index_id)
    constituents = constituents_from_snapshot(curr, index_id)
    breaches = rules.check_constraints(constituents, cap_result.event_type)
    sorted_deltas = cap_result.sorted_deltas()
    forced_sellers = [[t, d] for t, d in sorted_deltas if d < -1e-12]
    forced_buyers = [[t, d] for t, d in reversed(sorted_deltas) if d > 1e-12]

    special: dict[str, Any] | None = None
    if isinstance(rules, QQQRules):
        check = rules.special_rebalance_triggered(constituents)
        special = {
            "triggered": check.triggered,
            "reasons": list(check.reasons),
            "metrics": dict(check.metrics),
        }

    next_events = []
    for ev in rules.next_events(today, n=3):
        next_events.append(
            {
                "kind": ev.kind,
                "reference_date": ev.reference_date,
                "announcement_date": ev.announcement_date,
                "effective_trade_date": ev.effective_trade_date,
                "effective_date": ev.effective_date,
                "trading_days_to_reference": trading_days_between(today, ev.reference_date),
                "trading_days_to_trade": trading_days_between(today, ev.effective_trade_date),
                "notes": ev.notes,
            }
        )

    holdings = sorted(curr.holdings, key=lambda h: (-(h.weight or 0.0), h.ticker))
    analysis = {
        "etf": etf,
        "index_id": index_id,
        "as_of": curr.as_of,
        "prev_as_of": prev_as_of,
        "source": curr.source,
        "fund": {
            "shares_outstanding": _meta_number(curr.meta, "shares_outstanding"),
            "prev_shares_outstanding": _meta_number(prev.meta, "shares_outstanding") if with_prev else None,
            "nav": _meta_number(curr.meta, "nav"),
            "total_net_assets": _meta_number(curr.meta, "total_net_assets"),
        },
        "holdings": [
            {
                "ticker": h.ticker,
                "name": h.name,
                "asset_class": h.asset_class,
                "weight": h.weight,
                "shares": h.shares,
                "price": h.price,
                "market_value": h.market_value,
                "is_adr": h.is_adr,
                "company_id": h.company_id,
            }
            for h in holdings
        ],
        "decomposition": decomposition,
        "caps": {
            "event_type": cap_result.event_type,
            "feasible": cap_result.feasible,
            "binding_constraints": list(cap_result.binding_constraints),
            "notes": list(cap_result.notes),
            "metrics": dict(cap_result.metrics),
            "target_metrics": dict(cap_result.target_metrics),
            "breaches": [
                {
                    "rule": b.rule,
                    "description": b.description,
                    "tickers": list(b.tickers),
                    "value": b.value,
                    "limit": b.limit,
                }
                for b in breaches
            ],
            "forced_sellers": forced_sellers,
            "forced_buyers": forced_buyers,
            "special_rebalance": special,
        },
        "next_events": next_events,
        "warnings": warnings,
    }
    assert set(analysis) == ANALYSIS_KEYS
    return json.loads(json.dumps(_plain(analysis), allow_nan=False))


def build_analysis_all(
    *,
    today: date = TODAY,
    etfs: tuple[str, ...] = ETFS,
    with_prev: bool = True,
    errors: dict[str, str] | None = None,
    generated_utc: str = GENERATED_UTC,
) -> dict[str, Any]:
    """Build the ``analyze_all`` shaped dictionary for ``etfs``.

    ETFs named in ``errors`` are left out of ``etfs`` and reported in
    ``errors`` instead, as the analysis layer does when a fetch fails.
    """
    errors = dict(errors or {})
    return {
        "generated_utc": generated_utc,
        "today": today.isoformat(),
        "etfs": {etf: build_analysis(etf, with_prev=with_prev, today=today) for etf in etfs if etf not in errors},
        "errors": errors,
    }
