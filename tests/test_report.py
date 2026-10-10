"""Tests for etf_tracker.report: Korean markdown, summary lines and files.

The analysis dictionaries come from ``tests/analysis_fixture.py``, which
runs the real decomposition, capping and calendar code on synthetic
snapshots and assembles the result per the analysis JSON contract.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from etf_tracker.report import (
    _days_text,
    BREACH_LABELS,
    CLASSIFICATION_LABELS,
    ETF_ORDER,
    EVENT_LABELS,
    format_int,
    format_pct,
    format_pp,
    format_ratio,
    format_usd,
    render_markdown,
    render_summary_line,
    report_paths,
    write_reports,
)

try:
    from analysis_fixture import ANALYSIS_KEYS, ETFS, GENERATED_UTC, build_analysis, build_analysis_all
except ImportError:  # tests collected as a package
    from tests.analysis_fixture import ANALYSIS_KEYS, ETFS, GENERATED_UTC, build_analysis, build_analysis_all


@pytest.fixture(scope="module")
def analysis() -> dict:
    return build_analysis_all()


@pytest.fixture(scope="module")
def markdown(analysis) -> str:
    return render_markdown(analysis)


def _section(markdown: str, etf: str) -> str:
    """The markdown between ``## <etf>`` and the next ``## `` heading."""
    start = markdown.index(f"## {etf} (")
    nxt = markdown.find("\n## ", start + 1)
    return markdown[start:] if nxt < 0 else markdown[start:nxt]


def _between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


# ---------------------------------------------------------------------------
# The fixture itself honours the contract
# ---------------------------------------------------------------------------


def test_fixture_matches_the_analysis_contract(analysis):
    assert set(analysis) == {"generated_utc", "today", "etfs", "errors"}
    assert tuple(analysis["etfs"]) == ETFS == ETF_ORDER
    for etf, entry in analysis["etfs"].items():
        assert set(entry) == ANALYSIS_KEYS, etf
        assert entry["etf"] == etf and entry["index_id"] == etf
        assert set(entry["caps"]) >= {
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
        }
        weights = [h["weight"] for h in entry["holdings"]]
        assert weights == sorted(weights, reverse=True)
        assert len(entry["next_events"]) == 3
    # JSON-native with no NaN
    json.dumps(analysis, allow_nan=False)
    assert analysis["etfs"]["QQQ"]["caps"]["special_rebalance"] is not None
    assert analysis["etfs"]["SOXX"]["caps"]["special_rebalance"] is None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (1_230_000_000, "1.23B USD"),
        (-456_000_000, "-456.00M USD"),
        (12_345.6, "12,346 USD"),
        (2_500_000_000_000, "2,500.00B USD"),
        (None, "-"),
        (float("nan"), "-"),
    ],
)
def test_format_usd(value, expected):
    assert format_usd(value) == expected


def test_format_pct_pp_int_ratio():
    assert format_pct(0.095) == "9.50%"
    assert format_pct(0.0862) == "8.62%"
    assert format_pct(None) == "-"
    assert format_pct(float("inf")) == "-"
    assert format_pp(-0.0154) == "-1.54pp"
    assert format_pp(0.0036) == "+0.36pp"
    assert format_pp(0.0036, signed=False) == "0.36pp"
    assert format_pp(0.0) == "0.00pp"
    assert format_pp(-1e-9) == "0.00pp"
    assert format_int(82_700_000) == "82,700,000"
    assert format_int("82,700,000") == "82,700,000"
    assert format_ratio(1.0062) == "1.0062"
    assert format_ratio(True) == "-"


# ---------------------------------------------------------------------------
# Markdown: structure
# ---------------------------------------------------------------------------


def test_markdown_has_header_and_summary_table(markdown):
    assert markdown.startswith("# ETF 리밸런스 추적 리포트 (2026-10-09)\n")
    assert "2026-10-09 14:05:00 UTC" in markdown
    assert "| ETF | 보유 내역 기준일 | 전일 스냅샷 | 발행주식수 | 전일 대비 발행주식수 변화율 | 자금 유출입 추정 배율 k |" in markdown
    assert "|---|---|---|---:|---:|---:|" in markdown  # numeric columns right-aligned
    assert "| SOXX | 2026-10-08 | 2026-10-07 | 82,700,000 | +0.62% | 1.0062 |" in markdown
    assert "| QQQ | 2026-10-08 | 2026-10-07 | 585,200,000 | -0.15% | 0.9985 |" in markdown
    assert "| IGV | 2026-10-08 | 2026-10-07 | 112,400,000 | 0.00% | 1.0000 |" in markdown
    assert markdown.endswith("\n")


def test_markdown_has_one_section_per_etf_in_order(markdown):
    positions = [markdown.index(f"## {etf} (") for etf in ETF_ORDER]
    assert positions == sorted(positions)
    assert markdown.count("\n## SOXX (") == 1
    assert "## SOXX (NYSE Semiconductor Index)" in markdown
    assert "## QQQ (Nasdaq-100 Index)" in markdown
    assert "## IGV (S&P North American Expanded Technology Software Index)" in markdown
    for etf in ETF_ORDER:
        section = _section(markdown, etf)
        for heading in ("### 상한 점검", "### 다음 이벤트", "### 비중 변화 분해", "### 데이터 경고"):
            assert heading in section, (etf, heading)


def test_markdown_footnotes(markdown):
    footer = markdown[markdown.index("## 각주"):]
    assert "docs/methodology.md" in footer
    assert "T-1" in footer
    assert "근사치" in footer
    assert "pp" in footer


