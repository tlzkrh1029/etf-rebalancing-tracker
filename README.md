# etf-rebalancing-tracker

미국 ETF 세 종목(SOXX, QQQ, IGV)의 보유 내역을 매일 내려받아 저장하고, 종목별 비중 변화를 **가격 변동 효과(price drift)** 와 **주식수 변화 효과(share-count change)** 로 분해하며, 각 종목이 추적 지수의 **상한(cap) 규칙** 에서 얼마나 떨어져 있는지를 계산해 한국어 리포트로 쓰는 도구입니다. 목표는 지수 리밸런스 시점에 발생하는 **강제 매수·매도(forced buying / selling)** 를 미리 가늠하는 것입니다.

| ETF | 운용사 | 추적 지수 | 지수 제공자 | 종목 수 | 데이터 소스 |
|---|---|---|---|---|---|
| SOXX | iShares (BlackRock) | NYSE Semiconductor Index (구 ICE Semiconductor Index) | ICE Data Indices | 30 | iShares 보유 내역 CSV (과거 날짜 조회 가능) |
| QQQ | Invesco | Nasdaq-100 Index (NDX) | Nasdaq | 약 100개 회사 / 약 101개 증권 (Alphabet은 GOOGL, GOOG 두 종류) | Invesco dng-api JSON (최신 날짜만) |
| IGV | iShares (BlackRock) | S&P North American Expanded Technology Software Index | S&P Dow Jones Indices | 가변 (현재 106) | iShares 보유 내역 CSV (과거 날짜 조회 가능) |

## 1. 목적

지수 추종 ETF의 비중은 리밸런스 사이에 가격에 따라 자연스럽게 움직입니다(drift). 반면 운용사가 실제로 주식을 사고팔면 주식수가 바뀝니다. 두 효과를 분리하면 다음을 알 수 있습니다.

- 어떤 종목의 비중 증가가 단순한 주가 상승인지, 자금 유입·리밸런스에 따른 실제 매수인지
- 다음 리밸런스 참조일 기준으로 어떤 종목이 상한(예: QQQ 회사 단위 24%/20%, 4.5%/48%, SOXX 8%/4%/ADR 10%, IGV 8.5%/45%)에 걸려 **강제 매도** 되고, 그 비중이 어떤 종목으로 **재분배** 되어 강제 매수가 생기는지
- 그 이벤트가 며칠 뒤인지(참조일, 발표일, 매매일, 효력일)

규칙과 일정의 근거는 `docs/methodology.md`, 출처 URL은 `docs/sources.md`에 정리되어 있습니다.

## 2. 아키텍처

```
발행사 API (iShares CSV/JSON, Invesco JSON)
        |  etf_tracker/sources/ishares.py, sources/invesco.py  (http.py: 중립 User-Agent)
        v
   FetchResult ---> store.py ---> data/raw/<ETF>/<날짜>.*            (원본 바이트 그대로)
                              `-> data/normalized/<ETF>/<날짜>.json  (Snapshot)
                              `-> data/manifest.json                  (ETF별 최신 상태)
        |
        v
   analysis.py  (decompose + rules/bridge + market_calendar)  ---> 분석 JSON (ETF별 고정 스키마)
        |
        v
   report.py  ---> reports/latest.md, reports/latest.json, reports/daily/<날짜>.{md,json}

   pipeline.py / cli.py: 위 과정을 `python3 -m etf_tracker run` 한 번으로 실행
   .github/workflows/daily.yml: 평일 14:10 UTC와 18:10 UTC에 실행하고 data/, reports/를 커밋
```

### 2.1 데이터 계층 (`etf_tracker/sources/`, `http.py`, `store.py`, 구현 완료)

2026-10-09 이 저장소의 작업 컨테이너(GitHub Actions 러너와 같은 네트워크 조건)에서 세 ETF 모두 실제로 내려받아 검증했습니다. 초기 점검(`probe/summary.txt`)에서 막혔던 것은 상품 페이지 경로와 브라우저 User-Agent였고, 아래 API 엔드포인트와 중립 User-Agent 조합은 정상 동작합니다.

