# 출처 목록

접근일: 2026-10-09 (모든 항목 공통; 3절의 데이터 엔드포인트도 같은 날 확인). "원문 확인" 열은 이 세션의 작업 컨테이너에서 해당 URL을 실제로 내려받아 내용을 읽었는지를 뜻합니다. 읽지 못한 문서는 과제 브리프가 제공한 사실만 인용했고, `docs/methodology.md` 3절에 "재검증 필요"로 표시했습니다.

## 1. 지수 방법론 문서

| # | 출처 | URL | 문서 일자 | 사용 목적 | 원문 확인 |
|---|---|---|---|---|---|
| 1 | iShares Trust, Form 497 보충서 "Important Notice Regarding Change in Investment Policy" (iShares PHLX Semiconductor ETF에서 iShares Semiconductor ETF로 변경, 기초지수를 PHLX Semiconductor Sector Index에서 ICE Semiconductor Index로 변경) | https://www.sec.gov/Archives/edgar/data/1100663/000119312521126827/d82642d497.htm | 보충서 2021-04-22, 변경 효력 2021-06-21 | SOXX 지수의 종목 선정 기준, 8% / 4% / ADR 10% 상한과 재분배 절차, 9월 연간 재구성과 3/6/12월 분기 리밸런스의 참조일과 발표일, 월간 10% 주식수 업데이트, 분기 중 종목 교체 없음 | 예 (HTML 전문 읽음) |
| 1b | iShares Trust, Statement of Additional Information (SAI, Form 497) dated August 1, 2025. SOXX 기초지수 설명: "all constituents capped at 8% ... constituents outside the initial five largest capped at 4% ... cumulative weight of all ADRs capped at 10%" | https://www.sec.gov/ (EDGAR, iShares Trust CIK 1100663, 2025-08-01 제출 Form 497. 문서별 URL은 EDGAR 전문 검색으로 확인) | 2025-08-01 | SOXX 8% / 4% / ADR 10% 상한이 2025년 현재도 유효함을 확인. methodology.md 3.1절 SOXX 상한 행의 상태를 "확인됨"으로 변경한 근거 | 인용문은 통합 작업 브리프(2026-10-09)가 제공한 것이며, 이 세션의 컨테이너에서 SAI 전문을 직접 내려받지는 않았음 |
| 2 | iShares Semiconductor ETF (SOXX) 상품 페이지와 summary prospectus (지수명이 ICE Semiconductor Index에서 NYSE Semiconductor Index로 2023-11-03 변경되었다는 사실의 출처) | https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf | 2023년 이후 summary prospectus | SOXX 기초지수의 현재 명칭과 개명 효력일 | 상품 페이지 접근(HTTP 200)만 확인. summary prospectus 본문은 읽지 않았고 과제 브리프의 사실을 인용 |
| 3 | Nasdaq, "Nasdaq-100 Index Methodology" (NDX) | https://indexes.nasdaqomx.com/docs/methodology_NDX.pdf | PDF 생성일 2026-04-30 | QQQ 지수의 종목 선정 개요, 회사 단위 상한(24%/20%, 4.5%/48%/40%), 12월 증권 단위 상한(15%/14%, 상위 5개 40%/38.5%, 4.4%), Weight Interpolation Process, 재구성과 리밸런스 참조일, 발표일(효력일 6거래일 전), 효력일, Special Rebalance 발동 조건 | 예 (PDF 내려받아 텍스트 추출) |
| 4 | Nasdaq, "Nasdaq Index Weight Calculations" | https://indexes.nasdaqomx.com/docs/Nasdaq_Index_Weight_Calculations.pdf | 문서 본문 일자 2026-05-20 (PDF 생성일 2026-07-30) | QQQ 상한 재분배 수학: 최종 비중 = min(상한, 조정계수 × 초기 비중), 합계 보존, uncapped 종목의 공통 조정계수(비례 재분배), capping level 내 순위 보존, 다단계 capping의 처리 원칙 | 예 (PDF 내려받아 텍스트 추출) |
| 5 | BofA Finance LLC, Form 424B2 pricing supplement (S&P 500, SPDR S&P MidCap 400 ETF Trust, iShares Expanded Tech-Software Sector ETF에 연동된 Market Linked Securities). "The iShares Expanded Tech-Software Sector ETF"와 "S&P North American Expanded Technology Software Index" 절 | https://www.sec.gov/Archives/edgar/data/0001682472/000191870426012955/form424b2.htm | 2026-05-11 | IGV 지수의 구성(모지수 + Supplementary Stock), 적격 기준(시가총액 US$14억, 유동성 비율 30%/15%, float 20%/10%, 최소 22종목, ADR 제외), 8.5% 상한과 4.5%/45% 규칙의 6단계 절차, 가격 참조일(둘째 금요일 전 목요일), 분기 상한 적용과 반기 재구성 일정, index shares 고정에 따른 drift | 예 (HTML 전문 중 지수 설명 절 읽음) |
| 6 | S&P Dow Jones Indices, S&P North American Expanded Technology Software Index 페이지와 "S&P North American Technology Indices Methodology" | https://www.spglobal.com/spdji/en/indices/equity/sp-north-american-expanded-technology-software-index/ | 미확인 | IGV 규칙의 1차 출처. 재검증 대상 | 아니오. 이 컨테이너에서 HTTP 403 반환 |
| 7 | ICE Data Indices, NYSE Semiconductor Index 방법론 | 공개 URL 미확인 (indices.theice.com은 이 컨테이너에서 접속 실패) | 미확인 | SOXX 규칙의 1차 출처. 재검증 대상 | 아니오 |
| 8 | Nasdaq, "PHLX Semiconductor Sector Index Methodology" (SOX) | https://indexes.nasdaqomx.com/docs/methodology_SOX.pdf | PDF 생성일 2026-07-30 | SOXX와 자주 혼동되는 SOX 지수의 상한(시가총액 상위 3개 종목 12%/10%/8%, 나머지 4%)이 SOXX에 적용되지 않음을 확인하는 용도. methodology.md 3.2절 2항 | 예 (PDF 내려받아 텍스트 추출) |