def _assert_no_leaked_tokens(text: str) -> None:
    """No em/en dashes and nothing that looks like a Python repr."""
    assert "\u2014" not in text
    assert "\u2013" not in text
    assert "None" not in text
    assert "Decimal" not in text
    assert re.search(r"\bnan\b", text) is None
    assert "-0.00" not in text
    for token in ("[", "]", "{", "}", "_", "'", '"'):
        assert token not in text, token


def test_markdown_has_no_em_dash_or_python_literals(markdown, analysis):
    _assert_no_leaked_tokens(markdown)
    _assert_no_leaked_tokens(render_summary_line(analysis))


# ---------------------------------------------------------------------------
# Markdown: SOXX cap check
# ---------------------------------------------------------------------------


def test_soxx_cap_table_shows_thresholds_and_metrics(analysis, markdown):
    section = _section(markdown, "SOXX")
    metrics = analysis["etfs"]["SOXX"]["caps"]["metrics"]
    assert "| 지표 | 현재값 | 한도 | 여유/초과 |" in section
    assert f"| 최대 종목 비중 | {format_pct(metrics['max_weight'])} | 8.00% | 초과 {format_pp(metrics['max_weight'] - 0.08, signed=False)} |" in section
    assert "| 상위 5 밖 종목 중 4% 초과 개수 | 2종목 | 0종목 (종목별 4.00%) | 초과 2종목 |" in section
    assert f"| ADR 합계 비중 | {format_pct(metrics['adr_sum'])} | 10.00% | 여유 {format_pp(0.10 - metrics['adr_sum'], signed=False)} |" in section
    assert "분기 리밸런스의 규칙을 기준으로" in section
    assert "상위 5개 종목은 오늘의 비중 순위로 정했으며" in section
    # the names behind the ADR aggregate, largest first
    assert "ADR 합계에 포함된 종목은 3개(합계 9.14%)이며 비중 순으로 TSM 5.93%, ARM 2.21%, STM 1.00%입니다." in section
    # fund weights from the holdings table of the decomposition
    assert "| AMD | 9.50% |" in section
    assert "| INTC | 8.62% |" in section


def test_soxx_breach_list_uses_korean_labels(analysis, markdown):
    section = _section(markdown, "SOXX")
    breaches = analysis["etfs"]["SOXX"]["caps"]["breaches"]
    assert [b["rule"] for b in breaches] == ["soxx_single_cap_8", "soxx_outside_top5_cap_4"]
    assert f"- {BREACH_LABELS['soxx_single_cap_8']}: AMD, INTC (최대 {format_pct(breaches[0]['value'])}, 한도 8.00%)" in section
    assert f"- {BREACH_LABELS['soxx_outside_top5_cap_4']}: MU, QCOM (최대 {format_pct(breaches[1]['value'])}, 한도 4.00%)" in section


