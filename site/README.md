# 대시보드 페이지

`index.html`은 GitHub Pages로 배포되는 대시보드의 완성본이고, `dashboard.html`은 같은 내용을 claude.ai 아티팩트용 조각으로 만든 것입니다(현재는 Pages 배포본이 기준이며 아티팩트는 갱신하지 않습니다).

## 동작 방식

- `.github/workflows/pages.yml`이 `site/index.html`과 `reports/`(summary.json, history.json, latest.json, briefing/*.md, 날짜 목록 index.json)를 묶어 GitHub Pages에 배포합니다. site나 reports를 바꾸는 푸시마다 실행되고, 일일 수집 워크플로가 데이터를 커밋한 뒤에도 호출합니다.
- 페이지는 같은 출처의 `./reports/...` 파일을 읽으므로 커넥터나 허용 창이 필요 없고, 배포가 끝나면 열 때마다 최신 데이터를 보여줍니다. 상단 상태줄에 데이터 기준 시각이 표시됩니다.
- 탭: 보드(상한 게이지와 카운트다운), 노트(표 중심 정독), 트랙(이벤트 타임라인과 비중 경로 차트), 브리핑(`reports/briefing/latest.md`와 지난 브리핑 선택).

## 처음 한 번 할 일

저장소 설정의 Pages 메뉴에서 Build and deployment의 Source를 "GitHub Actions"로 고릅니다. 워크플로의 configure-pages 단계가 자동으로 켜 주기도 하지만, 권한이 없으면 수동으로 켜야 합니다. 주소는 `https://<owner>.github.io/etf-rebalancing-tracker/` 형태입니다.

## 소스 수정

페이지 소스는 이 폴더의 `index.html` 하나로 완결됩니다. 외부 스크립트나 외부 주소를 쓰지 않으며, 차트는 인라인 SVG입니다.