#### 엔드포인트

| 소스 | 용도 | URL (요지) | 비고 |
|---|---|---|---|
| iShares CSV (1차) | SOXX(portfolioId 239705), IGV(239771) 보유 내역 | `https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?...&component=holdings&portfolioId=<ID>[&asOfDate=YYYYMMDD]` | `asOfDate`를 생략하면 최신, 과거 거래일을 주면 그 날짜(수년치 조회 가능). 비거래일·미공개 날짜는 HTTP 200에 빈 템플릿(as-of `-`, 행 0개)이 오며 `no_data`로 처리 |
| iShares JSON (2차) | CSV 실패 시 대체. 정밀도가 높고 ISIN/CUSIP/SEDOL 포함, 발행주식수는 없음 | `https://www.ishares.com/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data?...&component=holdings.all&portfolioId=<ID>[&asOfDate=YYYYMMDD]` | 보유 내역이 병렬 배열(`dataPointsByNameMap.<field>.value`)로 들어 있음 |
| Invesco 보유 내역 | QQQ 보유 내역 (종목, 주식수, 비중) | `https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund?idType=ticker&productType=ETF&cb=<epoch>` | 종목별 가격·시가가 없음. 최신 스냅샷만 제공(과거 날짜 요청은 `unsupported`) |
| Invesco 펀드 정보 | QQQ NAV, 발행주식수, 순자산 | `https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ?idType=ticker&variationType=fundDetails&productType=ETF&cb=<epoch>` | 종목별 시가 = 비중 x 순자산, 가격 = 시가 / 주식수로 유도(`meta.price_derivation`) |

#### User-Agent 정책

- 모든 요청은 중립 프로젝트 UA `etf-rebalancing-tracker/0.2 (+https://github.com/...)`로 보냅니다(`etf_tracker.http.DEFAULT_USER_AGENT`).
- Invesco의 Fastly 엣지는 `Mozilla/5.0`으로 시작하는 UA와 빈 UA에 본문 없는 HTTP 406을 돌려줍니다. 406이 오면 UA 헤더 없이(urllib 기본 UA) 한 번 재시도하며, 브라우저 UA는 절대 보내지 않습니다.
- iShares/BlackRock은 크롤러 UA(Googlebot 등)에 403을 돌려줍니다. 중립 UA는 통과합니다.
- `HTTPS_PROXY` 환경 변수를 그대로 따르고 TLS 검증을 끄지 않습니다.

#### 파싱과 정규화 규칙

- 내려받은 바이트는 신뢰하지 않습니다. 본문 크기(8 MiB)와 행 수(10,000)를 제한하고, as-of 날짜, 행 수 하한(SOXX 20, IGV 60, QQQ 주식 90), 비중 합계(98~102%)를 검증합니다. 검증 실패는 `error`, 빈 템플릿은 `no_data`, 과거 날짜를 못 주는 소스는 `unsupported`입니다.
- 자산 구분: iShares `Equity` -> `equity`, `Money Market`/`Cash`/`Cash Collateral and Margins` -> `cash`, `Futures` -> `derivative`. Invesco `Common Stock` -> `equity`, `American Depository Receipt` -> `equity` + `is_adr`, `Index Future` -> `derivative`, `Currency`/`Currency Collateral`/`Synthetic Cash` -> `cash`. 티커가 빈 행은 종목명으로 안정적인 합성 티커(`_CASH_COLLATERAL` 등)를 만듭니다.
- ADR 판별(SOXX의 ADR 합계 10% 상한에 사용): 종목명에 `ADR` 또는 `AMERICAN DEPOSITARY`가 있으면 ADR, 그렇지 않은 주식 중 상장 지역(Location)이 미국이 아니면 **휴리스틱으로 ADR로 추정** 하고 `meta.adr_heuristic`에 기록합니다(현재 TSM). 미국 밖 회사지만 미국 상장 라인이 보통주인 종목은 `sources.ishares.NON_ADR_FOREIGN_ORDINARIES`(현재 TSEM과 NVMI, 각각 Tower Semiconductor와 Nova Ltd.의 보통주)에 적어 두면 추정에서 제외되고 `meta.adr_overrides`에 기록됩니다. 두 목록은 리포트의 데이터 경고에 매일 표시됩니다.
- 비중은 패키지 안에서 항상 0~1 분수입니다. 발행사 파일의 퍼센트는 파서가 100으로 나눕니다.

