# v2 검증 결과

Python 단위·통합 테스트 91개 통과. `node --check assets/app.js`, `python -m compileall -q digest tests`, 빈 상태의 `--build-only` HTML 생성 통과.

`python tests/browser_check.py --in-memory`로 12개 화면 크기 × 목록·보고서 24개 배치 검사와 CVE 더 보기·숨겨진 카드 검색·열 수·넘침·세부정보 동작을 확인했습니다. 결과는 `v2-ui-results.json`에 있습니다. 이는 메모리 주입 시험이며 로컬 스토리지는 모의 객체입니다.

원본 HTTP·DNS·실제 API·GitHub 배포·실기기 검증은 수행하지 못했습니다. localhost 브라우저 탐색은 환경의 `ERR_BLOCKED_BY_ADMINISTRATOR`로 막혔습니다. 실제 네트워크/CSP/스토리지 영속성/JS 비활성 모드 검증 성공으로 해석하지 마세요.

아래는 v1 검증 기록입니다.

# 전달 전 검증 기록

검증 날짜: **2026-09-22**.

## 실제 실행하여 통과한 검증

### Python 단위·통합 테스트: 40개

`python -m pytest -q`를 실행했습니다. 전달 환경의 Python 버전은 3.13입니다. Actions는 Python 3.12를 사용하도록 작성되어 있으므로 실제 GitHub 실행에서 3.12 환경을 추가 확인해야 합니다.

검증 범위는 URL 정규화와 위험한 스킴 제외, KST 날짜 경계, RSS/Atom/RDF 파싱, 한국어 EUC-KR 피드, XML 외부 엔티티 차단, HTML 기사 목록, 최근 시간 범위, GitHub 일간 지표 파싱, 요약·HOT 응답 검증, API 재시도·요청 상한, 세 번 실행 시 같은 보고서 누적, 중복 기사의 보류 목록 처리, 실패 후 이전 결과 보존, 5일 만료, 손상 상태 감지, HTML 이스케이프, 상대 링크와 빈 초기 화면입니다.

Gemini API 계약 검증은 가짜 응답을 넣어 엔드포인트·인증 헤더·요청 스키마·`store:false`·JSON 해석 등을 확인했습니다. **Google 서버에 실제 요청을 보낸 검증은 아닙니다.**

상태 보존은 로컬 bare Git 저장소를 원격처럼 사용해 두 개의 독립 작업 디렉터리에서 시험했습니다. 첫 실행에서 `news-state`를 만들고, 다음 실행에서 복구·추가 저장하며, `main` 소스가 덮어쓰이지 않고 상태 브랜치에는 `state.json`만 들어가는 것을 확인했습니다. **GitHub의 권한·토큰·실제 원격 배포를 시험한 것은 아닙니다.**

### 반응형 화면: 12개 viewport × 2페이지 = 24개 확인

Headless Chromium에서 아래 CSS viewport의 목록과 보고서를 렌더링했습니다.

```text
320×800    344×882    360×900    390×844
412×915    600×700    690×829    768×900
840×980    900×700    1024×768   1440×1000
```

각 크기에서 문서 가로 넘침이 없는지, 커버 1열·내부 조건 2열·데스크톱 2열인지, 고정 헤더·하단 메뉴가 너비를 벗어나지 않는지 확인했습니다. 한국어 긴 제목과 긴 영문 저장소 이름을 사용했습니다. 390px와 768px 화면의 스크린샷을 열어 배치도 확인했습니다.

추가로 검색·검색 결과 없음·검색 초기화, 저장 버튼과 저장 목록, 공유 API의 모의 호출, 리사이즈, 정적 내부 링크를 검사했습니다. 테스트 콘텐츠에는 `[화면 검증]`, `실제 뉴스 아님`을 명시했고 공개 배포 폴더에 넣지 않았습니다.

### 브라우저 검증의 제한

전달 환경의 관리 정책이 Chromium의 HTTP·파일 URL 탐색을 막았습니다. 따라서 이 환경에서는 `--in-memory` 모드로 생성된 HTML·CSS·JS를 메모리에 주입해 시험했습니다. 이 모드에서는 테스트 문서의 CSP를 제거하고 localStorage와 공유 API를 모의 구현했습니다.

**실제 HTTP 탐색, 브라우저의 실제 localStorage 영속성, 배포된 CSP 동작, 실제 휴대폰 공유 시트, 네트워크 하의 JavaScript 비활성화 모드는 검증하지 않았습니다.** CSS 배치와 함수 동작 확인을 실제 사이트의 완전한 종단 간 검증으로 표현하지 않습니다.

`tests/browser_check.py`의 기본 모드는 로컬 HTTP 서버를 사용하며, 실제 탐색과 JavaScript 비활성화 읽기도 검사합니다. `test.yml`은 이 기본 모드를 GitHub runner에서 실행하도록 구성했습니다. 그 결과는 첫 업로드 후 Actions의 **Tests** 실행에서 확인해야 합니다.

## 수행하지 않은 검증

실제 Gemini 키 요청, 각 수집원의 실시간 응답과 본문 추출 정확도, 번역·요약 품질과 HOT 판단 품질, GitHub 계정에서의 상태 브랜치 push 권한, Pages 실배포, 예약 실행 시각, 갤럭시 폴드 8 울트라 실기기 동작은 수행하지 않았습니다.

수집원 URL과 API 형식은 공식 문서·공개 웹 자료를 참고해 구성했지만, 개발 컨테이너의 외부 네트워크가 제한되어 전체 수집 파이프라인을 실시간으로 실행하지 못했습니다. 첫 `collect` 실행과 `--check-sources`, `--check-api` 진단으로 사용 환경에서 확인해야 합니다.

## 재현 명령

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m playwright install chromium
python tests/browser_check.py
```

인터넷·탐색이 제한된 개발 환경에서 레이아웃과 일부 함수만 확인하는 별도 명령:

```bash
python tests/browser_check.py --in-memory --screenshots .qa
```

테스트와 실제 기사 수집은 별도입니다. 테스트 데이터로 채운 보고서를 운영 사이트에 올리지 마세요.
