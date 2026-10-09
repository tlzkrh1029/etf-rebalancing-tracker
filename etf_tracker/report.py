"""Korean markdown and JSON reports from the daily analysis dictionary.

Standard library only.  This module consumes the dictionary produced by
``etf_tracker.analysis.analyze_all`` (or one ``analyze_etf`` entry) and
renders

* :func:`render_markdown` -- the daily Korean markdown report,
* :func:`render_summary_line` -- one line per ETF for logs and
  notifications,
* :func:`write_reports` -- ``reports/latest.json``, ``reports/latest.md``
  and dated copies under ``reports/daily/``.

Input contract (authoritative version in the project brief)
-----------------------------------------------------------
``analyze_all`` returns ``{"generated_utc", "today", "etfs": {ETF: entry},
"errors": {ETF: message}}``.  Each entry has the keys ``etf``, ``index_id``,
``as_of``, ``prev_as_of``, ``source``, ``fund``, ``holdings``,
``decomposition``, ``caps``, ``next_events`` and ``warnings``; weights are
fractions, dates are ISO strings and numbers are plain floats or ``None``.

The renderer treats the dictionary as untrusted data: every value is read
through a tolerant accessor, missing keys render as ``-``, control
characters and table pipes are neutralised, and nothing is ever evaluated.
Percentages are printed with two decimals, weight changes in percentage
points (``pp``) and amounts in US dollars with ``M``/``B`` suffixes.  The
output is deterministic for identical input.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from etf_tracker.rules import INDEX_FOR_ETF, SHARE_CLASS_GROUPS

__all__ = [
    "ETF_ORDER",
    "INDEX_NAMES",
    "EVENT_LABELS",
    "CLASSIFICATION_LABELS",
    "BREACH_LABELS",
    "TOP_N_FORCED",
    "TOP_N_CHANGES",
    "BOUNDARY_MARGIN",
    "format_pct",
    "format_pp",
    "format_usd",
    "format_int",
    "format_ratio",
    "render_markdown",
    "render_summary_line",
    "report_paths",
    "write_reports",
]

log = logging.getLogger(__name__)

#: Section order of the report; ETFs not listed follow alphabetically.
ETF_ORDER: tuple[str, ...] = ("SOXX", "QQQ", "IGV")

#: Index names shown in section titles.
INDEX_NAMES: dict[str, str] = {
    "SOXX": "NYSE Semiconductor Index",
    "QQQ": "Nasdaq-100 Index",
    "IGV": "S&P North American Expanded Technology Software Index",
}

#: Korean labels of ``RebalanceEvent.kind`` / ``caps.event_type``.
EVENT_LABELS: dict[str, str] = {
    "quarterly_rebalance": "분기 리밸런스",
    "annual_reconstitution": "연간 재구성",
    "semiannual_reconstitution": "반기 재구성",
    "special_rebalance": "특별 리밸런스",
}

#: Korean labels of ``TickerChange.classification``.
CLASSIFICATION_LABELS: dict[str, str] = {
    "flow_only": "자금 흐름만",
    "active_trade": "실제 매매",
    "entry": "신규 편입",
    "exit": "제외",
    "corporate_action_suspect": "기업행동 의심",
}

#: Classifications listed under "실제 매매로 분류된 종목".
_ACTIVE_CLASSES: tuple[str, ...] = ("active_trade", "entry", "exit", "corporate_action_suspect")

#: Korean descriptions of ``Breach.rule`` (fallback: the English description).
BREACH_LABELS: dict[str, str] = {
    "soxx_single_cap_8": "단일 종목 8% 상한 초과",
    "soxx_outside_top5_cap_4": "상위 5 밖 종목 4% 상한 초과",
    "soxx_adr_aggregate_10": "ADR 합계 10% 상한 초과",
    "qqq_company_24": "회사 비중 24% 발동 기준 초과 (20%로 조정 대상)",
    "qqq_cohort_48": "4.5% 초과 회사 합계 48% 이상 (40%로 조정 대상)",
    "qqq_security_15": "증권 비중 15% 발동 기준 초과 (14%로 조정 대상, 12월 전용)",
    "qqq_top5_40": "상위 5 증권 합계 40% 이상 (38.5%로 조정 대상, 12월 전용)",
    "igv_single_cap_8_5": "단일 회사 8.5% 상한 초과",
    "igv_cohort_45": "4.5% 초과 회사 합계 45% 상한 초과",
}

#: Short labels for the one-line summary.
_BREACH_SHORT: dict[str, str] = {
    "soxx_single_cap_8": "8% 초과",
    "soxx_outside_top5_cap_4": "상위 5 밖 4% 초과",
    "soxx_adr_aggregate_10": "ADR 합계 10% 초과",
    "qqq_company_24": "회사 24% 초과",
    "qqq_cohort_48": "4.5% 초과 회사 합계 48% 이상",
    "qqq_security_15": "증권 15% 초과",
    "qqq_top5_40": "상위 5 증권 합계 40% 이상",
    "igv_single_cap_8_5": "8.5% 초과",
    "igv_cohort_45": "4.5% 초과 회사 합계 45% 초과",
}

#: Rules whose ``value`` is an aggregate rather than the largest single weight.
_AGGREGATE_RULES: frozenset[str] = frozenset(
    {"soxx_adr_aggregate_10", "qqq_cohort_48", "qqq_top5_40", "igv_cohort_45"}
)

#: Indices whose caps aggregate share classes (mirrors ``bridge.COMPANY_LEVEL_INDICES``).
_COMPANY_LEVEL: frozenset[str] = frozenset({"QQQ", "IGV"})

#: Rows in the forced-seller / forced-buyer tables.
TOP_N_FORCED = 5
#: Rows in the decomposition table.
TOP_N_CHANGES = 10
#: Headroom below which a metric is flagged "경계" (0.25 percentage points,
#: docs/methodology.md section 3.2 item 6).
BOUNDARY_MARGIN = 0.0025

_EPS = 1e-12
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Korean renderings of the binding-constraint labels ``etf_tracker.rules``
#: produces (``prefix -> replacement``; the ticker list that follows the
#: prefix is kept).  Unknown labels are shown as they are.
_CONSTRAINT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("SOXX 8% single-name cap: ", "SOXX 단일 종목 8% 상한: "),
    ("SOXX 4% cap outside initial top-5: ", "SOXX 상위 5 밖 종목 4% 상한: "),
    ("SOXX 10% aggregate ADR cap: ", "SOXX ADR 합계 10% 상한: "),
    ("QQQ company 20% cap (24% trigger): ", "QQQ 회사 20% 상한 (24% 발동): "),
    ("QQQ aggregate >4.5% cohort cut to 40% (48% trigger): ", "QQQ 4.5% 초과 회사 합계 40%로 축소 (48% 발동): "),
    ("QQQ security 14% cap (15% trigger): ", "QQQ 증권 14% 상한 (15% 발동): "),
    ("QQQ top-5 securities cut to 38.5% (40% trigger): ", "QQQ 상위 5 증권 합계 38.5%로 축소 (40% 발동): "),
    ("IGV 8.5% single-company cap: ", "IGV 단일 회사 8.5% 상한: "),
    ("IGV aggregate 45% rule (companies >4.5%): reduced ", "IGV 4.5% 초과 회사 합계 45% 규칙으로 축소: "),
)

#: Korean renderings of the warning strings ``etf_tracker.analysis`` and the
#: analysis core emit (``pattern -> replacement template``).  The first match
#: wins; a warning no rule matches is shown as it is.
_WARNING_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^no previous snapshot: drift/trade decomposition skipped$"),
        "전일 스냅샷이 없어 비중 변화 분해를 건너뛰었습니다.",
    ),
    (
        re.compile(r"^snapshot as of (\S+) is (\d+) trading day\(s\) older than the last completed session (\S+)$"),
        r"보유 내역 기준일 \1: 마지막 완료 거래일 \3보다 \2거래일 오래된 데이터입니다.",
    ),
    (
        re.compile(r"^snapshot as of (\S+) is after today (\S+)$"),
        r"보유 내역 기준일 \1: 분석 기준일 \2보다 뒤의 날짜입니다.",
    ),
    (
        re.compile(r"^ADR flag inferred from listing location for: (.+)$"),
        r"ADR 추정 종목: \1. 이름에 ADR 표기는 없지만 상장 지역이 미국 밖이어서 ADR로 간주했으므로 확인이 필요합니다.",
    ),
    (
        re.compile(r"^ADR flag overridden to False by configuration for: (.+)$"),
        r"ADR 제외 설정 종목: \1. 미국 밖 상장이지만 ADR이 아닌 보통주로 상장되어 있어 ADR 합계에서 제외했습니다.",
    ),
    (
        re.compile(r"^decomposition: (\S+): exited; previous price used for its counterfactual value$"),
        r"비중 변화 분해: \1 제외 종목의 반사실 비중은 전일 가격으로 계산했습니다.",
    ),
    (re.compile(r"^decomposition failed: (.*)$"), r"비중 변화 분해 실패: \1"),
    (re.compile(r"^decomposition: (.*)$"), r"비중 변화 분해: \1"),
    (
        re.compile(r"^(\S+): derived price (\S+) from market_value/shares$"),
        r"\1: 가격이 없어 시가/주식수로 \2를 계산해 사용했습니다.",
    ),
    (
        re.compile(r"^(\S+): derived market_value (\S+) from shares\*price$"),
        r"\1: 시가가 없어 주식수x가격으로 \2를 계산해 사용했습니다.",
    ),
    (re.compile(r"^special rebalance trigger breached: (.*)$"), r"특별 리밸런스 발동 조건 충족: \1"),
    (re.compile(r"^caps infeasible under (\S+): (.*)$"), r"상한 재적용 미수렴 (\1): \2"),
    (
        re.compile(r"^caps skipped: no equity line with a positive weight$"),
        "양의 비중을 가진 주식 라인이 없어 상한 점검을 건너뛰었습니다.",
    ),
    (re.compile(r"^caps failed: (.*)$"), r"상한 재적용 실패: \1"),
    (re.compile(r"^constraint check failed: (.*)$"), r"상한 위반 점검 실패: \1"),
    (re.compile(r"^source (?:validation_warnings|parse_warnings|warnings): (.*)$"), r"원본 데이터 경고: \1"),
    (
        re.compile(r"^weights sum to (\S+), expected 1 \+/- (\S+)(.*)$"),
        r"비중 합계가 \1로 허용 범위 1 ± \2를 벗어났습니다\3.",
    ),
    (re.compile(r"^duplicate ticker '(.+)' appears (\d+) times$"), r"티커 \1이(가) \2번 중복됩니다."),
    (re.compile(r"^snapshot has no holdings$"), "스냅샷에 보유 종목이 없습니다."),
)
_UTC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


# --------------------------------------------------------------------------
# Tolerant accessors
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """A finite float, or ``None`` for anything that is not a plain number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip().replace(",", ""))
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _text(value: Any, missing: str = "-") -> str:
    """A single-line string without control characters; ``missing`` for ``None``."""
    if value is None:
        return missing
    text = _CONTROL_RE.sub(" ", str(value)).strip()
    return text or missing