def test_forced_sellers_and_buyers_appear_in_order_with_amounts(analysis, markdown):
    entry = analysis["etfs"]["SOXX"]
    section = _section(markdown, "SOXX")
    sellers = _between(section, "강제 매도 상위 5", "강제 매수 상위 5")
    buyers = _between(section, "강제 매수 상위 5", "### 다음 이벤트")
    assert "| 종목 | 현재 비중 | 목표 비중 | 변화(pp) | 추정 금액 |" in sellers

    expected_sellers = [t for t, _ in entry["caps"]["forced_sellers"][:5]]
    assert expected_sellers[:2] == ["AMD", "INTC"]
    positions = [sellers.index(f"| {t} |") for t in expected_sellers]
    assert positions == sorted(positions)

    expected_buyers = [t for t, _ in entry["caps"]["forced_buyers"][:5]]
    positions = [buyers.index(f"| {t} |") for t in expected_buyers]
    assert positions == sorted(positions)

    tna = entry["fund"]["total_net_assets"]
    ticker, delta = entry["caps"]["forced_sellers"][0]
    row = next(line for line in sellers.splitlines() if line.startswith(f"| {ticker} |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[3] == format_pp(delta)
    assert cells[4] == format_usd(delta * tna)
    assert cells[2] == "8.00%"  # AMD is cut to the 8% cap
    assert cells[4].endswith("M USD") and cells[4].startswith("-")


def test_forced_tables_omit_amount_without_net_assets(analysis):
    entry = copy.deepcopy(analysis["etfs"]["SOXX"])
    entry["fund"] = {"shares_outstanding": None, "prev_shares_outstanding": None, "nav": None, "total_net_assets": None}
    for holding in entry["holdings"]:
        holding["market_value"] = None
    section = _section(render_markdown(entry), "SOXX")
    sellers = _between(section, "강제 매도 상위 5", "강제 매수 상위 5")
    assert "| 종목 | 현재 비중 | 목표 비중 | 변화(pp) |" in sellers
    assert "추정 금액" not in sellers
    assert "USD" not in sellers


# ---------------------------------------------------------------------------
# Markdown: QQQ and IGV
# ---------------------------------------------------------------------------


def test_qqq_section_thresholds_special_rebalance_and_december_rows(analysis, markdown):
    entry = analysis["etfs"]["QQQ"]
    assert entry["caps"]["event_type"] == "annual_reconstitution"
    section = _section(markdown, "QQQ")
    metrics = entry["caps"]["metrics"]
    # both company-level thresholds are special-rebalance triggers (methodology 2.2)
    assert (
        f"| 최대 회사 비중 | {format_pct(metrics['max_company_weight'])} | 24.00% (조정 목표 20.00%, 특별 리밸런스 발동 기준) | 여유"
        in section
    )
    assert (
        f"| 4.5% 초과 회사 합계 | {format_pct(metrics['sum_over_4_5'])} | 48.00% (조정 목표 40.00%, 특별 리밸런스 발동 기준) | 여유"
        in section
    )
    assert "| 최대 증권 비중 (12월 전용) |" in section and "15.00% (조정 목표 14.00%)" in section
    assert "| 상위 5 증권 합계 (12월 전용) |" in section and "40.00% (조정 목표 38.50%)" in section
    assert "연간 재구성의 규칙을 기준으로" in section
    assert (
        f"특별 리밸런스 트리거: 미발동. 최대 회사 비중 {format_pct(metrics['max_company_weight'])}(기준 24%), "
        f"4.5% 초과 회사 합계 {format_pct(metrics['sum_over_4_5'])}(기준 48%)로 두 기준을 모두 넘지 않았습니다."
    ) in section
    assert "위반 목록:\n\n- 없음" in section
    assert "강제 매도 상위 5 (상한 재적용 시 비중이 줄어드는 종목):\n\n- 없음" in section
    assert "예상 강제 매매가 없습니다" in section


def test_qqq_december_rows_hidden_for_quarterly_rebalance(analysis):
    entry = copy.deepcopy(analysis["etfs"]["QQQ"])
    entry["caps"]["event_type"] = "quarterly_rebalance"
    section = _section(render_markdown(entry), "QQQ")
    assert "12월 전용" not in section
    assert "| 상위 5 증권 합계 |" in section
    assert "분기 리밸런스의 규칙을 기준으로" in section
    assert section.count("특별 리밸런스 발동 기준") == 2  # the 24% and 48% rows stay tagged


def test_qqq_special_rebalance_triggered_wording(analysis):
    entry = copy.deepcopy(analysis["etfs"]["QQQ"])
    entry["caps"]["special_rebalance"] = {
        "triggered": True,
        "reasons": ["companies above 4.5% sum to 49.10%, exceeding 48%"],
        "metrics": {"max_company_weight": 0.12, "sum_over_4_5": 0.491},
    }
    section = _section(render_markdown(entry), "QQQ")
    assert (
        "특별 리밸런스 트리거: 발동 조건 충족. 최대 회사 비중 12.00%(기준 24%), 4.5% 초과 회사 합계 49.10%(기준 48%)입니다. "
        "사유: 4.5% 초과 회사의 합계 49.10%가 48%를 초과. Nasdaq이 특별 리밸런스를 공지하면 참조일과 효력일이 별도로 정해집니다."
    ) in section
    assert "exceeding" not in section and "sum to" not in section


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("company NVDA weight 26.1% exceeds 24%", "회사 NVDA의 비중 26.1%가 24%를 초과"),
        (
            "companies above 4.5% (NVDA, AAPL, MSFT) sum to 49.1%, exceeding 48%",
            "4.5% 초과 회사(NVDA, AAPL, MSFT)의 합계 49.1%가 48%를 초과",
        ),
        ("companies above 4.5% sum to 48.2%, 48% or more", "4.5% 초과 회사의 합계 48.2%가 48%를 초과"),
        ("some reason the renderer has never seen", "some reason the renderer has never seen"),
    ],
)
def test_special_rebalance_reasons_are_localized(analysis, reason, expected):
    """The exact strings ``QQQRules.special_rebalance_triggered`` emits read as Korean."""
    entry = copy.deepcopy(analysis["etfs"]["QQQ"])
    entry["caps"]["special_rebalance"] = {"triggered": True, "reasons": [reason], "metrics": {}}
    section = _section(render_markdown(entry), "QQQ")
    assert f"사유: {expected}." in section
    assert "최대 회사 비중 -(기준 24%)" in section  # missing metrics render as '-'


def test_igv_section_thresholds_and_breaches(analysis, markdown):
    entry = analysis["etfs"]["IGV"]
    section = _section(markdown, "IGV")
    metrics = entry["caps"]["metrics"]
    # IGV caps are company-level (8.5% per company, >4.5% cohort), so the
    # metric labels say 회사 like the breach labels do.
    assert f"| 최대 회사 비중 | {format_pct(metrics['max_weight'])} | 8.50% | 초과 {format_pp(metrics['max_weight'] - 0.085, signed=False)} |" in section
    assert f"| 4.5% 초과 회사 합계 | {format_pct(metrics['sum_over_4_5'])} | 45.00% | 초과 {format_pp(metrics['sum_over_4_5'] - 0.45, signed=False)} |" in section
    assert "| 4.5% 초과 회사 수 | 7개 회사 | - | - |" in section
    cap_table = _between(section, "| 지표 |", "위반 목록:")
    assert "종목 비중" not in cap_table and "종목 합계" not in cap_table
    assert "ADR 합계에 포함된 종목" not in section  # no ADR rule for IGV
    rules = [b["rule"] for b in entry["caps"]["breaches"]]
    assert rules == ["igv_single_cap_8_5", "igv_cohort_45"]
    assert f"- {BREACH_LABELS['igv_single_cap_8_5']}: MSFT (최대" in section
    assert f"- {BREACH_LABELS['igv_cohort_45']}: MSFT, ORCL, CRM, NOW, ADBE, INTU, PLTR (합계" in section
    sellers = _between(section, "강제 매도 상위 5", "강제 매수 상위 5")
    assert sellers.index("| MSFT |") < sellers.index("| PLTR |")