#### 저장 구조 (`data/`, git 추적 대상)

```
data/
|-- manifest.json                      # ETF별 latest_as_of, last_status, last_message, row_count, source, 갱신 시각
|-- raw/<ETF>/<YYYY-MM-DD>.csv         # iShares CSV 원본 (SOXX, IGV)
|-- raw/QQQ/<YYYY-MM-DD>.json          # Invesco 보유 내역 원본
|-- raw/QQQ/<YYYY-MM-DD>.fund.json     # Invesco 펀드 정보 원본
`-- normalized/<ETF>/<YYYY-MM-DD>.json # Snapshot (holdings + meta), etf_tracker.holdings.load_snapshot으로 읽음
```

같은 날짜가 이미 `data/normalized/`에 있으면 다시 쓰지 않으므로(`--force`를 주지 않는 한) 하루에 몇 번을 실행해도 결과가 같습니다.

### 2.2 분석 코어 (`etf_tracker/`, 구현 완료)

표준 라이브러리만 사용합니다(pandas, numpy, requests 없음).

| 모듈 | 역할 | 패키지 내부 의존 |
|---|---|---|
| `market_calendar.py` | NYSE 거래일 달력(휴장일 규칙, 임시 휴장 목록), 셋째 금요일, 둘째 금요일 전 목요일, 거래일 산술 | 없음 |
| `holdings.py` | 데이터 모델 `Holding`, `Snapshot`과 JSON 저장·적재, 검증(`Snapshot.validate`), 정규화 비중 | 없음 |
| `rules.py` | 지수별 상한 규칙과 일정: `SOXXRules`, `QQQRules`, `IGVRules`, `apply_caps`, `check_constraints`, `schedule`, `next_events`, QQQ `special_rebalance_triggered` | `market_calendar` |
| `decompose.py` | 두 스냅샷 사이 비중 변화를 drift와 trade로 분해, 자금 유입 배율 k 추정, 기업행동 의심 탐지, 종목 분류 | `holdings` |
| `bridge.py` | `Snapshot` -> `rules.Constituent` 변환, 스냅샷에 상한 규칙 바로 적용 | `holdings`, `rules` |
| `http.py` | urllib 기반 GET(중립 UA, 재시도, gzip), `Response` | 없음 |
| `sources/__init__.py`, `sources/ishares.py`, `sources/invesco.py` | 발행사별 `fetch(etf, as_of=None, *, http_get=None) -> FetchResult`와 파서 | `http`, `holdings` |
| `store.py` | `data/raw`, `data/normalized`, `data/manifest.json` 입출력, 멱등 `ingest` | `holdings`, `sources` |
| `analysis.py` | `analyze_etf`, `analyze_all`: 저장된 최신 두 스냅샷으로 분해·상한·이벤트를 계산한 JSON 딕셔너리 | `decompose`, `bridge`, `rules`, `market_calendar` |
| `report.py` | 분석 JSON을 한국어 마크다운과 JSON 리포트로 렌더링(`render_markdown`, `render_summary_line`, `write_reports`) | `rules` |
| `pipeline.py`, `cli.py`, `__main__.py` | `run_daily`, `run_backfill`, `run_fetch`, `run_analysis`와 명령행 인터페이스 | 전부 |
| `__init__.py` | 주요 클래스·함수 재수출, `__version__` (0.2.0) | 전부 |

설계 원칙:

- 패키지 내부에서 비중은 항상 **0.0부터 1.0 사이의 분수(fraction)** 입니다. 퍼센트 표기는 리포트 단계에서만 합니다.
- 날짜는 `datetime.date`, 금액·가격은 `float`입니다.
- `import etf_tracker`는 네트워크 코드를 불러오지 않습니다. 소스 모듈은 `pipeline.SOURCES`(ETF -> 모듈 경로)를 통해 실행 시점에 import됩니다.
- `etf_tracker.decompose`는 서브모듈 이름이므로, 분해 함수는 패키지 최상위에서 `decompose_snapshots`라는 이름으로 노출합니다.

### 2.3 리포트 (`etf_tracker/report.py`, 구현 완료)

`reports/latest.md`는 요약표(기준일, 전일 스냅샷, 발행주식수, 자금 유출입 배율 k)와 ETF별 절(상한 점검, 다음 이벤트, 비중 변화 분해, 데이터 경고), 각주로 구성됩니다. 상한 점검표는 지수별 한도와 여유/초과를 보여 주고 한도 0.25pp 이내는 `(경계)`로 표시합니다. 강제 매도·매수 상위 5개에는 비중 변화(pp)와 순자산을 곱한 추정 금액이 붙습니다. 전일 스냅샷이 없는 ETF(처음 저장된 날)는 "첫 스냅샷" 문구가 나옵니다. 발행사 파일의 발행주식수는 결제 시차 때문에 보유 내역보다 하루 늦게 움직이는 경우가 있어(iShares 파일에서 확인), 같은 날의 k와 어긋날 수 있다는 각주가 붙습니다.

### 2.4 명령행 사용법

```bash
cd /path/to/etf-rebalancing-tracker