def _cell(value: Any) -> str:
    """Table-safe text (pipes escaped)."""
    return _text(value).replace("|", "\\|")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _iso_date(value: Any) -> str | None:
    """The value as a validated ISO date string, or ``None``."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()[:10]
        if _ISO_DATE_RE.match(text):
            try:
                return date.fromisoformat(text).isoformat()
            except ValueError:
                return None
    return None


def _date_text(value: Any, missing: str = "-") -> str:
    return _iso_date(value) or missing


# --------------------------------------------------------------------------
# Number formatting (public: the CLI and notifications reuse them)
# --------------------------------------------------------------------------


def format_pct(value: Any, digits: int = 2) -> str:
    """Fraction -> ``'9.50%'``; ``'-'`` when missing."""
    number = _num(value)
    return "-" if number is None else f"{number * 100:.{digits}f}%"


def _rounded_points(number: float, digits: int) -> float:
    """``number`` in percent, rounded; a value that rounds to zero becomes
    a positive 0.0 so it never prints as ``-0.00``."""
    points = round(number * 100, digits)
    return 0.0 if points == 0 else points


def format_pp(value: Any, digits: int = 2, signed: bool = True) -> str:
    """Fraction difference -> ``'+1.23pp'`` (or ``'1.23pp'`` unsigned);
    a change that rounds to zero prints as ``'0.00pp'``."""
    number = _num(value)
    if number is None:
        return "-"
    points = _rounded_points(number, digits)
    if points == 0 or not signed:
        return f"{points:.{digits}f}pp"
    return f"{points:+.{digits}f}pp"


def format_usd(value: Any) -> str:
    """USD amount with thousands separators and an M/B suffix: ``'1.23B USD'``."""
    number = _num(value)
    if number is None:
        return "-"
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    if magnitude >= 1e9:
        return f"{sign}{magnitude / 1e9:,.2f}B USD"
    if magnitude >= 1e6:
        return f"{sign}{magnitude / 1e6:,.2f}M USD"
    return f"{sign}{magnitude:,.0f} USD"


def format_int(value: Any) -> str:
    """Count-like number with thousands separators (``'82,700,000'``)."""
    number = _num(value)
    return "-" if number is None else f"{number:,.0f}"


def format_ratio(value: Any, digits: int = 4) -> str:
    """Plain ratio such as the flow factor k (``'1.0062'``)."""
    number = _num(value)
    return "-" if number is None else f"{number:.{digits}f}"


def _format_signed_pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    points = _rounded_points(number, 2)
    return f"{points:.2f}%" if points == 0 else f"{points:+.2f}%"


def _utc_text(value: Any) -> str:
    """``'2026-10-09T14:05:00Z'`` -> ``'2026-10-09 14:05:00 UTC'``."""
    if value is None:
        return "알 수 없음"
    text = _text(value)
    match = _UTC_RE.match(text)
    if match:
        return f"{match.group(1)} {match.group(2)} UTC"
    return text


def _localize_warning(text: str) -> str:
    """Korean sentence for a known English warning; the text itself otherwise."""
    for pattern, template in _WARNING_RULES:
        match = pattern.match(text)
        if match:
            out = match.expand(template)
            for event_type, label in EVENT_LABELS.items():
                out = out.replace(event_type, label)
            return out
    return text


def _localize_constraint(text: str) -> str:
    """Korean label for a known binding-constraint string; unchanged otherwise."""
    for prefix, replacement in _CONSTRAINT_PREFIXES:
        if text.startswith(prefix):
            return replacement + text[len(prefix):]
    return text


# --------------------------------------------------------------------------
# Shape normalisation
# --------------------------------------------------------------------------


def _as_report(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Accept an ``analyze_all`` dict or a single ``analyze_etf`` dict."""
    analysis = _mapping(analysis)
    if "etfs" in analysis or "errors" in analysis:
        return {
            "generated_utc": analysis.get("generated_utc"),
            "today": analysis.get("today"),
            "etfs": {str(k): _mapping(v) for k, v in _mapping(analysis.get("etfs")).items()},
            "errors": {str(k): v for k, v in _mapping(analysis.get("errors")).items()},
        }
    etf = _text(analysis.get("etf"), "ETF").upper()
    return {
        "generated_utc": analysis.get("generated_utc"),
        "today": analysis.get("today") or analysis.get("as_of"),
        "etfs": {etf: dict(analysis)} if analysis else {},
        "errors": {},
    }