def test_forced_tables_are_sorted_regardless_of_input_order(analysis):
    """Sellers most negative first and buyers largest first even if the
    producer's lists arrive scrambled; the heading shows the full count."""
    entry = copy.deepcopy(analysis["etfs"]["SOXX"])
    caps = entry["caps"]
    assert len(caps["forced_sellers"]) <= 5 < len(caps["forced_buyers"])
    caps["forced_sellers"] = list(reversed(caps["forced_sellers"]))
    caps["forced_buyers"] = sorted(caps["forced_buyers"], key=lambda pair: pair[0])
    section = _section(render_markdown(entry), "SOXX")
    sellers = _between(section, "강제 매도 상위 5", "강제 매수 상위 5")
    buyers = _between(section, "강제 매수 상위 5", "### 다음 이벤트")

    def deltas(table: str) -> list[float]:
        out = []
        for line in table.splitlines():
            if line.startswith("| ") and "변화(pp)" not in line:
                cells = [c.strip() for c in line.strip("|").split("|")]
                out.append(float(cells[3].replace("pp", "")))
        return out

    seller_deltas = deltas(sellers)
    buyer_deltas = deltas(buyers)
    assert seller_deltas and seller_deltas == sorted(seller_deltas)
    assert len(buyer_deltas) == 5 and buyer_deltas == sorted(buyer_deltas, reverse=True)
    expected_buyers = [t for t, _ in sorted(analysis["etfs"]["SOXX"]["caps"]["forced_buyers"], key=lambda p: -p[1])[:5]]
    assert [line.split("|")[1].strip() for line in buyers.splitlines() if line.startswith("| ") and "변화(pp)" not in line] == expected_buyers
    # headings: the total only when more rows exist than are shown
    assert "강제 매도 상위 5 (상한 재적용 시 비중이 줄어드는 종목):" in section
    assert f"강제 매수 상위 5 (총 {len(caps['forced_buyers'])}종목, 재분배를 받아 비중이 늘어나는 종목):" in section


def test_forced_tables_align_numbers_right(markdown):
    soxx = _section(markdown, "SOXX")
    sellers = _between(soxx, "강제 매도 상위 5", "강제 매수 상위 5")
    assert "|---|---:|---:|---:|---:|" in sellers
    cap_table = _between(soxx, "| 지표 |", "위반 목록:")
    assert "|---|---:|---|---|" in cap_table
    decomposition = _between(soxx, "| 종목 | 비중 |", "일방향 합계")
    assert "|---|---:|---:|---:|---:|---:|---|" in decomposition
    events = _between(soxx, "| 종류 |", "참조일 종가 비중이")
    assert "|---|---|---|---|---|---:|---:|" in events


def test_adr_constituents_listed_only_for_soxx(analysis, markdown):
    soxx = _section(markdown, "SOXX")
    adr_line = next(line for line in soxx.splitlines() if line.startswith("ADR 합계에 포함된 종목은"))
    # between the cap table and the breach list, largest ADR first, sum equals the metric
    assert soxx.index("| 상위 5 종목 합계 |") < soxx.index(adr_line) < soxx.index("위반 목록:")
    weights = [float(m) for m in re.findall(r" (\d+\.\d+)%", adr_line.split("비중 순으로")[1])]
    assert weights == sorted(weights, reverse=True) and len(weights) == 3
    assert f"(합계 {format_pct(analysis['etfs']['SOXX']['caps']['metrics']['adr_sum'])})" in adr_line
    for etf in ("QQQ", "IGV"):
        assert "ADR 합계에 포함된 종목" not in _section(markdown, etf)
    no_adr = copy.deepcopy(analysis["etfs"]["SOXX"])
    for holding in no_adr["holdings"]:
        holding["is_adr"] = False
    assert "ADR 합계에 포함된 종목" not in render_markdown(no_adr)


def test_qqq_cap_table_has_no_brackets(markdown):
    qqq = _section(markdown, "QQQ")
    assert "[" not in qqq and "]" not in qqq
    assert qqq.count("특별 리밸런스 발동 기준") == 2


# ---------------------------------------------------------------------------
# Markdown: next events
# ---------------------------------------------------------------------------


def test_next_events_tables(markdown):
    header = "| 종류 | 참조일 | 발표일 | 매매일(종가) | 효력일 | 참조일까지 거래일 | 매매일까지 거래일 |"
    soxx = _section(markdown, "SOXX")
    assert header in soxx
    assert "| 분기 리밸런스 | 2026-11-30 | 2026-12-04 | 2026-12-18 | 2026-12-21 | 35 | 49 |" in soxx
    qqq = _section(markdown, "QQQ")
    assert "| 연간 재구성 | 2026-11-30 | 2026-12-11 | 2026-12-18 | 2026-12-21 | 35 | 49 |" in qqq
    igv = _section(markdown, "IGV")
    assert "| 반기 재구성 | 2026-11-30 | 미공표 | 2026-12-18 | 2026-12-21 | 35 | 49 |" in igv
    assert "| 분기 리밸런스 | 2026-12-10 | 미공표 | 2026-12-18 | 2026-12-21 | 43 | 49 |" in igv
    for label in EVENT_LABELS.values():
        assert "_" not in label


def test_passed_reference_date_is_marked(analysis):
    entry = copy.deepcopy(analysis["etfs"]["SOXX"])
    entry["next_events"][0]["trading_days_to_reference"] = -3
    section = _section(render_markdown(entry), "SOXX")
    assert "| -3 (경과) | 49 |" in section


# ---------------------------------------------------------------------------
# Markdown: decomposition
# ---------------------------------------------------------------------------


