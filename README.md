# AI · Security Daily Digest

국내외 AI·보안·신기술 기사를 모아 Gemini로 한국어 요약을 작성하고, GitHub Pages에 날짜별 HTML 보고서로 게시합니다. 화면은 HTML·CSS·JavaScript, 수집·생성은 Python, 예약 실행은 GitHub Actions입니다.

**처음 설치하시면 [SETUP.md](SETUP.md)를 순서대로 따라 하세요. 브라우저용 안내서는 [SETUP.html](SETUP.html)입니다.**

## 동작

```text
KST 06:00 / 13:00 / 19:00 예약 또는 수동 실행
  → news-state 브랜치에서 이전 데이터 복구
  → 국내외 RSS·허용된 기사 목록·GitHub Trending 수집
  → URL/제목 중복 제거 + 원문 근거 확보
  → Gemini 한국어 요약·번역·분류
  → 오늘 누적된 전체 요약에서 Gemini HOT 10 선정
  → 최근 5일 HTML 재생성
  → news-state 저장 + public/만 GitHub Pages 배포
```

HTML 자체를 모델에게 맡기지 않습니다. Gemini는 구조화된 JSON만 반환하고 Python 템플릿이 이를 안전하게 HTML로 바꿉니다. 따라서 HOT은 **최종 HTML 작성 직전**에 오늘의 전체 누적 내용을 보고 선정합니다.

## 구현한 기능

| 항목 | 동작 |
|---|---|
| 첫 화면 | 최근 5일 보고서를 최신순으로 표시 |
| 일별 보고서 | `reports/YYYY-MM-DD.html` 한 개를 하루 세 번 누적 갱신 |
| 분류 | HOT / AI / 보안 / 신기술·논문 / GitHub 인기 |
| 카드 | 한국어 제목·요약·출처·시각·원문 링크·요약 근거 범위 |
| HOT | 모든 누적 후보에서 Gemini가 중요도순 최대 10개 선정. 분야별 카드와 중복 표시 |
| GitHub | Trending의 `stars today` 내림차순. 총 스타로 대체하지 않음 |
| 중복 처리 | URL 추적 매개변수 정리, 같은 URL과 정규화된 동일 제목 중복 억제 |
| 실패 처리 | 보류 목록, 수집원별 상태, 이전 보고서 보존, HOT 재선정 실패 표시 |
| 비용 제어 | 신규 기사 중심 요약, 저장소 설명 캐시, 호출 간격, 재시도 포함 실행당 호출 상한 |
| 모바일 | 좁은 커버 화면 1열, 내부 화면 조건에서 2열, 회전·분할 화면에 반응 |
| 편의 기능 | 분야 이동, 읽기 진행률, 보고서 내 검색, 이 브라우저 저장, 공유 |
| 보관 | 한국시간 오늘 포함 5개 날짜만 현재 사이트와 최신 상태에 유지 |

처리할 적격 이슈가 부족하거나 API가 실패하면 HOT을 가짜 기사로 10개까지 채우지 않습니다. 충분한 서로 다른 이슈가 있으면 10개, 부족하면 실제 개수와 사유를 표시합니다.

## 포함한 수집원

`config/sources.json`의 14개 뉴스·논문 수집원과 GitHub Trending 1개입니다.

| 지역 | 수집원 |
|---|---|
| 국내 | AI타임스, 데일리시큐, 전자신문, GeekNews, 보안뉴스 |
| 해외 AI | OpenAI, Google AI, Hugging Face |
| 해외 보안 | The Hacker News, BleepingComputer, SecurityWeek, Krebs on Security |
| 논문 | arXiv cs.AI, arXiv cs.CR |
| 저장소 | GitHub Trending |

이는 **설정한 수집원 목록**입니다. 모든 사이트의 실시간 접근 성공을 보장하거나 인터넷 전체 기사를 수집한다는 의미가 아닙니다. 사이트 정책·RSS 주소·HTML 구조·Actions IP 차단에 따라 일부가 실패할 수 있습니다. 수집 실패는 숨기지 않고 사이트에 표시합니다. `python -m digest --check-sources`로 사용 환경에서 확인할 수 있습니다.

보안뉴스는 RSS 주소 대신 기사 목록 HTML 어댑터를 사용합니다. 본문 수집이 제한되면 충분한 RSS 제공문으로 요약하고, 제목만 있는 자료는 근거가 부족하므로 보류합니다. 논문은 PDF 전체가 아닌 RSS 초록을 요약합니다.

## 기본 설정

`config/settings.json`에서 변경합니다.

| 설정 | 기본값 | 의미 |
|---|---:|---|
| `keep_days` | 5 | 오늘을 포함한 KST 달력 날짜 수 |
| `lookback_hours` | 48 | 새로 가져올 기사의 발행 시각 범위 |
| `summary_model` | `gemini-3.5-flash-lite` | 번역·요약·분류 |
| `hot_model` | `gemini-3.8-flash` | HOT 선정 |
| `batch_size` | 6 | 요약 요청 한 번에 보내는 기사 수 |
| `max_new_articles_per_run` | 0 | 기사 수 별도 제한 없음. API 상한은 별개 |
| `max_api_calls_per_run` | 80 | 재시도·HOT 포함 요청 수 상한 |
| `api_interval_seconds` | 5 | 요청 시작 사이의 최소 간격 |
| `max_input_chars_per_article` | 6000 | 기사당 모델에 보내는 최대 글자 수 |
| `fetch_article_body` | true | 허용된 경우 본문 일부 확보 |
| `github_max_items` | 25 | Trending 화면에서 읽는 최대 저장소 수 |