def _today_of(report: Mapping[str, Any]) -> str:
    """The report date: ``today``, else the latest ``as_of``, else the
    generation date, else the wall clock (UTC)."""
    today = _iso_date(report.get("today"))
    if today:
        return today
    as_ofs = [d for d in (_iso_date(_mapping(e).get("as_of")) for e in _mapping(report.get("etfs")).values()) if d]
    if as_ofs:
        return max(as_ofs)
    generated = _iso_date(report.get("generated_utc"))
    if generated:
        return generated
    return datetime.now(tz=None).date().isoformat()


def _etf_order(report: Mapping[str, Any]) -> list[str]:
    present = set(_mapping(report.get("etfs"))) | set(_mapping(report.get("errors")))
    ordered = [etf for etf in ETF_ORDER if etf in present]
    ordered.extend(sorted(present - set(ETF_ORDER)))
    return ordered


def _index_id(etf: str, entry: Mapping[str, Any]) -> str:
    return _text(entry.get("index_id"), INDEX_FOR_ETF.get(etf, etf)).upper()


# --------------------------------------------------------------------------
# Weight helpers
# --------------------------------------------------------------------------


def _equity_holdings(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for raw in _sequence(entry.get("holdings")):
        holding = _mapping(raw)
        if not holding:
            continue
        if _text(holding.get("asset_class"), "equity") != "equity":
            continue
        out.append(holding)
    return out


def _equity_weights(entry: Mapping[str, Any]) -> dict[str, float]:
    """Equity-only weights normalised to one, by ticker.

    Market values are used when every equity line has one (the basis the
    capping rules use), otherwise the published fund weights.  Duplicate
    tickers are aggregated; non-positive lines are dropped.
    """
    equities = _equity_holdings(entry)
    use_mv = bool(equities) and all(_num(h.get("market_value")) is not None for h in equities)
    raw: dict[str, float] = {}
    for holding in equities:
        value = _num(holding.get("market_value") if use_mv else holding.get("weight"))
        if value is None or value <= 0:
            continue
        ticker = _text(holding.get("ticker"))
        raw[ticker] = raw.get(ticker, 0.0) + value
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {t: v / total for t, v in raw.items()}


def _company_weights(entry: Mapping[str, Any], index_id: str, weights: Mapping[str, float]) -> dict[str, float]:
    """Aggregate ``weights`` by company id (share classes merged for QQQ/IGV)."""
    company_of: dict[str, str] = {}
    for holding in _equity_holdings(entry):
        ticker = _text(holding.get("ticker"))
        if ticker in company_of:
            continue
        explicit = holding.get("company_id")
        if explicit:
            company_of[ticker] = _text(explicit)
        elif index_id in _COMPANY_LEVEL:
            company_of[ticker] = SHARE_CLASS_GROUPS.get(ticker.upper(), ticker)
        else:
            company_of[ticker] = ticker
    out: dict[str, float] = {}
    for ticker, weight in weights.items():
        company = company_of.get(ticker, ticker)
        out[company] = out.get(company, 0.0) + weight
    return out


def _current_and_target(
    caps: Mapping[str, Any], weights: Mapping[str, float]
) -> tuple[Mapping[str, float], Mapping[str, float] | None]:
    """Current and target weights by ticker; the caps dict wins when it
    carries them, otherwise current comes from the holdings and target is
    reconstructed as current + delta by the caller."""
    current_raw = _mapping(caps.get("current_weights"))
    target_raw = _mapping(caps.get("target_weights"))
    current = {str(t): w for t, w in ((t, _num(v)) for t, v in current_raw.items()) if w is not None} or dict(weights)
    target = {str(t): w for t, w in ((t, _num(v)) for t, v in target_raw.items()) if w is not None} or None
    return current, target


def _total_net_assets(entry: Mapping[str, Any]) -> float | None:
    """Fund TNA: ``fund.total_net_assets``, else NAV x shares outstanding,
    else the sum of holding market values."""
    fund = _mapping(entry.get("fund"))
    tna = _num(fund.get("total_net_assets"))
    if tna is not None and tna > 0:
        return tna
    nav = _num(fund.get("nav"))
    shares = _num(fund.get("shares_outstanding"))
    if nav is not None and shares is not None and nav > 0 and shares > 0:
        return nav * shares
    total = 0.0
    seen = False
    for raw in _sequence(entry.get("holdings")):
        mv = _num(_mapping(raw).get("market_value"))
        if mv is not None:
            total += mv
            seen = True
    return total if seen and total > 0 else None


# --------------------------------------------------------------------------
# Markdown building blocks
# --------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], align: str = "") -> list[str]:
    """Markdown table; ``align`` holds one ``l``/``r`` per column (``l`` is
    the default) so numeric columns line up on the right."""
    seps = []
    for index in range(len(headers)):
        seps.append("---:" if index < len(align) and align[index] == "r" else "---")
    lines = ["| " + " | ".join(_cell(h) for h in headers) + " |", "|" + "|".join(seps) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return lines


def _bullets(items: Sequence[Any], empty: str = "없음") -> list[str]:
    texts = [_text(item) for item in items if _text(item, "") != ""]
    if not texts:
        return [f"- {empty}"]
    return [f"- {t}" for t in texts]


def _headroom_text(value: float | None, limit: float) -> str:
    if value is None:
        return "-"
    room = limit - value
    if room >= -_EPS:
        text = f"여유 {format_pp(room, signed=False)}"
        if room < BOUNDARY_MARGIN:
            text += " (경계)"
        return text
    return f"초과 {format_pp(-room, signed=False)}"


def _limit_row(label: str, value: Any, limit: float, target: float | None = None, suffix: str = "") -> list[str]:
    number = _num(value)
    limit_text = format_pct(limit)
    notes: list[str] = []
    if target is not None:
        notes.append(f"조정 목표 {format_pct(target)}")
    if suffix:
        notes.append(suffix.strip("()"))
    if notes:
        limit_text += f" ({', '.join(notes)})"
    return [label, format_pct(number), limit_text, _headroom_text(number, limit)]


def _count_row(label: str, value: Any, unit: str, limit_text: str = "-", zero_is_limit: bool = False) -> list[str]:
    number = _num(value)
    if number is None:
        return [label, "-", limit_text, "-"]
    count = int(round(number))
    if not zero_is_limit:
        return [label, f"{count}{unit}", limit_text, "-"]
    return [label, f"{count}{unit}", limit_text, "없음" if count <= 0 else f"초과 {count}{unit}"]


#: Tag on the QQQ rows whose threshold also triggers a special rebalance.
_SPECIAL_TAG = "(특별 리밸런스 발동 기준)"


def _limit_specs(index_id: str, event_type: str) -> list[tuple[str, str, float, float | None, str]]:
    """``(label, metric key, limit, adjustment target, suffix)`` for every
    capped metric of ``index_id`` under ``event_type`` (methodology 2.0).

    SOXX: 8% single name, 10% aggregate ADR.  QQQ: company 24% (cut to
    20%) and the >4.5% cohort 48% (cut to 40%), both special-rebalance
    triggers; in December also security 15% (to 14%) and top-5 securities
    40% (to 38.5%).  IGV: company 8.5%, >4.5% cohort 45%.
    """
    if index_id == "SOXX":
        return [
            ("최대 종목 비중", "max_weight", 0.08, None, ""),
            ("ADR 합계 비중", "adr_sum", 0.10, None, ""),
        ]
    if index_id == "QQQ":
        specs = [
            ("최대 회사 비중", "max_company_weight", 0.24, 0.20, _SPECIAL_TAG),
            ("4.5% 초과 회사 합계", "sum_over_4_5", 0.48, 0.40, _SPECIAL_TAG),
        ]
        if event_type == "annual_reconstitution":
            specs.append(("최대 증권 비중 (12월 전용)", "max_security_weight", 0.15, 0.14, ""))
            specs.append(("상위 5 증권 합계 (12월 전용)", "top5_security_sum", 0.40, 0.385, ""))
        return specs
    if index_id == "IGV":
        return [
            ("최대 회사 비중", "max_weight", 0.085, None, ""),
            ("4.5% 초과 회사 합계", "sum_over_4_5", 0.45, None, ""),
        ]
    return []


def _metric_rows(index_id: str, metrics: Mapping[str, Any], event_type: str) -> list[list[str]]:
    rows: list[list[str]] = []
    specs = {key: (label, limit, target, suffix) for label, key, limit, target, suffix in _limit_specs(index_id, event_type)}

    def limit(key: str) -> list[str]:
        label, cap, target, suffix = specs[key]
        return _limit_row(label, metrics.get(key), cap, target=target, suffix=suffix)

    if index_id == "SOXX":
        rows.append(limit("max_weight"))
        rows.append(
            _count_row(
                "상위 5 밖 종목 중 4% 초과 개수",
                metrics.get("names_over_4_outside_top5"),
                "종목",
                limit_text="0종목 (종목별 4.00%)",
                zero_is_limit=True,
            )
        )
        rows.append(limit("adr_sum"))
        rows.append(_count_row("8% 초과 종목 수", metrics.get("names_over_8"), "종목", limit_text="0종목", zero_is_limit=True))
        rows.append(["상위 5 종목 합계", format_pct(metrics.get("top5_sum")), "-", "-"])
    elif index_id == "QQQ":
        rows.append(limit("max_company_weight"))
        rows.append(limit("sum_over_4_5"))
        rows.append(_count_row("4.5% 초과 회사 수", metrics.get("names_over_4_5"), "개 회사"))
        if event_type == "annual_reconstitution":
            rows.append(limit("max_security_weight"))
            rows.append(limit("top5_security_sum"))
        else:
            rows.append(["상위 5 증권 합계", format_pct(metrics.get("top5_security_sum")), "-", "-"])
    elif index_id == "IGV":
        rows.append(limit("max_weight"))
        rows.append(limit("sum_over_4_5"))
        rows.append(_count_row("4.5% 초과 회사 수", metrics.get("names_over_4_5"), "개 회사"))
    else:
        for key in sorted(metrics):
            rows.append([_text(key), format_pct(metrics.get(key)), "-", "-"])
    return rows


def _boundary_alerts(index_id: str, metrics: Mapping[str, Any], event_type: str) -> list[str]:
    """Metrics within :data:`BOUNDARY_MARGIN` below their limit, for the
    summary line (``'ADR 합계 비중 9.79% (한도 10.00%, 경계)'``)."""
    out: list[str] = []
    for label, key, cap, _target, _suffix in _limit_specs(index_id, event_type):
        value = _num(metrics.get(key))
        if value is None:
            continue
        room = cap - value
        if -_EPS <= room < BOUNDARY_MARGIN:
            out.append(f"{label} {format_pct(value)} (한도 {format_pct(cap)}, 경계)")
    return out


def _breach_line(breach: Mapping[str, Any]) -> str:
    rule = _text(breach.get("rule"), "")
    label = BREACH_LABELS.get(rule) or _text(breach.get("description"), rule or "규칙 위반")
    tickers = [_text(t) for t in _sequence(breach.get("tickers"))]
    shown = ", ".join(tickers[:8])
    if len(tickers) > 8:
        shown += f" 외 {len(tickers) - 8}종목"
    value_label = "합계" if rule in _AGGREGATE_RULES else "최대"
    return (
        f"{label}: {shown or '-'} ({value_label} {format_pct(breach.get('value'))}, "
        f"한도 {format_pct(breach.get('limit'))})"
    )


def _forced_pairs(pairs: Sequence[Any], sellers: bool) -> list[tuple[str, float]]:
    """``(ticker, delta)`` pairs in report order: sellers most negative
    first, buyers largest first.  The producer already sorts them; sorting
    again keeps the order right for any input."""
    keyed: list[tuple[str, float, int]] = []
    for position, raw in enumerate(_sequence(pairs)):
        pair = _sequence(raw)
        if len(pair) < 2:
            continue
        delta = _num(pair[1])
        if delta is None:
            continue
        keyed.append((_text(pair[0]), delta, position))
    keyed.sort(key=lambda item: (item[1] if sellers else -item[1], item[2]))
    return [(ticker, delta) for ticker, delta, _ in keyed]


def _forced_heading(kind: str, pairs: Sequence[tuple[str, float]], explanation: str) -> str:
    total = len(pairs)
    shown = min(total, TOP_N_FORCED)
    if total > TOP_N_FORCED:
        return f"{kind} 상위 {shown} (총 {total}종목, {explanation}):"
    return f"{kind} 상위 {TOP_N_FORCED} ({explanation}):"


def _adr_line(entry: Mapping[str, Any], weights: Mapping[str, float]) -> str | None:
    """SOXX: which names count toward the 10% ADR aggregate and their
    weights, largest first (``None`` when no holding is flagged)."""
    adrs: dict[str, float] = {}
    for holding in _equity_holdings(entry):
        if holding.get("is_adr") is True:
            ticker = _text(holding.get("ticker"))
            adrs[ticker] = weights.get(ticker, 0.0)
    if not adrs:
        return None
    ranked = sorted(adrs.items(), key=lambda item: (-item[1], item[0]))
    listed = ", ".join(f"{t} {format_pct(w)}" for t, w in ranked)
    return f"ADR 합계에 포함된 종목은 {len(ranked)}개(합계 {format_pct(sum(adrs.values()))})이며 비중 순으로 {listed}입니다."


def _forced_table(
    pairs: Sequence[tuple[str, float]],
    current: Mapping[str, float],
    target: Mapping[str, float] | None,
    tna: float | None,
) -> list[str]:
    rows: list[list[str]] = []
    for ticker, delta in pairs[:TOP_N_FORCED]:
        w_now = _num(current.get(ticker))
        if target is not None and _num(target.get(ticker)) is not None:
            w_target = _num(target.get(ticker))
        elif w_now is not None:
            w_target = w_now + delta
        else:
            w_target = None
        row = [ticker, format_pct(w_now), format_pct(w_target), format_pp(delta)]
        if tna is not None:
            row.append(format_usd(delta * tna))
        rows.append(row)
    if not rows:
        return ["- 없음"]
    headers = ["종목", "현재 비중", "목표 비중", "변화(pp)"]
    if tna is not None:
        headers.append("추정 금액")
    return _table(headers, rows, align="lrrrr")


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def _summary_table(report: Mapping[str, Any]) -> list[str]:
    etfs = _mapping(report.get("etfs"))
    errors = _mapping(report.get("errors"))
    rows: list[list[str]] = []
    for etf in _etf_order(report):
        entry = _mapping(etfs.get(etf))
        if not entry:
            rows.append([etf, "오류", "-", "-", "-", "-"])
            continue
        fund = _mapping(entry.get("fund"))
        so = _num(fund.get("shares_outstanding"))
        so_prev = _num(fund.get("prev_shares_outstanding"))
        change = _format_signed_pct(so / so_prev - 1.0) if so is not None and so_prev else "-"
        decomposition = _mapping(entry.get("decomposition"))
        rows.append(
            [
                etf,
                _date_text(entry.get("as_of")),
                _date_text(entry.get("prev_as_of")),
                format_int(so),
                change,
                format_ratio(decomposition.get("scale_factor")) if decomposition else "-",
            ]
        )
    lines = _table(
        ["ETF", "보유 내역 기준일", "전일 스냅샷", "발행주식수", "전일 대비 발행주식수 변화율", "자금 유출입 추정 배율 k"],
        rows,
        align="lllrrr",
    )
    failed = [etf for etf in _etf_order(report) if etf not in etfs]
    if failed:
        lines.append("")
        lines.append(
            "다음 ETF는 이번 실행에서 분석하지 못했습니다: "
            + ", ".join(f"{etf} ({_text(errors.get(etf), '원인 미상')})" for etf in failed)
            + "."
        )
    return lines


def _intro_line(entry: Mapping[str, Any]) -> str:
    fund = _mapping(entry.get("fund"))
    n_equities = len(_equity_holdings(entry))
    parts = [
        f"보유 내역 기준일은 {_date_text(entry.get('as_of'))}이고 출처는 {_text(entry.get('source'), '미상')}입니다.",
        f"주식 종목 수는 {n_equities}개입니다.",
    ]
    details: list[str] = []
    so = _num(fund.get("shares_outstanding"))
    if so is not None:
        details.append(f"발행주식수는 {format_int(so)}주")
    tna = _num(fund.get("total_net_assets"))
    if tna is not None:
        details.append(f"순자산은 {format_usd(tna)}")
    nav = _num(fund.get("nav"))
    if nav is not None:
        details.append(f"NAV는 {nav:,.2f} USD")
    if details:
        parts.append(", ".join(details) + "입니다.")
    return " ".join(parts)


def _caps_section(etf: str, index_id: str, entry: Mapping[str, Any]) -> list[str]:
    caps = _mapping(entry.get("caps"))
    metrics = _mapping(caps.get("metrics"))
    event_type = _text(caps.get("event_type"), "quarterly_rebalance")
    event_label = EVENT_LABELS.get(event_type, event_type)
    lines = ["### 상한 점검", ""]
    intro = (
        f"다음 이벤트인 {event_label}의 규칙을 기준으로 오늘의 보유 비중(주식만으로 재정규화한 값)을 점검했습니다. "
        "한도까지 남은 여유가 0.25pp 이내이면 (경계)로 표시합니다."
    )
    if index_id == "SOXX":
        intro += " 상위 5개 종목은 오늘의 비중 순위로 정했으며, 지수는 참조일 종가의 상한 적용 전 순위를 씁니다."
    lines.append(intro)
    lines.append("")
    lines.extend(_table(["지표", "현재값", "한도", "여유/초과"], _metric_rows(index_id, metrics, event_type), align="lrll"))

    weights = _equity_weights(entry)
    if index_id == "SOXX":
        adr_line = _adr_line(entry, weights)
        if adr_line:
            lines.append("")
            lines.append(adr_line)

    if index_id == "QQQ":
        lines.append("")
        lines.append(_special_rebalance_line(caps))

    lines.append("")
    lines.append("위반 목록:")
    lines.append("")
    breaches = [_mapping(b) for b in _sequence(caps.get("breaches"))]
    lines.extend(_bullets([_breach_line(b) for b in breaches if b]))

    current, target = _current_and_target(caps, weights)
    tna = _total_net_assets(entry)
    sellers = _forced_pairs(caps.get("forced_sellers"), sellers=True)
    buyers = _forced_pairs(caps.get("forced_buyers"), sellers=False)
    lines.append("")
    lines.append(_forced_heading("강제 매도", sellers, "상한 재적용 시 비중이 줄어드는 종목"))
    lines.append("")
    lines.extend(_forced_table(sellers, current, target, tna))
    lines.append("")
    lines.append(_forced_heading("강제 매수", buyers, "재분배를 받아 비중이 늘어나는 종목"))
    lines.append("")
    lines.extend(_forced_table(buyers, current, target, tna))

    extras: list[str] = []
    feasible = caps.get("feasible")
    if feasible is False:
        extras.append("상한 재적용이 수렴하지 않았습니다. 목표 비중은 모든 제약을 만족하지 않습니다.")
    turnover = _num(metrics.get("turnover"))
    if turnover is not None and turnover <= _EPS:
        extras.append("상한을 다시 적용해도 비중이 바뀌지 않으므로 예상 강제 매매가 없습니다.")
    elif turnover is not None:
        text = f"예상 일방향 회전율은 {format_pct(turnover)}"
        if tna is not None:
            text += f" (약 {format_usd(turnover * tna)})"
        extras.append(text + "입니다.")
    binding = [_localize_constraint(_text(b)) for b in _sequence(caps.get("binding_constraints"))]
    if binding:
        extras.append("구속 제약: " + "; ".join(binding))
    notes = [_text(n) for n in _sequence(caps.get("notes"))]
    if notes:
        extras.append("참고: " + " ".join(notes))
    if extras:
        lines.append("")
        lines.extend(f"- {e}" for e in extras)
    return lines


#: Korean renderings of ``QQQRules.special_rebalance_triggered`` reasons.
_TRIGGER_REASON_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^company (\S+) weight (\S+%) exceeds 24%$"),
        r"회사 \1의 비중 \2가 24%를 초과",
    ),
    (
        re.compile(r"^companies above 4\.5% \((.+)\) sum to (\S+%), exceeding 48%$"),
        r"4.5% 초과 회사(\1)의 합계 \2가 48%를 초과",
    ),
    (
        re.compile(r"^companies above 4\.5% sum to (\S+%), (?:exceeding 48%|48% or more)$"),
        r"4.5% 초과 회사의 합계 \1가 48%를 초과",
    ),
)