python3 -m etf_tracker run --root .                 # 최신 보유 내역 수집 -> 분석 -> 리포트 (워크플로가 호출)
python3 -m etf_tracker fetch --root . --etf SOXX    # 수집·저장만
python3 -m etf_tracker analyze --root . --json      # 저장된 스냅샷 분석 결과를 JSON으로 출력
python3 -m etf_tracker report --root .              # 수집 없이 분석하고 리포트만 다시 씀
python3 -m etf_tracker backfill --root . --etf SOXX --start 2026-09-01 --end 2026-10-08   # 과거 구간 수집
python3 -m etf_tracker --help                       # 하위 명령과 옵션(한국어 도움말)
```

공통 옵션: `--root`(기본 `.`), `--etf`(반복 가능, 생략 시 SOXX·QQQ·IGV. 수집 대상만 제한하며, 분석과 리포트 파일은 저장소에 스냅샷이 있는 모든 ETF를 항상 포함합니다), `--as-of YYYY-MM-DD`(특정 날짜 요청), `--force`(저장된 날짜도 덮어쓰기), `--today`(분석 기준일, 생략 시 UTC 오늘), `--log-level`, `--json`.

종료 코드: `0` 모든 ETF가 정상이거나 이미 최신, `2` 어떤 ETF의 수집·분석·리포트가 실패(성공한 ETF의 데이터는 이미 저장됨), `3` 분석할 스냅샷이 하나도 없음, `1` 사용법 오류(인자 구문 오류, backfill에서 `--etf`가 하나가 아니거나 종료일이 시작일보다 앞인 경우) 또는 예외로 중단. `no_data`(비거래일, 미공개)와 `unsupported`(Invesco 과거 날짜)는 실패로 보지 않고 리포트의 데이터 경고로만 남깁니다.

### 2.5 과거 데이터 채우기 (backfill)

iShares는 `asOfDate`로 과거 거래일을 조회할 수 있으므로 SOXX와 IGV는 임의 구간을 채울 수 있습니다. `backfill`은 거래일만 오래된 순서로 요청하고, 이미 저장된 날짜는 건너뛰며, 요청 사이에 0.5초(`--pause`) 쉬고, 전송 오류가 3번 연속이면 멈춥니다. Invesco는 최신 스냅샷만 제공하므로 QQQ는 매일 실행으로만 역사가 쌓입니다. 2026-10-09에 SOXX와 IGV는 2026-09-01부터 2026-10-08까지 27거래일을 채웠고 QQQ는 2026-10-08부터 시작합니다.

GitHub Actions에서도 `workflow_dispatch` 입력(`etf`, `backfill_start`, `backfill_end`, `force`)으로 같은 작업을 실행할 수 있습니다.

### 2.6 일일 워크플로 (`.github/workflows/daily.yml`)

- 일정: 평일 **14:10 UTC** 1차, **18:10 UTC** 재시도(GitHub cron은 UTC만 지원). 2026-10-09 측정 기준으로 세 발행사 모두 T-1 보유 내역을 거래일 T의 13:10 UTC 전에 공개했습니다. 1차 실행이 데이터를 저장하면 2차 실행은 "already stored"로 끝나므로 중복 저장이 없습니다. 새로 저장된 스냅샷이 없으면 manifest와 리포트도 건드리지 않으므로, 시각만 바뀐 불필요한 커밋이 생기지 않습니다.
- 절차: checkout -> Python 3.12 -> `pytest -q` -> `python3 -m etf_tracker run --root .` -> `data/`, `reports/`를 `github-actions[bot]`으로 커밋(`git pull --rebase` 후 push, 3회 시도) -> 추적기 종료 코드가 0이 아니면 그때 작업을 실패로 표시(데이터 커밋은 그 전에 끝남).
- 미국 휴장일에도 cron은 돌지만 발행사가 같은 날짜를 다시 주므로 아무것도 저장하지 않고 0으로 끝납니다.

### 2.7 Claude 일일 브리핑에서 결과를 읽는 방법

- 사람이 읽는 본문은 `reports/latest.md`(한국어), 수치는 `reports/latest.json`입니다. 두 파일은 새 스냅샷이 저장된 실행, `--force` 실행, 또는 파일이 없을 때만 다시 쓰고(`report` 하위 명령은 항상 다시 씁니다), 같은 내용이 `reports/daily/<분석 기준일>.{md,json}`에 날짜별로 남습니다. GitHub에서 바로 읽을 때는 `https://raw.githubusercontent.com/<owner>/<repo>/<branch>/reports/latest.json` 형태의 raw URL을 쓰면 됩니다.
- `reports/latest.json`의 구조는 `analyze_all`의 반환값과 같습니다.