def test_decomposition_table_and_active_trades(analysis, markdown):
    soxx = _section(markdown, "SOXX")
    assert "### 비중 변화 분해 (2026-10-07 -> 2026-10-08)" in soxx
    assert "| 종목 | 비중 | 전일 비중 | 변화(pp) | 가격 효과 drift(pp) | 주식수 효과 trade(pp) | 분류 |" in soxx
    table = _between(soxx, "| 종목 | 비중 |", "일방향 합계")
    rows = [line for line in table.splitlines() if line.startswith("| ") and "분류" not in line]
    assert len(rows) == 10
    # sorted by |trade| desc: the two active trades lead
    assert rows[0].startswith("| AMD |") and "실제 매매" in rows[0]
    assert rows[1].startswith("| TXN |") and "실제 매매" in rows[1]
    assert "- AMD: 실제 매매, 주식수 " in soxx and "자금 흐름 조정 후 주식수 변화 +2.00%" in soxx
    assert "- TXN: 실제 매매, 주식수 " in soxx and "자금 흐름 조정 후 주식수 변화 -1.50%" in soxx
    assert re.search(r"- AMD: 실제 매매, 주식수 \d{1,3}(,\d{3})+ -> \d{1,3}(,\d{3})+", soxx)
    # the share counts are the raw issuer figures, not the flow-adjusted ones
    amd = next(c for c in analysis["etfs"]["SOXX"]["decomposition"]["changes"] if c["ticker"] == "AMD")
    assert f"- AMD: 실제 매매, 주식수 {format_int(amd['shares_prev'])} -> {format_int(amd['shares_curr'])}" in soxx
    assert format_int(amd["shares_prev_adjusted"]) not in soxx
    implied = analysis["etfs"]["SOXX"]["decomposition"]["summary"]["fund_flow"]["implied_flow_usd_from_scale_factor"]
    assert implied > 0
    assert f"k=1.0062이므로 전일 대비 약 0.62%({format_usd(implied)} 상당)의 설정(자금 유입)이 있었던 것으로 추정됩니다." in soxx

    qqq = _section(markdown, "QQQ")
    assert "- NFLX: 기업행동 의심" in qqq and "추정 분할 비율 10:1" in qqq
    nflx = next(c for c in analysis["etfs"]["QQQ"]["decomposition"]["changes"] if c["ticker"] == "NFLX")
    assert f"주식수 {format_int(nflx['shares_prev'])} -> {format_int(nflx['shares_curr'])}" in qqq  # the 10x jump is visible
    assert "- PLTR: 실제 매매" in qqq
    implied = analysis["etfs"]["QQQ"]["decomposition"]["summary"]["fund_flow"]["implied_flow_usd_from_scale_factor"]
    assert implied < 0
    assert f"k=0.9985이므로 전일 대비 약 0.15%({format_usd(-implied)} 상당)의 환매(자금 유출)" in qqq

    igv = _section(markdown, "IGV")
    assert "- GTLB: 신규 편입, 주식수 0 -> " in igv and "비중 0.60%로 신규 편입" in igv
    assert "- DOCU: 제외, 주식수 1,310,000 -> 0" in igv
    assert "- CRWD: 실제 매매" in igv and "-3.00%" in igv
    assert "k=1.0000이므로 전일 대비 순설정이나 순환매가 거의 없었던 것으로 추정됩니다." in igv
    for label in CLASSIFICATION_LABELS.values():
        assert "_" not in label


def test_decomposition_table_orders_quiet_days_by_weight_change(analysis):
    """Without real trades the share-count residuals are rounding noise, so
    the table ranks by the weight change rather than by that noise."""
    entry = copy.deepcopy(analysis["etfs"]["SOXX"])
    changes = entry["decomposition"]["changes"]
    for index, change in enumerate(changes):
        change["classification"] = "flow_only"
        change["trade"] = (index + 1) * 1e-7  # below 0.005pp: prints as 0.00pp
    section = _section(render_markdown(entry), "SOXX")
    table = _between(section, "| 종목 | 비중 |", "일방향 합계")
    rows = [line for line in table.splitlines() if line.startswith("| ") and "분류" not in line]
    tickers = [row.split("|")[1].strip() for row in rows]
    by_total = sorted(
        (c for c in changes if c.get("asset_class", "equity") == "equity"),
        key=lambda c: (-abs(c["total_change"]), c["ticker"]),
    )
    assert tickers == [c["ticker"] for c in by_total[:10]]
    assert all("0.00pp | 자금 흐름만" in row for row in rows)
    assert "실제 매매로 분류된 종목 (실제 매매, 신규 편입, 제외, 기업행동 의심):\n\n- 없음" in section
    assert "같으면 비중 변화의 절대값이 큰 순서로" in section
    assert "현금과 파생상품을 포함한 펀드 비중이라 상한 점검표의 주식 기준 비중보다 조금 작습니다" in section


def test_k_sentence_without_fund_flow_summary(analysis):
    entry = copy.deepcopy(analysis["etfs"]["SOXX"])
    del entry["decomposition"]["summary"]["fund_flow"]
    section = _section(render_markdown(entry), "SOXX")
    assert "k=1.0062이므로 전일 대비 약 0.62%의 설정(자금 유입)이 있었던 것으로 추정됩니다." in section
    assert "상당" not in section


def test_decomposition_null_wording_on_first_day():
    first_day = build_analysis_all(with_prev=False)
    for entry in first_day["etfs"].values():
        assert entry["decomposition"] is None and entry["prev_as_of"] is None
    text = render_markdown(first_day)
    assert text.count("기준 첫 스냅샷을 저장했습니다. 전일 스냅샷이 없어 비중 변화 분해는 다음 거래일 스냅샷부터 시작됩니다.") == 3
    assert "### 비중 변화 분해 (" not in text
    assert "주식수 효과 trade(pp)" not in text
    assert "| SOXX | 2026-10-08 | - | 82,700,000 | - | - |" in text


# ---------------------------------------------------------------------------
# Markdown: warnings and errors
# ---------------------------------------------------------------------------


def test_warnings_rendered_as_bullets_or_none(markdown):
    soxx = _section(markdown, "SOXX")
    assert soxx[soxx.index("### 데이터 경고"):].startswith("### 데이터 경고\n\n- 없음")
    qqq = _section(markdown, "QQQ")
    assert "- NFLX: share ratio ~10x with inverse price move" in qqq