def _localize_trigger_reason(text: str) -> str:
    for pattern, template in _TRIGGER_REASON_RULES:
        match = pattern.match(text)
        if match:
            return match.expand(template)
    return text


def _special_rebalance_line(caps: Mapping[str, Any]) -> str:
    """The Nasdaq-100 special-rebalance verdict as one complete sentence."""
    special = _mapping(caps.get("special_rebalance"))
    if not special:
        return "특별 리밸런스 트리거: 판정 정보가 없습니다."
    metrics = _mapping(special.get("metrics"))
    detail = (
        f"최대 회사 비중 {format_pct(metrics.get('max_company_weight'))}(기준 24%), "
        f"4.5% 초과 회사 합계 {format_pct(metrics.get('sum_over_4_5'))}(기준 48%)"
    )
    if special.get("triggered"):
        reasons = [_localize_trigger_reason(_text(r)) for r in _sequence(special.get("reasons"))]
        reason_text = "; ".join(r for r in reasons if r) or "사유 미상"
        return (
            f"특별 리밸런스 트리거: 발동 조건 충족. {detail}입니다. 사유: {reason_text}. "
            "Nasdaq이 특별 리밸런스를 공지하면 참조일과 효력일이 별도로 정해집니다."
        )
    return f"특별 리밸런스 트리거: 미발동. {detail}로 두 기준을 모두 넘지 않았습니다."