```
{
  "generated_utc": "2026-10-09T14:53:14Z", "today": "2026-10-09",
  "etfs": {
    "SOXX": {
      "etf", "index_id", "as_of", "prev_as_of", "source",
      "fund": {"shares_outstanding", "prev_shares_outstanding", "nav", "total_net_assets"},
      "holdings": [ {"ticker","name","asset_class","weight","shares","price","market_value","is_adr","company_id"} ... 비중 내림차순 ],
      "decomposition": null | {"scale_factor", "changes": [...], "entries", "exits", "warnings", "summary": {...}},
      "caps": {"event_type", "feasible", "binding_constraints", "notes", "metrics", "target_metrics",
               "breaches": [{"rule","description","tickers","value","limit"}],
               "forced_sellers": [["AMD", -0.0151], ...], "forced_buyers": [["NVDA", 0.0044], ...],
               "special_rebalance": null | {"triggered", "reasons", "metrics"}},
      "next_events": [{"kind","reference_date","announcement_date","effective_trade_date","effective_date",
                       "trading_days_to_reference","trading_days_to_trade","notes"} ... 3개],
      "warnings": [ ... ]
    }, "QQQ": {...}, "IGV": {...}
  },
  "errors": {}
}
```

- 브리핑에 넣을 핵심 항목: `etfs.<ETF>.as_of`(기준일), `caps.breaches`(현재 위반), `caps.forced_sellers`/`forced_buyers`(상한 재적용 시 매도·매수 종목과 비중 변화, 분수 단위이므로 100을 곱해 pp로), `caps.metrics`(지수별 지표: SOXX `max_weight`, `adr_sum`, `names_over_8`, `names_over_4_outside_top5`; QQQ `max_company_weight`, `sum_over_4_5`; IGV `max_weight`, `sum_over_4_5`), `caps.special_rebalance.triggered`(QQQ), `next_events[0]`의 `trading_days_to_reference`와 `trading_days_to_trade`, `decomposition.summary`의 `scale_factor`, `active_trade_tickers`, `entries`, `exits`(실제 매매가 있었던 날인지), `warnings`(데이터 경고). `errors`에 ETF가 있으면 그 ETF는 이번 실행에서 분석되지 않은 것입니다.
- 한 줄 요약이 필요하면 `python3 -c "import json, etf_tracker as et; print(et.render_summary_line(json.load(open('reports/latest.json'))))"`를 실행합니다. 예: `SOXX 2026-10-08: 8% 초과 2종목(AMD 9.51%, INTC 8.63%), 상위 5 밖 4% 초과 4종목(MRVL 4.68%, KLAC 4.05%, ADI 4.04%, AMAT 4.01%), ADR 합계 비중 9.79% (한도 10.00%, 경계), 예상 회전율 2.93% (약 1.35B USD, 최대 매도 AMD -1.51pp), 실제 매매 없음, k=0.9915, 다음 참조일 2026-11-30 (D-35거래일)`.
- 대시보드처럼 파일을 통째로 읽어야 하는 소비자(claude.ai 페이지가 GitHub 커넥터로 읽는 경우 등)에게는 `reports/latest.json`(약 220KB)이 너무 크므로, 같은 실행에서 `reports/summary.json`(약 95KB)을 함께 씁니다(`etf_tracker/summary.py`, `build_summary`는 분석 딕셔너리만의 순수 함수). 최상위 `generated_utc`, `today`, `errors`는 같고, ETF별로 `fund`, `caps`, `next_events`, `warnings`는 그대로, `holdings` 대신 주식 보유만 `equities`(`ticker`, `name`, `weight_eq`(주식만으로 재정규화한 비중, 상한 점검과 같은 기준), `weight_fund`(발행사 비중), `is_adr`, `shares`, `price`, `market_value`; `weight_eq` 내림차순)와 `equity_count`로, `decomposition`은 `summary`와 `scale_factor`, 주식 변화 중 |주식수 효과|(소수 4자리 반올림) 내림차순·|총 변화| 내림차순 상위 10개 `top_changes`, 주목할 변화 `notable`(flow_only가 아닌 변화. 단, 현금·선물 라인의 active_trade는 매일 생기는 잔고 변동이라 제외하고 편입·제외·기업행동만 남김), `n_changes`로 줄입니다. 기준 입력(시가 또는 비중)이 없는 주식 라인의 `weight_eq`는 상한 점검과 같이 0이며, `reports/summary.json`이 없으면 `run`이 새 스냅샷 없이도 리포트 묶음을 다시 씁니다(`latest.*`와 같은 정책). `reports/history.json`은 날짜별 지표·비중 시계열입니다(`etf_tracker/history.py`).
- 모든 비중은 분수(0~1)이고 날짜는 ISO 문자열이며 NaN은 없습니다(`null`). 리포트의 상한 점검 비중은 주식만으로 재정규화한 값이라 보유 내역의 펀드 비중보다 조금 큽니다(현금 비중만큼).