## 2. 달력

| # | 출처 | URL | 사용 목적 | 원문 확인 |
|---|---|---|---|---|
| 9 | NYSE, Hours and Calendars (휴장일 공식 달력) | https://www.nyse.com/markets/hours-calendars | 2026년과 2027년 NYSE 휴장일. methodology.md의 날짜는 규칙 기반 계산(`etf_tracker/market_calendar.py`와 독립 계산의 교차 확인)이며, 이 페이지의 공식 달력과 대조하는 것을 권장 | 페이지 접근(HTTP 200)만 확인 |

## 3. 보유 내역 데이터 소스 (추적기 입력)

### 3.1 현재 사용하는 엔드포인트 (접근일 2026-10-09, 이 저장소의 작업 컨테이너에서 실제 다운로드 확인)

구현: `etf_tracker/sources/ishares.py`, `etf_tracker/sources/invesco.py`, 전송 계층 `etf_tracker/http.py`. 모든 요청은 중립 프로젝트 User-Agent(`etf-rebalancing-tracker/0.2 (+https://github.com/...)`)로 보냅니다.

| # | ETF | 용도 | URL | 확인 결과 |
|---|---|---|---|---|
| 10 | SOXX, IGV | 보유 내역 CSV (1차) | `https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&userType=individual&component=holdings&portfolioId=<ID>[&asOfDate=YYYYMMDD]` (SOXX 239705, IGV 239771) | HTTP 200, `text/csv`. `asOfDate` 생략 시 최신(2026-10-09 12:50 UTC에 Oct 08 데이터 확인), 과거 거래일 지정 시 그 날짜(2026-09-01부터 2026-10-08까지 27거래일 수집 성공, 2025-12-31도 응답). 비거래일(예: 20261004)과 미공개 날짜(20261009)는 HTTP 200에 빈 템플릿(`Fund Holdings as of,"-"`, 행 0개). 응답은 `cache-control: no-store`. 주식수 열 이름은 이 호스트에서 `Quantity`(blackrock.com/us/individual 미러는 `Shares`) |
| 11 | SOXX, IGV | 보유 내역 JSON (2차, CSV 실패 시) | `https://www.ishares.com/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data?appSubType=ISHARES&appType=PRODUCT_PAGE&component=holdings.all&locale=en_US&portfolioId=<ID>&targetSite=us-ishares&userType=individual&excludeContent=true&includeConfig=true[&asOfDate=YYYYMMDD]` | HTTP 200, `application/json`. 보유 내역은 `componentsByNameMap.holdings.containersByNameMap.all.dataPointsByNameMap.<field>.value` 병렬 배열, 스칼라 `asOfDate`(YYYYMMDD). ISIN/CUSIP/SEDOL 포함, 발행주식수 없음. 미공개 날짜는 배열이 `null`로 옴 |
| 12 | QQQ | 보유 내역 JSON | `https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund?idType=ticker&productType=ETF&cb=<unix epoch>` | HTTP 200, `content-type: text/plain`(본문은 JSON). `effectiveDate` 2026-10-08을 2026-10-09 13:05 UTC에 확인. 105행(주식 100, ADR 포함), 종목별 가격·시가 없음. 최신 스냅샷만 제공. `Mozilla/5.0`으로 시작하는 UA와 빈 UA에는 본문 없는 HTTP 406 |
| 13 | QQQ | 펀드 정보 JSON (NAV, 발행주식수, 순자산) | `https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ?idType=ticker&variationType=fundDetails&productType=ETF&cb=<unix epoch>` | HTTP 200. 종목별 시가 = `percentageOfTotalNetAssets/100 x shareclassTotalNetAssets`, 가격 = 시가 / units로 유도 |

