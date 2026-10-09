# etf-rebalancing-tracker

미국 ETF 세 종목(SOXX, QQQ, IGV)의 보유 내역을 매일 추적하여, 종목별 비중 변화를 **가격 변동 효과(price drift)** 와 **주식수 변화 효과(share-count change)** 로 분해하고, 각 종목이 추적 지수의 **상한(cap) 규칙** 에서 얼마나 떨어져 있는지를 계산하는 도구입니다. 목표는 지수 리밸런스 시점에 발생하는 **강제 매수·매도(forced buying / selling)** 를 미리 가늠하는 것입니다.

| ETF | 운용사 | 추적 지수 | 지수 제공자 | 종목 수 |
|---|---|---|---|---|
| SOXX | iShares (BlackRock) | NYSE Semiconductor Index (구 ICE Semiconductor Index) | ICE Data Indices | 30 |
| QQQ | Invesco | Nasdaq-100 Index (NDX) | Nasdaq | 약 100개 회사 / 약 101개 증권 (Alphabet은 GOOGL, GOOG 두 종류) |
| IGV | iShares (BlackRock) | S&P North American Expanded Technology Software Index | S&P Dow Jones Indices | 가변 |

## 1. 목적

지수 추종 ETF의 비중은 리밸런스 사이에 가격에 따라 자연스럽게 움직입니다(drift). 반면 운용사가 실제로 주식을 사고팔면 주식수가 바뀝니다. 두 효과를 분리하면 다음을 알 수 있습니다.

- 어떤 종목의 비중 증가가 단순한 주가 상승인지, 자금 유입·리밸런스에 따른 실제 매수인지
- 다음 리밸런스 참조일 기준으로 어떤 종목이 상한(예: QQQ 회사 단위 24%/20%, 4.5%/48%, SOXX 8%/4%, IGV 8.5%/45%)에 걸려 **강제 매도** 되고, 그 비중이 어떤 종목으로 **재분배** 되어 강제 매수가 생기는지
- 그 이벤트가 며칠 뒤인지(참조일, 발표일, 매매일, 효력일)

규칙과 일정의 근거는 `docs/methodology.md`, 출처 URL은 `docs/sources.md`에 정리되어 있습니다.

## 2. 아키텍처

```
발행사 보유 내역 파일  ->  [데이터 계층: 미구현]  ->  Snapshot(JSON)  ->  [분석 코어]  ->  리포트(미구현)
```

### 2.1 데이터 계층 (미구현)

iShares(SOXX, IGV)와 Invesco(QQQ)의 보유 내역 다운로드를 GitHub Actions 러너에서 점검한 결과(`probe/summary.txt`, 2026-10-09 12:42 UTC), **발행사 사이트가 데이터센터 IP의 요청을 차단** 합니다.

- iShares CSV 엔드포인트: HTTP 200이지만 본문이 CSV가 아닌 HTML(상품 페이지)로 돌아옴
- Invesco 다운로드 및 상품 페이지: HTTP 406

따라서 자동 수집기는 아직 구현하지 않았고, 대체 소스(발행사 외 데이터 제공자, 브라우저 기반 수집, 수동 업로드 등)에 대한 **소스 평가가 진행 중** 입니다. 데이터 계층이 들어오면 원본 파일은 `data/raw/<ETF>/`, 정규화된 스냅샷은 `data/normalized/<ETF>/<YYYY-MM-DD>.json`에 저장하는 것을 전제로 하며(`etf_tracker.holdings.snapshot_path`), `data/`는 git으로 추적합니다.

점검 스크립트와 워크플로: `scripts/probe_downloads.sh`, `.github/workflows/probe.yml`.

### 2.2 분석 코어 (`etf_tracker/` 패키지, 구현 완료)

표준 라이브러리만 사용합니다(pandas, numpy, requests 없음).

| 모듈 | 역할 | 패키지 내부 의존 |
|---|---|---|
| `market_calendar.py` | NYSE 거래일 달력(휴장일 규칙, 임시 휴장 목록), 셋째 금요일, 둘째 금요일 전 목요일, 거래일 산술(`add_trading_days`, `trading_days_between`) | 없음 |
| `holdings.py` | 데이터 모델 `Holding`, `Snapshot`과 JSON 저장·적재(`save_snapshot`, `load_snapshot`, `latest_two`), 검증(`Snapshot.validate`), 정규화 비중(`normalized_weights`) | 없음 |
| `rules.py` | 지수별 상한 규칙과 일정: `SOXXRules`, `QQQRules`, `IGVRules`(공통 인터페이스 `IndexRules`), `apply_caps`(상한 재적용과 재분배), `check_constraints`, `schedule`, `next_events`, QQQ `special_rebalance_triggered` | `market_calendar`(일정 계산 시에만 지연 import) |
| `decompose.py` | 두 스냅샷 사이 비중 변화를 drift(가격 효과)와 trade(주식수 효과)로 분해, 자금 유입 배율 k 추정, 액면분할 등 기업행동 의심 탐지, 종목 분류(`flow_only`, `active_trade`, `entry`, `exit`, `corporate_action_suspect`) | `holdings` |
| `bridge.py` | `Snapshot` -> `rules.Constituent` 변환(`constituents_from_snapshot`), 스냅샷에 상한 규칙 바로 적용(`apply_caps_to_snapshot`) | `holdings`, `rules` |
| `__init__.py` | 위 모듈의 주요 클래스·함수 재수출, `__version__` | 전부 |