def test_known_english_warnings_are_rendered_in_korean():
    entry = build_analysis("SOXX")
    entry["warnings"] = [
        "no previous snapshot: drift/trade decomposition skipped",
        "snapshot as of 2026-10-08 is 1 trading day(s) older than the last completed session 2026-10-09",
        "ADR flag inferred from listing location for: TSM, TSEM",
        "ADR flag overridden to False by configuration for: TSEM",
        "USD: derived price 0.999889 from market_value/shares",
        "decomposition: SWKS: exited; previous price used for its counterfactual value",
        "special rebalance trigger breached: AAPL company weight 26.10% > 24%",
        "caps infeasible under annual_reconstitution: 1.00% could not be placed",
        "something the renderer has never seen",
    ]
    text = _section(render_markdown(entry), "SOXX")
    assert "- 전일 스냅샷이 없어 비중 변화 분해를 건너뛰었습니다." in text
    assert "- 보유 내역 기준일 2026-10-08: 마지막 완료 거래일 2026-10-09보다 1거래일 오래된 데이터입니다." in text
    assert "- ADR 추정 종목: TSM, TSEM. 이름에 ADR 표기는 없지만" in text
    assert "- ADR 제외 설정 종목: TSEM. 미국 밖 상장이지만 ADR이 아닌 보통주로" in text
    assert "- USD: 가격이 없어 시가/주식수로 0.999889를 계산해 사용했습니다." in text
    assert "- 비중 변화 분해: SWKS 제외 종목의 반사실 비중은 전일 가격으로 계산했습니다." in text
    assert "- 특별 리밸런스 발동 조건 충족: AAPL company weight 26.10% > 24%" in text
    assert "- 상한 재적용 미수렴 (연간 재구성): 1.00% could not be placed" in text
    assert "- something the renderer has never seen" in text
    assert "drift/trade decomposition skipped" not in text
    assert "ADR flag inferred" not in text


def test_binding_constraints_are_rendered_in_korean():
    entry = build_analysis("SOXX")
    entry["caps"]["binding_constraints"] = [
        "SOXX 8% single-name cap: AMD, INTC",
        "SOXX 10% aggregate ADR cap: ARM, TSM",
        "IGV aggregate 45% rule (companies >4.5%): reduced NOW",
        "unknown constraint text",
    ]
    text = _section(render_markdown(entry), "SOXX")
    assert (
        "- 구속 제약: SOXX 단일 종목 8% 상한: AMD, INTC; SOXX ADR 합계 10% 상한: ARM, TSM; "
        "IGV 4.5% 초과 회사 합계 45% 규칙으로 축소: NOW; unknown constraint text"
    ) in text


def test_event_rows_are_ordered_by_reference_date(markdown):
    igv = _section(markdown, "IGV")
    assert igv.index("| 반기 재구성 | 2026-11-30 |") < igv.index("| 분기 리밸런스 | 2026-12-10 |")


def test_shares_outstanding_lag_is_explained(markdown):
    soxx = _section(markdown, "SOXX")
    assert "발행사 파일의 발행주식수 변화율은" in soxx
    assert "결제 시차" in markdown[markdown.index("## 각주"):]


def test_failed_etf_is_reported_in_table_and_section():
    failed = build_analysis_all(errors={"QQQ": "HTTP 406 from dng-api.invesco.com"})
    assert "QQQ" not in failed["etfs"]
    text = render_markdown(failed)
    assert "| QQQ | 오류 | - | - | - | - |" in text
    assert "QQQ (HTTP 406 from dng-api.invesco.com)" in text
    section = _section(text, "QQQ")
    assert section.startswith("## QQQ (Nasdaq-100 Index)")
    assert "이 ETF의 분석을 수행하지 못했습니다. 원인: HTTP 406 from dng-api.invesco.com" in section
    positions = [text.index(f"## {etf} (") for etf in ETF_ORDER]
    assert positions == sorted(positions)


def test_single_etf_dict_is_accepted(analysis):
    entry = analysis["etfs"]["IGV"]
    text = render_markdown(entry)
    assert text.startswith("# ETF 리밸런스 추적 리포트 (2026-10-08)\n")
    assert "## IGV (" in text and "## SOXX (" not in text
    assert "알 수 없음" in text  # no generated_utc on a single entry


def test_sparse_and_hostile_input_renders_without_raising():
    sparse = {"generated_utc": None, "today": "../../etc", "etfs": {"SOXX": {"etf": "SOXX"}}, "errors": {}}
    text = render_markdown(sparse)
    assert "## SOXX (NYSE Semiconductor Index)" in text
    assert "../../etc" not in text
    hostile = build_analysis("SOXX")
    hostile["holdings"][0]["name"] = "AMD | rm -rf\nline2"
    hostile["warnings"] = ["pipe | in | warning\x00with control"]
    hostile["caps"]["forced_sellers"][0][0] = "AMD|X"
    hostile["caps"]["breaches"].append({"rule": "unknown_rule", "description": "desc", "tickers": None, "value": "x", "limit": None})
    text = render_markdown(hostile)
    assert "| AMD\\|X |" in text
    assert "\x00" not in text
    assert "- desc: - (최대 -, 한도 -)" in text


def test_markdown_is_deterministic_and_key_order_independent(analysis, markdown):
    assert render_markdown(analysis) == markdown
    reordered = json.loads(json.dumps(analysis, sort_keys=True))
    assert render_markdown(reordered) == markdown


# ---------------------------------------------------------------------------
# Summary line
# ---------------------------------------------------------------------------