API 모델은 저장소 Variables의 `GEMINI_MODEL`, `GEMINI_HOT_MODEL`로 덮어쓸 수도 있습니다. 모델 제공 여부와 할당량은 사용 프로젝트에서 확인해야 합니다. 호출 수 제한은 **금액의 절대 상한이 아닙니다**. 긴 기사와 HOT 후보 수에 따라 입력 토큰이 늘어납니다.

보류된 기사는 다음 실행에서 다시 시도하지만, 발행 후 48시간을 넘기거나 5일 보관 범위를 벗어나면 처리 대상에서 빠집니다. API 할당량이 계속 부족하면 모든 수집 기사를 요약할 수 없습니다.

## 폴더 구조

```text
ai-security-digest/
├── .github/workflows/
│   ├── update-news.yml       # 예약·수동 실행, 상태 보존, Pages 배포
│   └── test.yml              # 코드·브라우저 테스트
├── assets/
│   ├── style.css            # 첨부 디자인 기반, 폴드 반응형
│   ├── app.js               # 검색·저장·공유·진행률
│   └── favicon.svg
├── config/
│   ├── settings.json        # 모델·요청 제한 등
│   └── sources.json         # 국내외 수집원
├── digest/
│   ├── __main__.py          # 실행 명령
│   ├── common.py            # KST·URL·파일 처리
│   ├── network.py           # robots·호스트 제한·네트워크 요청
│   ├── sources.py           # RSS/HTML/Trending 해석
│   ├── gemini.py            # Gemini Interactions + JSON 검증
│   ├── pipeline.py          # 누적·중복·재시도·HOT·보관
│   └── render.py            # 정적 HTML 생성
├── templates/
│   ├── base.html            # 공통 머리글·하단 메뉴
│   ├── index.html           # 보고서 목록
│   ├── report.html          # 보고서 양식
│   ├── macros.html          # 카드·갱신 현황
│   ├── icons.html
│   └── not-found.html
├── scripts/state_branch.py # news-state 복구·저장
├── tests/                   # 외부 접속 없는 검증 자료 포함
├── docs/
│   ├── DESIGN.md            # 폴드 CSS 설명
│   ├── OPERATIONS.md        # 보관·오류·보안·제약
│   └── TESTING.md            # 검증 범위와 미검증 항목
├── public/                  # 실제 배포 대상. 초기에는 빈 목록
├── requirements.txt
├── requirements-dev.txt
├── .env.example             # 변수 이름 예시. 실제 키 없음
├── .gitignore
├── README.md
├── SETUP.md                 # 처음부터 배포하기
└── SETUP.html               # 같은 안내서의 브라우저용 버전
```

`state/`는 실행 중 생기는 디렉터리이므로 배포 ZIP에는 없습니다. Actions에서는 별도 `news-state` 브랜치의 `state.json`을 worktree로 연결합니다. `main`에는 프로그램, `news-state`에는 수집 상태를 보관하고, Pages에는 `public/`의 HTML·CSS·JS만 올립니다.

## 로컬에서 화면만 확인

```bash
python -m pip install -r requirements.txt
python -m digest --build-only
python -m http.server 8000 --directory public
```

브라우저에서 `http://localhost:8000`을 엽니다. 수집 전에는 빈 목록이 정상입니다. 파일을 열거나 화면을 새로고침하는 것만으로 Gemini 요청이 발생하지 않습니다.

## 테스트

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m playwright install chromium
python tests/browser_check.py
```

테스트 데이터에는 실제 뉴스가 아니라는 표시가 있으며 `public/`에 게시되지 않습니다. 이 전달본의 실제 검증 범위는 [docs/TESTING.md](docs/TESTING.md)를 참고하세요.

## 운영 전 확인할 사항

GitHub 예약 실행은 지연·누락될 수 있습니다. 5일이 지난 콘텐츠는 다음 성공한 생성·배포에서 서버에서 제거됩니다. **Git 커밋 이력, 과거 아티팩트, 별도 백업까지 영구 삭제하는 기능은 아닙니다.** 공개 저장소의 `news-state` 브랜치도 공개됩니다. 웹페이지 전체 HTML은 저장하지 않지만, 보류 항목의 RSS 제공문(기본 최대 6,000자)·URL과 완료된 요약은 저장됩니다.

출처의 지시문을 신뢰하지 않는 데이터로 취급하고 모델 출력은 검증·이스케이프합니다. 그렇더라도 번역·요약의 오류를 완전히 제거하지 못합니다. 보안 패치·정책·수치 등 중요한 판단은 원문으로 확인해야 합니다.
