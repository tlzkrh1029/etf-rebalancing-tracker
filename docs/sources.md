# 출처 목록

접근일: 2026-10-09 (모든 항목 공통). "원문 확인" 열은 이 세션의 작업 컨테이너에서 해당 URL을 실제로 내려받아 내용을 읽었는지를 뜻합니다. 읽지 못한 문서는 과제 브리프가 제공한 사실만 인용했고, `docs/methodology.md` 3절에 "재검증 필요"로 표시했습니다.

## 1. 지수 방법론 문서

| # | 출처 | URL | 문서 일자 | 사용 목적 | 원문 확인 |
|---|---|---|---|---|---|
| 1 | iShares Trust, Form 497 보충서 "Important Notice Regarding Change in Investment Policy" (iShares PHLX Semiconductor ETF에서 iShares Semiconductor ETF로 변경, 기초지수를 PHLX Semiconductor Sector Index에서 ICE Semiconductor Index로 변경) | https://www.sec.gov/Archives/edgar/data/1100663/000119312521126827/d82642d497.htm | 보충서 2021-04-22, 변경 효력 2021-06-21 | SOXX 지수의 종목 선정 기준, 8% / 4% / ADR 10% 상한과 재분배 절차, 9월 연간 재구성과 3/6/12월 분기 리밸런스의 참조일과 발표일, 월간 10% 주식수 업데이트, 분기 중 종목 교체 없음 | 예 (HTML 전문 읽음) |
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

## 3. 보유 내역 데이터 소스 (추적기 입력, 참고)

`probe/summary.txt`(2026-10-09 12:42 UTC 실행)에 기록된 다운로드 가능성 점검 결과입니다. 방법론 문서 작성에는 사용하지 않았고, 추적기가 매일 읽을 데이터의 위치를 기록해 둡니다.

| # | ETF | URL | 점검 결과 |
|---|---|---|---|
| 10 | SOXX 보유 내역 CSV | https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund | HTTP 200. 다만 응답 본문이 HTML로 판별되어 파서에서 처리 필요 |
| 11 | IGV 보유 내역 CSV | https://www.ishares.com/us/products/239771/ishares-north-american-tech-software-etf/1467271812596.ajax?fileType=csv&fileName=IGV_holdings&dataType=fund | HTTP 200. 응답 본문이 HTML로 판별되어 파서에서 처리 필요 |
| 12 | QQQ 보유 내역 다운로드 | https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0?audienceType=Investor&action=download&ticker=QQQ | HTTP 406. 대체 경로 필요 |
| 13 | QQQ 상품 페이지 | https://www.invesco.com/qqq-etf/en/home.html | HTTP 200 (2026-10-09 이 세션에서 확인). 보유 내역 페이지 https://www.invesco.com/qqq-etf/en/holdings.html 은 probe에서 HTTP 406 |

## 4. 과제 브리프에서 인용한 사실 중 원문을 읽지 않은 것

| 사실 | 브리프가 밝힌 출처 | 처리 |
|---|---|---|
| SOXX 기초지수 개명 (2023-11-03) | iShares summary prospectus | 2번 항목. methodology.md 3.1절에 "원문 확인 아니오"로 표시 |
| 2024년부터 2025년 사이 제3자 자료가 SOXX의 8% / 4% / 10% 상한을 여전히 인용 | 제3자 웹 자료 (URL 미제공) | methodology.md 3.2절 1항에 서술. 개별 URL은 없음 |
| SOXX 연간 재구성 효력일이 9월 셋째 금요일 종가 후 | 브리프 요약 (1번 문서에는 "9월에 재구성"이라고만 기재) | methodology.md 2.1절에 추론임을 명시 |