def test_summary_line_one_line_per_etf(analysis):
    lines = render_summary_line(analysis).splitlines()
    assert len(lines) == 3
    soxx, qqq, igv = lines
    soxx_entry = analysis["etfs"]["SOXX"]
    metrics = soxx_entry["caps"]["metrics"]
    tna = soxx_entry["fund"]["total_net_assets"]
    assert soxx.startswith(f"SOXX 2026-10-08: 8% 초과 2종목(AMD {format_pct(metrics['max_weight'])}, INTC ")
    assert "상위 5 밖 4% 초과 2종목(MU " in soxx
    # turnover with the dollar amount and the largest forced sale, then the day's trades and k
    seller, delta = soxx_entry["caps"]["forced_sellers"][0]
    assert seller == "AMD"
    assert (
        f", 예상 회전율 {format_pct(metrics['turnover'])} (약 {format_usd(metrics['turnover'] * tna)}, 최대 매도 AMD {format_pp(delta)}), "
        "실제 매매 2종목(주식수 효과 0.17pp), k=1.0062, 다음 참조일 2026-11-30 (D-35거래일)"
    ) in soxx
    assert soxx.endswith(", 다음 참조일 2026-11-30 (D-35거래일)")
    assert "경계" not in soxx  # ADR sum 9.14% is 0.86pp below the cap

    qqq_metrics = analysis["etfs"]["QQQ"]["caps"]["metrics"]
    assert qqq == (
        f"QQQ 2026-10-08: 상한 위반 없음(최대 회사 비중 {format_pct(qqq_metrics['max_company_weight'])}, "
        f"4.5% 초과 회사 합계 {format_pct(qqq_metrics['sum_over_4_5'])}), 예상 강제 매매 없음, "
        "실제 매매 2종목(주식수 효과 0.03pp), k=0.9985, 다음 참조일 2026-11-30 (D-35거래일)"
    )

    assert igv.startswith("IGV 2026-10-08: 8.5% 초과 1종목(MSFT ")
    assert "4.5% 초과 회사 합계 45% 초과(합계 " in igv and ", 7종목)" in igv
    assert "최대 매도 MSFT -0.43pp)" in igv
    assert "실제 매매 3종목(주식수 효과 0.91pp), k=1.0000" in igv  # GTLB entry, DOCU exit, CRWD trade
    # nearest upcoming reference across the listed events (semi-annual before pricing)
    assert igv.endswith(", 다음 참조일 2026-11-30 (D-35거래일)")
    for line in lines:
        assert "\n" not in line and "—" not in line


def test_summary_line_special_rebalance_and_passed_reference(analysis):
    entry = copy.deepcopy(analysis["etfs"]["QQQ"])
    entry["caps"]["special_rebalance"]["triggered"] = True
    for event in entry["next_events"]:
        event["trading_days_to_reference"] = -2
    line = render_summary_line(entry)
    metrics = entry["caps"]["metrics"]
    assert line == (
        f"QQQ 2026-10-08: 상한 위반 없음(최대 회사 비중 {format_pct(metrics['max_company_weight'])}, "
        f"4.5% 초과 회사 합계 {format_pct(metrics['sum_over_4_5'])}), 특별 리밸런스 발동 조건 충족, 예상 강제 매매 없음, "
        "실제 매매 2종목(주식수 효과 0.03pp), k=0.9985, 참조일 2026-11-30 경과, 매매일 2026-12-18 (D-49거래일)"
    )


def test_summary_line_flags_metrics_at_the_boundary(analysis):
    entry = copy.deepcopy(analysis["etfs"]["SOXX"])
    entry["caps"]["metrics"]["adr_sum"] = 0.0979
    line = render_summary_line(entry)
    assert ", ADR 합계 비중 9.79% (한도 10.00%, 경계), 예상 회전율 " in line
    entry["caps"]["metrics"]["adr_sum"] = 0.1001  # over the cap: a breach, not a boundary note
    assert "경계" not in render_summary_line(entry)
    entry["caps"]["metrics"]["adr_sum"] = 0.08
    assert "경계" not in render_summary_line(entry)


def test_summary_line_first_snapshot_and_quiet_day():
    first_day = build_analysis_all(with_prev=False)
    for line in render_summary_line(first_day).splitlines():
        assert ", 첫 스냅샷, " in line and "k=" not in line and "실제 매매" not in line
    quiet = build_analysis("SOXX")
    for change in quiet["decomposition"]["changes"]:
        change["classification"] = "flow_only"
    assert ", 실제 매매 없음, k=1.0062, " in render_summary_line(quiet)


def test_summary_line_reference_date_today(analysis):
    entry = copy.deepcopy(analysis["etfs"]["SOXX"])
    entry["next_events"][0]["trading_days_to_reference"] = 0
    assert render_summary_line(entry).endswith(", 참조일 2026-11-30 (오늘)")
    assert "| 0 (오늘) | 49 |" in _section(render_markdown(entry), "SOXX")


def test_summary_line_resolves_company_level_breach_tickers(analysis):
    entry = copy.deepcopy(analysis["etfs"]["QQQ"])
    entry["caps"]["breaches"] = [
        {"rule": "qqq_company_24", "description": "...", "tickers": ["ALPHABET"], "value": 0.25, "limit": 0.24}
    ]
    line = render_summary_line(entry)
    googl = next(h["weight"] for h in entry["holdings"] if h["ticker"] == "GOOGL")
    goog = next(h["weight"] for h in entry["holdings"] if h["ticker"] == "GOOG")
    equity_total = sum(h["weight"] for h in entry["holdings"] if h["asset_class"] == "equity")
    expected = format_pct((googl + goog) / equity_total)
    assert f"회사 24% 초과 1종목(ALPHABET {expected})" in line