def _days_text(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    days = int(number)
    if days < 0:
        return f"{days} (경과)"
    if days == 0:
        return "0 (오늘)"
    return str(days)


def _events_section(entry: Mapping[str, Any]) -> list[str]:
    lines = ["### 다음 이벤트", ""]
    keyed: list[tuple[tuple[str, str, int], list[str]]] = []
    for position, raw in enumerate(_sequence(entry.get("next_events"))):
        event = _mapping(raw)
        if not event:
            continue
        kind = _text(event.get("kind"), "")
        reference = _iso_date(event.get("reference_date"))
        trade = _iso_date(event.get("effective_trade_date"))
        # Chronological by reference date (an IGV reconstitution reference
        # precedes the quarterly pricing reference of the same trade date);
        # the producer's order breaks ties.
        keyed.append(
            (
                (reference or "9999-99-99", trade or "9999-99-99", position),
                [
                    EVENT_LABELS.get(kind, kind or "-"),
                    _date_text(event.get("reference_date")),
                    _date_text(event.get("announcement_date"), "미공표"),
                    _date_text(event.get("effective_trade_date")),
                    _date_text(event.get("effective_date")),
                    _days_text(event.get("trading_days_to_reference")),
                    _days_text(event.get("trading_days_to_trade")),
                ],
            )
        )
    rows = [row for _, row in sorted(keyed, key=lambda item: item[0])]
    if not rows:
        lines.append("예정된 이벤트 정보가 없습니다.")
        return lines
    lines.extend(
        _table(
            ["종류", "참조일", "발표일", "매매일(종가)", "효력일", "참조일까지 거래일", "매매일까지 거래일"],
            rows,
            align="lllllrr",
        )
    )
    lines.append("")
    lines.append(
        "참조일 종가 비중이 상한 판정의 기준이고, 매매일 종가에 지수 추종 자금의 강제 매매가 집행됩니다. "
        "남은 거래일 수는 분석 기준일 다음 거래일부터 해당 날짜까지 센 NYSE 거래일 수입니다. "
        "발표일이 미공표로 표시된 이벤트는 지수 제공자가 발표 일정 규칙을 공개하지 않아 날짜를 계산할 수 없는 경우입니다."
    )
    return lines


def _k_sentence(k: float | None, fund: Mapping[str, Any], implied_usd: float | None = None) -> str:
    """Flow interpretation of the scale factor k, with the implied dollar
    amount when the decomposition summary carries one."""
    if k is None:
        return "자금 유출입 추정 배율 k를 계산할 수 없었습니다."
    flow = k - 1.0
    amount = f"({format_usd(abs(implied_usd))} 상당)" if implied_usd is not None and abs(implied_usd) >= 1 else ""
    if abs(flow) < 0.0005:
        body = f"k={format_ratio(k)}이므로 전일 대비 순설정이나 순환매가 거의 없었던 것으로 추정됩니다."
    elif flow > 0:
        body = f"k={format_ratio(k)}이므로 전일 대비 약 {flow * 100:.2f}%{amount}의 설정(자금 유입)이 있었던 것으로 추정됩니다."
    else:
        body = f"k={format_ratio(k)}이므로 전일 대비 약 {-flow * 100:.2f}%{amount}의 환매(자금 유출)가 있었던 것으로 추정됩니다."
    so = _num(fund.get("shares_outstanding"))
    so_prev = _num(fund.get("prev_shares_outstanding"))
    if so is not None and so_prev:
        body += (
            f" 발행사 파일의 발행주식수 변화율은 {_format_signed_pct(so / so_prev - 1.0)}이며, "
            "설정·환매의 결제 시차 때문에 보유 내역보다 하루 늦게 반영될 수 있습니다."
        )
    return body


def _shares_text(value: Any) -> str:
    number = _num(value)
    return "0" if number is None else format_int(number)


def _active_line(change: Mapping[str, Any]) -> str:
    ticker = _text(change.get("ticker"))
    classification = _text(change.get("classification"), "")
    label = CLASSIFICATION_LABELS.get(classification, classification or "-")
    # Raw share counts as the issuer files show them; the flow-adjusted
    # change (rel_share_change) follows separately.
    before_raw = change.get("shares_prev")
    before = _shares_text(before_raw if _num(before_raw) is not None else change.get("shares_prev_adjusted"))
    after = _shares_text(change.get("shares_curr"))
    parts = [f"{ticker}: {label}, 주식수 {before} -> {after}"]
    rel = _num(change.get("rel_share_change"))
    if classification == "entry":
        parts.append(f"비중 {format_pct(change.get('w_curr'))}로 신규 편입")
    elif classification == "exit":
        parts.append(f"전일 비중 {format_pct(change.get('w_prev'))}에서 제외")
    elif rel is not None:
        parts.append(f"자금 흐름 조정 후 주식수 변화 {_format_signed_pct(rel)}")
    split = _num(change.get("split_ratio"))
    if split is not None and classification == "corporate_action_suspect":
        parts.append(f"추정 분할 비율 {split:g}:1")
    trade = _num(change.get("trade"))
    if trade is not None:
        parts.append(f"주식수 효과 {format_pp(trade)}")
    return ", ".join(parts)


def _decomposition_section(entry: Mapping[str, Any]) -> list[str]:
    decomposition = _mapping(entry.get("decomposition"))
    fund = _mapping(entry.get("fund"))
    if not decomposition:
        return [
            "### 비중 변화 분해",
            "",
            f"{_date_text(entry.get('as_of'))} 기준 첫 스냅샷을 저장했습니다. 전일 스냅샷이 없어 비중 변화 분해는 다음 거래일 스냅샷부터 시작됩니다.",
        ]
    prev_text = _date_text(decomposition.get("prev_as_of") or entry.get("prev_as_of"))
    curr_text = _date_text(decomposition.get("curr_as_of") or entry.get("as_of"))
    lines = [f"### 비중 변화 분해 ({prev_text} -> {curr_text})", ""]
    lines.append(
        "비중 변화를 가격 효과(drift, 주식수를 고정한 채 가격만 움직인 몫)와 주식수 효과(trade, 주식수 변화의 몫)로 나눕니다. "
        f"주식수 효과의 절대값이 큰 순서로, 같으면 비중 변화의 절대값이 큰 순서로 상위 {TOP_N_CHANGES}개 주식 종목을 표시합니다. "
        "이 표의 비중은 현금과 파생상품을 포함한 펀드 비중이라 상한 점검표의 주식 기준 비중보다 조금 작습니다."
    )
    lines.append("")

    changes = [_mapping(c) for c in _sequence(decomposition.get("changes"))]
    equities = [c for c in changes if c and _text(c.get("asset_class"), "equity") == "equity"]

    def total_change(change: Mapping[str, Any]) -> float | None:
        total = _num(change.get("total_change"))
        if total is None:
            w_curr = _num(change.get("w_curr"))
            w_prev = _num(change.get("w_prev"))
            if w_curr is not None and w_prev is not None:
                total = w_curr - w_prev
        return total

    def sort_key(change: Mapping[str, Any]) -> tuple[float, float, str]:
        # The trade effect is compared at the printed precision (0.01pp), so
        # on a day without trades the table is ordered by the weight change
        # instead of by floating-point noise in the share counts.
        trade = abs(_rounded_points(_num(change.get("trade")) or 0.0, 2))
        total = abs(total_change(change) or 0.0)
        return (-trade, -total, _text(change.get("ticker")))

    ranked = sorted(equities, key=sort_key)
    rows: list[list[str]] = []
    for change in ranked[:TOP_N_CHANGES]:
        classification = _text(change.get("classification"), "")
        rows.append(
            [
                _text(change.get("ticker")),
                format_pct(change.get("w_curr")),
                format_pct(change.get("w_prev")),
                format_pp(total_change(change)),
                format_pp(change.get("drift")),
                format_pp(change.get("trade")),
                CLASSIFICATION_LABELS.get(classification, classification or "-"),
            ]
        )
    if rows:
        lines.extend(
            _table(
                ["종목", "비중", "전일 비중", "변화(pp)", "가격 효과 drift(pp)", "주식수 효과 trade(pp)", "분류"],
                rows,
                align="lrrrrrl",
            )
        )
    else:
        lines.append("분해할 주식 종목이 없습니다.")

    summary = _mapping(decomposition.get("summary"))
    gross_drift = _num(summary.get("gross_drift"))
    gross_trade = _num(summary.get("gross_trade"))
    if gross_drift is not None or gross_trade is not None:
        lines.append("")
        lines.append(
            f"일방향 합계는 가격 효과 {format_pp(gross_drift, signed=False)}, 주식수 효과 {format_pp(gross_trade, signed=False)}입니다."
        )

    lines.append("")
    lines.append("실제 매매로 분류된 종목 (실제 매매, 신규 편입, 제외, 기업행동 의심):")
    lines.append("")
    active = [c for c in ranked if _text(c.get("classification"), "") in _ACTIVE_CLASSES]
    lines.extend(_bullets([_active_line(c) for c in active]))

    lines.append("")
    implied_usd = _num(_mapping(summary.get("fund_flow")).get("implied_flow_usd_from_scale_factor"))
    lines.append(_k_sentence(_num(decomposition.get("scale_factor")), fund, implied_usd))
    return lines


def _warnings_section(entry: Mapping[str, Any], error: Any) -> list[str]:
    lines = ["### 데이터 경고", ""]
    items = [_localize_warning(_text(w)) for w in _sequence(entry.get("warnings"))]
    if error is not None:
        items.insert(0, f"실행 오류: {_text(error)}")
    lines.extend(_bullets(items))
    return lines


def _etf_section(etf: str, report: Mapping[str, Any]) -> list[str]:
    entry = _mapping(_mapping(report.get("etfs")).get(etf))
    error = _mapping(report.get("errors")).get(etf)
    index_id = _index_id(etf, entry)
    title = f"## {etf} ({INDEX_NAMES.get(index_id, index_id)})"
    if not entry:
        return [title, "", f"이 ETF의 분석을 수행하지 못했습니다. 원인: {_text(error, '알 수 없음')}"]
    lines = [title, "", _intro_line(entry), ""]
    lines.extend(_caps_section(etf, index_id, entry))
    lines.append("")
    lines.extend(_events_section(entry))
    lines.append("")
    lines.extend(_decomposition_section(entry))
    lines.append("")
    lines.extend(_warnings_section(entry, error))
    return lines


def _footnotes() -> list[str]:
    return [
        "## 각주",
        "",
        "- 데이터 출처: 각 발행사 API(iShares, Invesco)에서 내려받은 ETF 보유 내역이며, 전 거래일(T-1) 종가 기준입니다.",
        "- 상한 점검표와 강제 매매표의 비중은 ETF 보유 비중을 주식만으로 재정규화한 값이고, 비중 변화 분해표의 비중은 현금과 파생상품을 포함한 펀드 비중입니다. 둘 다 지수 비중의 근사치이며, 현금과 파생상품, 공개 시점 차이 때문에 지수 제공자의 공식 비중과 다를 수 있습니다.",
        "- 강제 매매의 추정 금액은 비중 변화(pp)에 ETF 순자산을 곱한 값이고, 예상 일방향 회전율은 상한 재적용으로 줄어드는 비중의 합계입니다.",
        "- 상한 규칙과 일정의 근거는 docs/methodology.md에 정리되어 있습니다. 지수 제공자의 공지가 있으면 공지가 우선합니다.",
        "- 자금 유출입 추정 배율 k는 보유 주식수의 공통 변화율에서 추정한 값입니다. 발행사 파일의 발행주식수는 설정·환매의 결제 시차 때문에 보유 내역보다 하루 늦게 움직이는 경우가 있어(iShares 파일에서 확인), 같은 날의 k와 발행주식수 변화율이 어긋날 수 있습니다.",
        "- 비중은 소수점 둘째 자리 퍼센트로, 비중 변화는 퍼센트포인트(pp)로, 금액은 미국 달러(M은 백만, B는 십억)로 표기합니다.",
    ]


# --------------------------------------------------------------------------
# Public renderers
# --------------------------------------------------------------------------


def render_markdown(analysis: Mapping[str, Any]) -> str:
    """Render the Korean markdown report for an ``analyze_all`` dictionary.

    A single ``analyze_etf`` dictionary is accepted too.  The output ends
    with a newline and is identical for identical input.
    """
    report = _as_report(analysis)
    today = _today_of(report)
    lines = [f"# ETF 리밸런스 추적 리포트 ({today})", ""]
    lines.append(
        f"생성 시각은 {_utc_text(report.get('generated_utc'))}입니다. 보유 내역은 발행사가 공개한 전 거래일(T-1) 기준이며, ETF별 기준일은 아래 표와 같습니다."
    )
    lines.append("")
    lines.extend(_summary_table(report))
    for etf in _etf_order(report):
        lines.append("")
        lines.extend(_etf_section(etf, report))
    lines.append("")
    lines.extend(_footnotes())
    return "\n".join(lines).rstrip() + "\n"


def _breach_summary(breaches: Sequence[Mapping[str, Any]], lookup: Mapping[str, float]) -> str:
    parts: list[str] = []
    for breach in breaches:
        rule = _text(breach.get("rule"), "")
        label = _BREACH_SHORT.get(rule) or _text(breach.get("description"), rule or "위반")
        tickers = [_text(t) for t in _sequence(breach.get("tickers"))]
        if rule in _AGGREGATE_RULES:
            parts.append(f"{label}(합계 {format_pct(breach.get('value'))}, {len(tickers)}종목)")
            continue
        shown = ", ".join(f"{t} {format_pct(lookup.get(t))}" for t in tickers[:5])
        if len(tickers) > 5:
            shown += f" 외 {len(tickers) - 5}"
        parts.append(f"{label} {len(tickers)}종목({shown})")
    return ", ".join(parts) if parts else "상한 위반 없음"


def _headline_metrics(index_id: str, metrics: Mapping[str, Any], event_type: str) -> str:
    """The capped metrics with their values, for a line without breaches
    (``'최대 회사 비중 8.37%, 4.5% 초과 회사 합계 32.54%'``)."""
    parts: list[str] = []
    for label, key, _cap, _target, _suffix in _limit_specs(index_id, event_type):
        value = _num(metrics.get(key))
        if value is not None and "12월 전용" not in label:
            parts.append(f"{label} {format_pct(value)}")
    return ", ".join(parts)


def _event_summary(entry: Mapping[str, Any]) -> str:
    events = [_mapping(e) for e in _sequence(entry.get("next_events"))]
    events = [e for e in events if e]
    if not events:
        return "다음 이벤트 정보 없음"
    # The nearest reference date still ahead, across all listed events (an
    # IGV reconstitution reference precedes the quarterly pricing reference).
    upcoming: list[tuple[int, str]] = []
    for event in events:
        days = _num(event.get("trading_days_to_reference"))
        if days is not None and days >= 0:
            upcoming.append((int(days), _date_text(event.get("reference_date"))))
    if upcoming:
        days, ref_date = min(upcoming)
        if days == 0:
            return f"참조일 {ref_date} (오늘)"
        return f"다음 참조일 {ref_date} (D-{days}거래일)"
    first = events[0]
    ref_date = _date_text(first.get("reference_date"))
    trade_date = _date_text(first.get("effective_trade_date"))
    trade_days = _num(first.get("trading_days_to_trade"))
    if trade_days is not None:
        return f"참조일 {ref_date} 경과, 매매일 {trade_date} (D-{int(trade_days)}거래일)"
    return f"다음 참조일 {ref_date}"


def _turnover_summary(caps: Mapping[str, Any], tna: float | None) -> str | None:
    """``'예상 회전율 2.93% (약 1.35B USD, 최대 매도 AMD -1.51pp)'`` or the
    no-trade statement; ``None`` when the caps block carries no turnover."""
    turnover = _num(_mapping(caps.get("metrics")).get("turnover"))
    if turnover is None:
        return None
    if turnover <= _EPS:
        return "예상 강제 매매 없음"
    text = f"예상 회전율 {format_pct(turnover)}"
    details: list[str] = []
    if tna is not None:
        details.append(f"약 {format_usd(turnover * tna)}")
    sellers = _forced_pairs(caps.get("forced_sellers"), sellers=True)
    if sellers:
        ticker, delta = sellers[0]
        details.append(f"최대 매도 {ticker} {format_pp(delta)}")
    if details:
        text += f" ({', '.join(details)})"
    return text


def _flow_summary(entry: Mapping[str, Any]) -> str:
    """``'첫 스냅샷'``, or the active-trade count and k of the decomposition."""
    decomposition = _mapping(entry.get("decomposition"))
    if not decomposition:
        return "첫 스냅샷"
    summary = _mapping(decomposition.get("summary"))
    changes = [_mapping(c) for c in _sequence(decomposition.get("changes"))]
    active = [
        c
        for c in changes
        if c
        and _text(c.get("asset_class"), "equity") == "equity"
        and _text(c.get("classification"), "") in _ACTIVE_CLASSES
    ]
    if active:
        gross_trade = _num(summary.get("gross_trade"))
        text = f"실제 매매 {len(active)}종목"
        if gross_trade is not None:
            text += f"(주식수 효과 {format_pp(gross_trade, signed=False)})"
    else:
        text = "실제 매매 없음"
    k = _num(decomposition.get("scale_factor"))
    if k is not None:
        text += f", k={format_ratio(k)}"
    return text


def render_summary_line(analysis: Mapping[str, Any]) -> str:
    """One line per ETF for logs and notifications, joined with newlines.

    Each line carries, in order: the current breaches with the offending
    weights, metrics within 0.25pp of a limit, the QQQ special-rebalance
    verdict when triggered, the expected one-way turnover with the largest
    forced sale, whether the day had real trades (or is the first snapshot)
    with the flow factor k, and the countdown to the next reference date.

    Example: ``'SOXX 2026-10-08: 8% 초과 2종목(AMD 9.51%, INTC 8.63%),
    ADR 합계 비중 9.79% (한도 10.00%, 경계), 예상 회전율 2.93% (약 1.35B USD,
    최대 매도 AMD -1.51pp), 실제 매매 없음, k=0.9915, 다음 참조일 2026-11-30
    (D-35거래일)'``.
    """
    report = _as_report(analysis)
    etfs = _mapping(report.get("etfs"))
    errors = _mapping(report.get("errors"))
    lines: list[str] = []
    for etf in _etf_order(report):
        entry = _mapping(etfs.get(etf))
        if not entry:
            lines.append(f"{etf}: 오류, {_text(errors.get(etf), '원인 미상')}")
            continue
        index_id = _index_id(etf, entry)
        caps = _mapping(entry.get("caps"))
        event_type = _text(caps.get("event_type"), "quarterly_rebalance")
        weights = _equity_weights(entry)
        lookup = dict(_company_weights(entry, index_id, weights))
        lookup.update(weights)
        breaches = [_mapping(b) for b in _sequence(caps.get("breaches"))]
        metrics = _mapping(caps.get("metrics"))
        breach_text = _breach_summary([b for b in breaches if b], lookup)
        if not any(breaches):
            headline = _headline_metrics(index_id, metrics, event_type)
            if headline:
                breach_text += f"({headline})"
        parts = [breach_text]
        parts.extend(_boundary_alerts(index_id, metrics, event_type))
        special = _mapping(caps.get("special_rebalance"))
        if special.get("triggered"):
            parts.append("특별 리밸런스 발동 조건 충족")
        turnover = _turnover_summary(caps, _total_net_assets(entry))
        if turnover:
            parts.append(turnover)
        parts.append(_flow_summary(entry))
        parts.append(_event_summary(entry))
        lines.append(f"{etf} {_date_text(entry.get('as_of'))}: " + ", ".join(parts))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def report_paths(root: str | os.PathLike[str], today: str | date) -> dict[str, Path]:
    """The four report paths under ``<root>/reports`` for ``today``."""
    day = _iso_date(today)
    if day is None:
        raise ValueError(f"today must be an ISO date, got {today!r}")
    base = Path(root) / "reports"
    return {
        "latest_json": base / "latest.json",
        "latest_md": base / "latest.md",
        "daily_md": base / "daily" / f"{day}.md",
        "daily_json": base / "daily" / f"{day}.json",
    }


def _jsonable(obj: Any) -> Any:
    """Dates to ISO strings, tuples/sets to lists, non-finite floats to None."""
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _write_atomic(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_reports(root: str | os.PathLike[str], analysis: Mapping[str, Any]) -> list[Path]:
    """Write ``reports/latest.json``, ``reports/latest.md`` and the dated
    copies ``reports/daily/<today>.md`` / ``.json``.

    The JSON is the analysis dictionary as given (``indent=2``,
    ``ensure_ascii=False``, ``sort_keys=True``), so a clean input round-trips
    through ``json.load``.  Directories are created; writes are atomic.
    Returns the paths in the order latest.json, latest.md, daily md, daily
    json.
    """
    report = _as_report(analysis)
    today = _today_of(report)
    paths = report_paths(root, today)
    markdown = render_markdown(analysis)
    payload = json.dumps(_jsonable(analysis), indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    written = [
        _write_atomic(paths["latest_json"], payload),
        _write_atomic(paths["latest_md"], markdown),
        _write_atomic(paths["daily_md"], markdown),
        _write_atomic(paths["daily_json"], payload),
    ]
    log.info("wrote %d report files for %s under %s", len(written), today, paths["latest_json"].parent)
    return written