### 2.8 파이썬에서 직접 사용하기

```python
from datetime import date
from pathlib import Path
import etf_tracker as et

root = Path(".")

# (1) 저장된 최신 두 스냅샷으로 분해: drift(가격 효과) vs trade(주식수 효과)
prev, curr = et.latest_two(root, "SOXX")
result = et.decompose_snapshots(prev, curr)
for change in result.active_trades()[:5]:
    print(change.ticker, f"{change.drift:+.4%}", f"{change.trade:+.4%}", change.classification)

# (2) 지수 상한 규칙 재적용: 다음 이벤트 종류는 달력에서 자동 선택
caps = et.apply_caps_to_snapshot(curr, "SOXX")
print(caps.event_type, caps.forced_sellers, caps.binding_constraints)

# (3) 분석 JSON과 리포트
analysis = et.analyze_all(root, ["SOXX", "QQQ", "IGV"], date(2026, 10, 9))
print(et.render_summary_line(analysis))
et.write_reports(root, analysis)

# (4) 전체 일일 작업 (수집 -> 저장 -> 분석 -> 리포트)
outcome = et.run_daily(root)
print(outcome.exit_code, outcome.failed_etfs)
```

### 2.9 문서

- `docs/methodology.md`: 세 지수의 선정·가중·상한·일정 규칙, 2026년 4분기부터 2027년 4분기까지의 이벤트 일정표, 검증 상태와 주의 사항, 매일 계산할 지표 목록, 실제 데이터로 확인한 사항
- `docs/sources.md`: 출처 URL, 문서 일자, 원문 확인 여부, 데이터 엔드포인트