공개 시점(2026-10-09 측정): 세 ETF 모두 T-1(10월 8일) 보유 내역이 거래일 T의 13:10 UTC 이전에 공개되었습니다. 일일 워크플로는 14:10 UTC와 18:10 UTC에 실행합니다.

### 3.2 초기 점검 결과 (2026-10-09 12:42 UTC, `probe/summary.txt`, 기록용)

위 API 경로를 찾기 전에 상품 페이지의 다운로드 경로를 점검한 결과입니다. 이 경로들은 사용하지 않습니다.

| # | ETF | URL | 점검 결과 |
|---|---|---|---|
| 14 | SOXX 보유 내역 CSV (상품 페이지 경로) | https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund | HTTP 200이지만 본문이 HTML(상품 페이지). 사용하지 않음 |
| 15 | IGV 보유 내역 CSV (상품 페이지 경로) | https://www.ishares.com/us/products/239771/ishares-north-american-tech-software-etf/1467271812596.ajax?fileType=csv&fileName=IGV_holdings&dataType=fund | HTTP 200이지만 본문이 HTML. 사용하지 않음 |
| 16 | QQQ 보유 내역 다운로드 (상품 페이지 경로) | https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0?audienceType=Investor&action=download&ticker=QQQ | HTTP 406 (브라우저 UA). 12번 API로 대체 |
| 17 | QQQ 상품 페이지 | https://www.invesco.com/qqq-etf/en/home.html | HTTP 200. 보유 내역 페이지 https://www.invesco.com/qqq-etf/en/holdings.html 은 HTTP 406 |

## 4. 과제 브리프에서 인용한 사실 중 원문을 읽지 않은 것

| 사실 | 브리프가 밝힌 출처 | 처리 |
|---|---|---|
| SOXX 기초지수 개명 (2023-11-03) | iShares summary prospectus | 2번 항목. methodology.md 3.1절에 "원문 확인 아니오"로 표시 |
| 2024년부터 2025년 사이 제3자 자료가 SOXX의 8% / 4% / 10% 상한을 여전히 인용 | 제3자 웹 자료 (URL 미제공) | methodology.md 3.2절 1항에 서술. 개별 URL은 없음 |
| SOXX 연간 재구성 효력일이 9월 셋째 금요일 종가 후 | 브리프 요약 (1번 문서에는 "9월에 재구성"이라고만 기재) | methodology.md 2.1절에 추론임을 명시 |