def test_summary_line_reports_errors():
    failed = build_analysis_all(errors={"IGV": "empty template"})
    lines = render_summary_line(failed).splitlines()
    assert lines[-1] == "IGV: 오류, empty template"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_write_reports_creates_four_files_and_round_trips(tmp_path, analysis, markdown):
    paths = write_reports(tmp_path, analysis)
    expected = report_paths(tmp_path, "2026-10-09")
    assert paths == [expected["latest_json"], expected["latest_md"], expected["daily_md"], expected["daily_json"]]
    assert [p.relative_to(tmp_path).as_posix() for p in paths] == [
        "reports/latest.json",
        "reports/latest.md",
        "reports/daily/2026-10-09.md",
        "reports/daily/2026-10-09.json",
    ]
    for path in paths:
        assert path.is_file()
    assert not list((tmp_path / "reports").glob("**/*.tmp"))

    with open(paths[0], encoding="utf-8") as fh:
        assert json.load(fh) == analysis
    latest_json = paths[0].read_text(encoding="utf-8")
    assert latest_json == paths[3].read_text(encoding="utf-8")
    assert latest_json == json.dumps(analysis, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    assert "\\u" not in latest_json  # ensure_ascii=False keeps Korean readable
    assert paths[1].read_text(encoding="utf-8") == markdown
    assert paths[2].read_text(encoding="utf-8") == markdown

    # deterministic: a second run rewrites identical bytes
    before = [p.read_bytes() for p in paths]
    assert write_reports(tmp_path, analysis) == paths
    assert [p.read_bytes() for p in paths] == before


def test_write_reports_sanitises_today_for_file_names(tmp_path, analysis):
    tampered = copy.deepcopy(analysis)
    tampered["today"] = "../../2026-10-09"
    paths = write_reports(tmp_path, tampered)
    assert paths[2] == tmp_path / "reports" / "daily" / "2026-10-08.md"  # falls back to the latest as_of
    assert all(tmp_path in p.parents for p in paths)


def test_write_reports_accepts_single_etf_dict(tmp_path, analysis):
    entry = analysis["etfs"]["SOXX"]
    paths = write_reports(tmp_path, entry)
    assert paths[2].name == "2026-10-08.md"
    with open(paths[0], encoding="utf-8") as fh:
        assert json.load(fh) == entry


def test_report_paths_rejects_non_dates(tmp_path):
    with pytest.raises(ValueError):
        report_paths(tmp_path, "latest")


# ---------------------------------------------------------------------------
# The committed report from live data (skipped when the repository has none)
# ---------------------------------------------------------------------------


_LATEST_JSON = Path(__file__).resolve().parents[1] / "reports" / "latest.json"


@pytest.fixture(scope="module")
def latest_analysis() -> dict:
    if not _LATEST_JSON.is_file():
        pytest.skip("reports/latest.json is not present")
    with open(_LATEST_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def test_latest_report_numbers_trace_to_latest_json(latest_analysis):
    """Every forced-trade row of the real report equals the JSON it was made
    from, the per-index thresholds are the documented ones and nothing
    leaks out of the renderer."""
    markdown = render_markdown(latest_analysis)
    _assert_no_leaked_tokens(markdown)
    _assert_no_leaked_tokens(render_summary_line(latest_analysis))
    thresholds = {
        "SOXX": ("| 8.00% |", "| 10.00% |", "(종목별 4.00%)"),
        "QQQ": ("24.00% (조정 목표 20.00%, 특별 리밸런스 발동 기준)", "48.00% (조정 목표 40.00%, 특별 리밸런스 발동 기준)"),
        "IGV": ("| 8.50% |", "| 45.00% |"),
    }
    for etf, entry in latest_analysis["etfs"].items():
        section = _section(markdown, etf)
        for needle in thresholds.get(etf, ()):
            assert needle in section, (etf, needle)
        if etf == "QQQ" and entry["caps"]["event_type"] == "annual_reconstitution":
            assert "15.00% (조정 목표 14.00%)" in section and "40.00% (조정 목표 38.50%)" in section
        tna = entry["fund"]["total_net_assets"]
        for key, start, end in (
            ("forced_sellers", "강제 매도 상위", "강제 매수 상위"),
            ("forced_buyers", "강제 매수 상위", "### 다음 이벤트"),
        ):
            table = _between(section, start, end)
            pairs = entry["caps"][key]
            if not pairs:
                assert "- 없음" in table
                continue
            rows = [line for line in table.splitlines() if line.startswith("| ") and "변화(pp)" not in line]
            assert len(rows) == min(5, len(pairs))
            ordered = sorted(pairs, key=lambda p: p[1] if key == "forced_sellers" else -p[1])
            for row, (ticker, delta) in zip(rows, ordered):
                cells = [c.strip() for c in row.strip("|").split("|")]
                assert cells[0] == ticker
                assert cells[3] == format_pp(delta)
                if tna is not None:
                    assert cells[4] == format_usd(delta * tna)
            if len(pairs) > 5:
                assert f"(총 {len(pairs)}종목," in section
        for breach in entry["caps"]["breaches"]:
            assert f"한도 {format_pct(breach['limit'])})" in section
        for event in entry["next_events"]:
            assert f"| {event['reference_date']} |" in section
            # between the reference and the trade date the counts render as '0 (오늘)' / '-N (경과)'
            assert f"| {_days_text(event['trading_days_to_reference'])} | {_days_text(event['trading_days_to_trade'])} |" in section
        if entry["decomposition"] is None:
            assert f"{entry['as_of']} 기준 첫 스냅샷을 저장했습니다." in section
        else:
            assert f"k={format_ratio(entry['decomposition']['scale_factor'])}이므로" in section