설계 원칙:

- 패키지 내부에서 비중은 항상 **0.0부터 1.0 사이의 분수(fraction)** 입니다. 퍼센트 표기는 리포트 단계에서만 합니다.
- 날짜는 `datetime.date`, 금액·가격은 `float`입니다.
- 모듈 간 결합을 최소화합니다. `market_calendar`는 다른 모듈을 import하지 않고, `decompose`는 `holdings`만 import합니다.
- `etf_tracker.decompose`는 서브모듈 이름이므로, 분해 함수 `decompose.decompose`는 패키지 최상위에서 `decompose_snapshots`라는 이름으로 노출합니다.

### 2.3 사용 예

```python
from datetime import date
import etf_tracker as et

prev = et.load_snapshot("data/normalized/QQQ/2026-10-08.json")
curr = et.load_snapshot("data/normalized/QQQ/2026-10-09.json")

# (1) 비중 변화 분해: drift(가격 효과) vs trade(주식수 효과)
result = et.decompose_snapshots(prev, curr)
for change in result.active_trades()[:5]:
    print(change.ticker, f"{change.drift:+.4%}", f"{change.trade:+.4%}", change.classification)

# (2) 지수 상한 규칙 재적용: 다음 이벤트 종류는 달력에서 자동 선택
caps = et.apply_caps_to_snapshot(curr, "QQQ")
print(caps.event_type, caps.forced_sellers, caps.binding_constraints)

# (3) 다음 리밸런스 일정
for event in et.rules_for("QQQ").next_events(date(2026, 10, 9), n=2):
    print(event.kind, event.reference_date, event.announcement_date, event.effective_trade_date)
```

### 2.4 문서

- `docs/methodology.md`: 세 지수의 선정·가중·상한·일정 규칙, 2026년 4분기부터 2027년 4분기까지의 이벤트 일정표, 검증 상태와 주의 사항, 매일 계산할 지표 목록
- `docs/sources.md`: 출처 URL, 문서 일자, 원문 확인 여부

## 3. 테스트 실행

Python 3.11 이상(개발 환경은 3.13)에서 다음을 실행합니다.

```bash
cd /path/to/etf-rebalancing-tracker
python3 -m pip install -r requirements-dev.txt   # pytest만 설치됨
python3 -m pytest -q
```

테스트는 `tests/`에 모듈별로 있습니다(`test_market_calendar.py`, `test_holdings.py`, `test_rules.py`, `test_decompose.py`, `test_bridge.py`, `test_package_api.py`). 달력 테스트는 NYSE 공식 2025년부터 2027년 휴장일 목록을, 규칙 테스트는 합계 1, 상한 준수, 순위 보존 등의 불변식을 검사합니다.

## 4. 저장소 구조

```
.
|-- .github/workflows/probe.yml   # 발행사 다운로드 가능성 점검 워크플로(수동 실행)
|-- scripts/probe_downloads.sh    # 점검 스크립트
|-- probe/summary.txt             # 점검 결과(2026-10-09)
|-- etf_tracker/                  # 분석 코어 패키지
|   |-- __init__.py
|   |-- market_calendar.py
|   |-- holdings.py
|   |-- rules.py
|   |-- decompose.py
|   `-- bridge.py
|-- tests/                        # pytest
|-- docs/
|   |-- methodology.md
|   `-- sources.md
|-- data/                         # (예정) raw/<ETF>/ 원본, normalized/<ETF>/<날짜>.json 스냅샷. git 추적 대상
|-- requirements-dev.txt
|-- .gitignore
`-- README.md
```

## 5. 상태 (2026-10-09 기준)

완료:

- 분석 코어 5개 모듈과 패키지 API(`etf_tracker` 0.1.0), 테스트 전부 통과
- 지수 방법론 문서와 출처 목록
- 발행사 다운로드 가능성 점검(결과: 데이터센터 IP 차단 확인)

미완 / 다음 단계:

- 데이터 계층: 보유 내역 소스 평가 진행 중. 소스가 정해지면 다운로드·파싱·`Snapshot` 정규화 모듈과 일일 실행 워크플로를 추가
- 리포트 계층: 분해 결과와 상한 거리, 다음 이벤트 카운트다운을 사람이 읽을 형식으로 출력하는 모듈
- 규칙 재검증: SOXX 상한은 2021년 iShares 497 보충서, IGV 규칙은 2026년 BofA 424B2 보충서의 설명을 근거로 구현했고, ICE와 S&P DJI의 원 방법론 문서는 컨테이너에서 접근할 수 없어 확인하지 못했습니다. QQQ 회사 단위 2단계에서 4.5% 미만 회사의 하향 조정 상한은 Nasdaq 문서에 명시되어 있지 않아 근사치입니다. 자세한 내용은 `docs/methodology.md` 3절

다음 이벤트(세 지수 공통 매매일 2026-12-18): SOXX 분기 리밸런스(참조 2026-11-30, 발표 2026-12-04), QQQ 연간 재구성(참조 2026-11-30, 발표 2026-12-11, 효력 2026-12-21), IGV 반기 재구성(재구성 참조 2026-11-30, 가격 참조 2026-12-10).
