# 대시보드 페이지 (클로드 아티팩트)

`dashboard.html`은 claude.ai 아티팩트로 발행된 대시보드의 소스입니다. 발행 주소는 https://claude.ai/artifact/Em7XgPghM7nNAmoHVCFczz 이며, 소유자 계정으로만 열립니다.

## 동작 방식

- 페이지는 열릴 때 `mcp` 런타임 기능으로 보는 사람의 GitHub 커넥터를 통해 저장소의 `reports/summary.json`, `reports/history.json`, `reports/briefing/latest.md`를 읽습니다(도구 `get_file_contents`). 처음 열 때 커넥터 사용 허용을 한 번 묻습니다.
- 커넥터를 쓸 수 없으면(허용 거부, 미연결, 다른 화면) 아티팩트에 함께 발행된 `data/` 사본을 보여주고 상단에 "발행 시점 사본"이라고 표시합니다.
- 탭: 보드(상한 게이지와 카운트다운), 노트(표 중심 정독), 트랙(이벤트 타임라인과 비중 경로 차트), 브리핑(`reports/briefing/latest.md`와 지난 브리핑 선택).

## 다시 발행할 때

디자인을 바꾼 경우에만 다시 발행합니다. 데이터는 페이지가 저장소에서 직접 읽으므로 매일 발행할 필요가 없습니다. 발행 시 선언할 항목:

- capabilities: `{"mcp": {"servers": [{"server": "github", "tools": ["get_file_contents"]}]}}`
- files: `data/summary.json`, `data/history.json`, `data/briefing.md` (저장소의 reports 파일 사본)

페이지는 외부 스크립트나 외부 주소를 쓰지 않습니다. 아티팩트 보안 정책상 외부 `fetch`는 차단되므로, 저장소 읽기는 반드시 커넥터 경로를 거쳐야 합니다.