## 3. 테스트 실행

Python 3.11 이상(개발 환경은 3.13, 워크플로는 3.12)에서 다음을 실행합니다.

```bash
cd /path/to/etf-rebalancing-tracker
python3 -m pip install -r requirements-dev.txt   # pytest만 설치됨
python3 -m pytest -q
ETF_TRACKER_NETWORK_TESTS=1 python3 -m pytest -q -k live   # 발행사 API를 실제로 호출하는 테스트 2개 (기본은 skip)
```

테스트는 `tests/`에 모듈별로 있습니다. 분석 코어: `test_market_calendar.py`, `test_holdings.py`, `test_rules.py`, `test_decompose.py`, `test_bridge.py`, `test_package_api.py`. 데이터 계층과 리포트: `test_source_ishares.py`, `test_source_invesco.py`(2026-10-09에 받은 실제 응답 `tests/fixtures/`로 파서 검증, 가짜 전송 계층으로 fetch 경로 검증), `test_store.py`, `test_analysis.py`, `test_pipeline_cli.py`, `test_report.py`. 네트워크 테스트는 환경 변수 `ETF_TRACKER_NETWORK_TESTS=1`일 때만 실행됩니다.

## 4. 저장소 구조

```
.
|-- .github/workflows/daily.yml   # 일일 수집·분석·리포트·커밋 워크플로 (평일 14:10, 18:10 UTC)
|-- .github/workflows/probe.yml   # 발행사 다운로드 가능성 점검 워크플로(수동 실행, 초기 조사용)
|-- scripts/probe_downloads.sh    # 점검 스크립트
|-- probe/summary.txt             # 점검 결과(2026-10-09)
|-- etf_tracker/                  # 패키지 (0.2.0)
|   |-- __init__.py, __main__.py, cli.py, pipeline.py
|   |-- http.py, store.py, analysis.py, report.py
|   |-- sources/__init__.py, sources/ishares.py, sources/invesco.py
|   |-- market_calendar.py, holdings.py, rules.py, decompose.py, bridge.py
|-- tests/                        # pytest (fixtures/에 2026-10-09 실제 응답)
|-- docs/
|   |-- methodology.md
|   `-- sources.md
|-- data/                         # raw/<ETF>/ 원본, normalized/<ETF>/<날짜>.json 스냅샷, manifest.json. git 추적 대상
|-- reports/                      # latest.md, latest.json, daily/<날짜>.{md,json}. git 추적 대상
|-- requirements-dev.txt
|-- .gitignore
`-- README.md
```

## 5. 상태 (2026-10-09 기준)

완료:

- 분석 코어 5개 모듈, 데이터 계층(iShares CSV/JSON, Invesco JSON), 저장 계층, 분석 JSON, 한국어 리포트, 명령행 인터페이스, 일일 워크플로. 패키지 버전 0.2.0, 테스트 695개 통과(네트워크 테스트 2개는 기본 skip).
- 실제 실행: 2026-10-09 14:40 UTC에 세 ETF의 2026-10-08 보유 내역을 받아 저장했고, SOXX와 IGV는 2026-09-01부터 2026-10-08까지 27거래일을 backfill했습니다. QQQ는 2026-10-08부터 시작합니다(Invesco는 과거 날짜를 제공하지 않음).
- 실제 데이터로 확인한 사항: SOXX의 9월 연간 재구성은 매매일(2026-09-18) 종가 파일에 이미 반영되어 있어 2026-09-17 -> 2026-09-18 분해에서 신규 편입 SKHY, TSEM, CBRS와 제외 NVMI, RMBS, SWKS, INTC 주식수 +60.6%, TSM -32.8% 등 26개 실제 매매(주식수 효과 합계 10.4pp)로 나타납니다. 2026-09-18 -> 2026-09-21은 가격 효과만 있습니다. 평상시 하루 분해(2026-10-07 -> 2026-10-08)는 k=0.9915, 주식수 효과 0.01pp로 조용합니다.
- 규칙 재검증: SOXX의 8% / 상위 5 밖 4% / ADR 합계 10% 상한은 iShares Trust SAI(2025-08-01, Form 497)의 서술로 확인되었습니다(`docs/methodology.md` 3절). ICE의 현행 방법론 원문은 로그인이 필요해 아직 읽지 못했습니다.

알려진 한계와 다음 단계:

- ADR 판별은 발행사 파일의 종목명과 상장 지역에 의존합니다. TSM은 이름에 ADR이 없어 상장 지역으로 추정하고, TSEM(Tower Semiconductor)과 NVMI(Nova Ltd.)는 이스라엘 회사의 보통주가 NASDAQ에 직접 상장된 경우라 `NON_ADR_FOREIGN_ORDINARIES`로 제외합니다. 새 종목이 편입되면 데이터 경고의 "ADR 추정 종목"을 확인해야 합니다.
- 발행사 파일의 발행주식수는 결제 시차 때문에 보유 내역보다 하루 늦게 움직이는 경우가 있어, 같은 날의 k(보유 주식수에서 추정)와 발행주식수 변화율이 어긋날 수 있습니다. 리포트 각주에 명시했습니다.
- IGV 규칙은 2026년 BofA 424B2 보충서의 요약에 기초합니다(S&P DJI 원문은 HTTP 403). QQQ 회사 단위 2단계에서 4.5% 미만 회사의 하향 조정 상한은 Nasdaq 문서에 명시되어 있지 않아 근사치입니다. 12월 리밸런스 발표 비중과 추적기 계산을 대조해 검증할 예정입니다.
- 워크플로는 미국 휴장일에도 실행되며(저장 없이 종료), 발행사가 공개를 늦추면 18:10 UTC 재시도까지 기다립니다. 그 뒤에도 없으면 다음 날 실행이 하루치를 건너뛴 상태로 저장하므로, 빠진 날짜는 `backfill`로 채울 수 있습니다(iShares만).

다음 이벤트(세 지수 공통 매매일 2026-12-18): SOXX 분기 리밸런스(참조 2026-11-30, 발표 2026-12-04), QQQ 연간 재구성(참조 2026-11-30, 발표 2026-12-11, 효력 2026-12-21), IGV 반기 재구성(재구성 참조 2026-11-30, 가격 참조 2026-12-10). 2026-10-09 기준 참조일까지 35거래일, 매매일까지 49거래일입니다.

## 6. 대시보드 (클로드 아티팩트)

`site/dashboard.html`을 claude.ai 아티팩트로 발행한 대시보드가 결과를 보여줍니다. 주소는 https://claude.ai/artifact/Em7XgPghM7nNAmoHVCFczz 이고 소유자 계정으로만 열립니다. 페이지는 열릴 때 GitHub 커넥터로 `reports/summary.json`, `reports/history.json`, `reports/briefing/latest.md`를 읽으므로, Actions가 커밋한 최신 데이터와 예약 작업이 커밋한 브리핑이 다시 발행하지 않아도 반영됩니다. 자세한 내용은 `site/README.md`, 브리핑 작성 규칙과 예약 작업 지시문은 `docs/briefing.md`에 있습니다.
