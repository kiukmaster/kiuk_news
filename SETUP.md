> **v2 변경사항:** 첫 설정은 이 안내서를 따릅니다. 수집 대체 경로, CVE 날짜 기준, 선택 사항인 NVD 키는 [UPGRADE.md](UPGRADE.md)에 있습니다.

# 처음부터 설정하기

**목표: API 키 등록 → GitHub 저장소 업로드 → Actions 실행 → Pages에서 확인.**

이 안내서는 2026년 9월 22일 확인한 공식 문서와 이 프로젝트의 코드에 맞춰 작성했습니다. 실제 키와 GitHub 계정은 사용자가 설정해야 합니다. 키를 채팅, HTML, 저장소 파일에 넣지 마세요.

## 0. 준비와 용어

필요한 것은 Google 계정, GitHub 계정, 그리고 프로젝트 ZIP입니다. GitHub에서 자동 운영만 하려면 PC에 Python을 설치할 필요는 없습니다.

| 용어 | 이 프로젝트에서 하는 일 |
|---|---|
| Repository, 저장소 | 프로그램 파일을 올리는 장소 |
| Actions | GitHub에서 Python 수집 프로그램을 실행하는 기능 |
| Workflow | 언제 어떤 명령을 실행할지 적은 YAML 파일 |
| Secret | API 키처럼 공개하면 안 되는 값을 보관하는 설정 |
| Variable | 모델 이름처럼 공개되어도 되는 설정 |
| Pages | 만들어진 HTML·CSS·JS를 웹사이트로 제공하는 기능 |
| main | 프로그램 소스를 보관할 기본 브랜치 |
| news-state | 이전 실행의 보고서·요약·중복 판별 정보를 보존할 브랜치 |

설정은 **본인이 관리자 권한을 가진 저장소**에서 진행하세요. 기관·조직 저장소는 관리자가 Actions·Pages·키 생성을 제한할 수 있습니다.

## 1. Gemini API 키 만들기

1. [Google AI Studio API Keys](https://aistudio.google.com/api-keys)에 접속하고 Google 계정으로 로그인합니다.
2. 처음 이용한다면 약관과 프로젝트 생성 안내를 확인합니다. 기존 프로젝트를 사용할 경우 대시보드의 **Projects → Import projects**에서 가져온 후 진행합니다.
3. **Create API key**를 누르고 사용할 프로젝트를 선택합니다. 용도를 알아볼 수 있도록 이름을 `ai-security-digest`로 지정해도 됩니다.
4. 생성한 키를 복사합니다. 지금은 GitHub Secret에 넣을 때까지만 안전하게 보관합니다.
5. 키 목록의 **Key Type**이 **Auth**인지 확인합니다. 공식 안내상 2026년 9월에는 기존 Standard 키 거부 전환이 예정되어 있으므로, 예전에 발급한 키보다 AI Studio에서 새로 만든 Auth 키를 사용하세요.

키 생성 권한 오류가 나면 프로젝트 소유자나 조직 관리자에게 문의하세요. 이미 쓰고 있는 다른 서비스의 키 설정을 임의로 바꾸지 말고 이 프로젝트용 키를 별도로 만드는 편이 관리하기 쉽습니다.

**API 사용료와 호출 제한은 별도입니다.** AI Studio에서 해당 프로젝트의 모델 사용 가능 여부, 한도, 사용량 및 결제 상태를 확인하세요. Google 계정이 있다고 해서 모든 모델·모든 요청이 무료인 것은 아닙니다. 이 프로그램의 호출 상한도 금액을 확정적으로 제한하지는 않습니다.

참고: [공식 API 키 안내](https://ai.google.dev/gemini-api/docs/api-key), [모델 목록](https://ai.google.dev/gemini-api/docs/models), [사용 한도](https://ai.google.dev/gemini-api/docs/rate-limits), [가격 안내](https://ai.google.dev/gemini-api/docs/pricing).

## 2. GitHub 저장소 만들기

1. GitHub에 로그인한 뒤 우측 상단 **+ → New repository**를 누릅니다.
2. **Repository name**에 `ai-security-digest`를 입력합니다. 다른 이름을 써도 됩니다.
3. 처음 설정할 때는 **Public**을 선택하는 경로로 진행합니다. GitHub Free에서 공개 저장소의 Pages를 사용하는 구성입니다. 비공개 저장소의 Pages 지원은 사용 중인 요금제와 정책을 별도로 확인하세요.
4. **Add a README file**, `.gitignore`, License의 자동 추가는 선택하지 않아도 됩니다. 프로젝트에 필요한 파일이 들어 있습니다.
5. **Create repository**를 누릅니다.

공개 저장소에서는 프로그램 코드뿐 아니라 자동으로 생성되는 `news-state` 브랜치도 공개됩니다. 개인정보·비밀 문서 등을 수집원으로 넣는 용도로 이 구성을 사용하지 마세요.

참고: [GitHub Pages 소개](https://docs.github.com/en/pages/getting-started-with-github-pages/about-github-pages).

## 3. 프로젝트 파일 올리기

ZIP을 압축 해제한 뒤 **`ai-security-digest` 폴더 안의 내용**을 저장소 최상위에 올립니다. ZIP 파일 자체를 업로드해서는 동작하지 않습니다.

업로드 후 저장소 최상위가 다음과 같아야 합니다.

```text
.github/
assets/
config/
digest/
templates/
scripts/
tests/
public/
requirements.txt
README.md
SETUP.md
...
```

다음처럼 한 겹 더 들어가면 Actions가 인식하지 못합니다.

```text
저장소/
└── ai-security-digest/
    └── .github/workflows/update-news.yml   ← 잘못된 위치
```

### 방법 A. 웹에서 업로드

빈 저장소 화면의 **uploading an existing file**을 누르거나, 파일이 있는 저장소에서는 **Add file → Upload files**를 누릅니다. 압축 해제한 내부 파일과 폴더를 모두 끌어다 놓고 **Commit changes**를 누릅니다.

`.github` 폴더가 빠지지 않았는지 반드시 확인합니다. Windows 탐색기에서 폴더를 볼 수 없다면 **보기 → 표시 → 숨긴 항목**을 켭니다. GitHub에서 다음 두 경로를 실제로 열 수 있어야 합니다.

```text
.github/workflows/update-news.yml
.github/workflows/test.yml
```

웹 업로드가 `.github` 또는 워크플로 파일을 거부하는 환경에서는 아래 Git 방식으로 업로드하세요. 웹 업로드만 반복하면서 폴더 위치를 바꾸지 마세요.

### 방법 B. Git으로 업로드

Git이 설치되어 있다면 압축 해제한 프로젝트 폴더에서 터미널을 엽니다. 아래 주소는 **본인이 만든 저장소의 HTTPS 주소**로 교체합니다. 실제 API 키는 어떤 명령에도 넣지 않습니다.

```bash
git init
git add .
git commit -m "Initial AI security digest"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_ID/ai-security-digest.git
git push -u origin main
```

작성자 정보가 없다는 오류가 나면 해당 폴더에서 다음을 설정한 후 `git commit`부터 다시 실행합니다.

```bash
git config user.name "본인의 GitHub 표시 이름"
git config user.email "본인의 커밋용 이메일"
```

인증 창이 뜨면 GitHub 로그인 절차를 따르세요. 계정 비밀번호나 인증 토큰을 코드에 적지 않습니다. 기존에 파일이 있는 저장소에 무작정 `--force`로 밀어 넣지 마세요.

참고: [로컬 코드 GitHub에 추가](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github).

## 4. GEMINI_API_KEY를 Secret에 등록하기

1. 방금 만든 저장소를 엽니다. **계정 설정이 아니라 해당 저장소의 Settings**로 이동합니다.
2. 왼쪽 메뉴에서 **Secrets and variables → Actions**를 누릅니다.
3. **Secrets** 탭에서 **New repository secret**를 누릅니다.
4. **Name**에 아래 이름을 정확히 입력합니다.

```text
GEMINI_API_KEY
```

5. **Secret**에 AI Studio에서 복사한 실제 API 키를 붙여넣습니다. 따옴표나 설명 문구를 추가하지 않습니다.
6. **Add secret**를 누릅니다. 목록에 `GEMINI_API_KEY`가 나타나면 등록된 것입니다. 다시 열어도 키 원문이 보이지 않는 것이 정상입니다.

**Variables 탭에 키를 넣지 마세요.** `.env`, `settings.json`, HTML, JavaScript, `update-news.yml`에도 키를 적지 않습니다.

코드에는 이미 다음 참조가 연결되어 있습니다. 사용자가 이 줄을 실제 키로 교체하는 것이 아닙니다.

```yaml
env:
  GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
```

2026년 9월부터 Gemini의 기존 Standard 키 요청이 거부됩니다. AI Studio에서 키 유형이 **Auth**인지, 차단된 키는 아닌지 확인하고, 401 오류가 나면 새 Auth 키를 만들어 `GEMINI_API_KEY` Secret을 교체하세요. 변경을 기본 브랜치에 반영한 후 **Actions → Update news & deploy → Run workflow → Branch: main → mode: check-api**로 두 모델의 연결을 확인합니다. 이 모드는 Gemini 요청을 보내지만 뉴스 수집·상태 저장·Pages 배포는 하지 않습니다. 성공하면 `mode: collect`를 실행하세요. 키 값은 채팅이나 저장소에 붙여넣지 마세요.

키를 잘못 넣었다면 Secret의 수정 버튼으로 값을 교체합니다. 실제 키가 공개 파일에 올라갔다면 파일 삭제만 하지 말고 AI Studio에서 해당 키를 폐기하고 새 키를 등록하세요.

참고: [GitHub Actions Secret 사용](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).

## 5. 모델 설정 확인하기

처음에는 모델 설정을 따로 만들지 않아도 됩니다. 프로젝트 기본값은 다음과 같습니다.

| 역할 | 모델 |
|---|---|
| 번역·요약·분류 | `gemini-3.5-flash-lite` |
| HOT 선정 | `gemini-3.8-flash` |

사용 프로젝트에서 해당 모델이 제공되지 않거나 다른 모델로 운영하려면 **Settings → Secrets and variables → Actions → Variables → New repository variable**에서 설정합니다.

| Name | Value 예시 |
|---|---|
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` |
| `GEMINI_HOT_MODEL` | `gemini-3.8-flash` |

모델 이름은 API에서 사용하는 정확한 ID여야 합니다. 웹 서비스 화면의 표시 이름을 임의로 넣으면 안 됩니다. 이 구현은 **Interactions API와 구조화된 JSON 출력**을 사용하므로 이를 지원하는 모델을 선택합니다.

참고: [Gemini 모델 목록](https://ai.google.dev/gemini-api/docs/models), [구조화된 출력](https://ai.google.dev/gemini-api/docs/structured-output), [Interactions API](https://ai.google.dev/gemini-api/docs/interactions-overview).

## 6. Actions 권한 확인하기

저장소 **Settings → Actions → General**로 이동합니다.

**Actions permissions**에서 GitHub 공식 Actions를 실행할 수 있어야 합니다. 개인 저장소에서는 기본 허용 설정으로 진행할 수 있습니다. 조직 정책이 제한 중이면 필요한 공식 Actions를 허용하도록 관리자와 확인하세요. 임의의 외부 Action은 사용하지 않습니다.

페이지 아래쪽 **Workflow permissions**에서 기본 쓰기 권한을 허용할 수 있는 환경인지 확인합니다. 제공된 YAML은 빌드 작업에 `contents: write`, 배포 작업에 `pages: write`, `id-token: write`를 필요한 범위로 명시합니다. 개인 저장소에서 `news-state` 쓰기가 거절된다면 **Read and write permissions** 설정과 저장소 Rulesets를 확인하세요.

GitHub 자체 예약 실행만 쓰는 경우 별도의 `GH_TOKEN`, 개인 액세스 토큰(PAT), GitHub API 키를 만들 필요는 없습니다. 외부 예약 서비스에서 `workflow_dispatch`를 호출하려면 아래 10절의 제한된 권한 토큰이 필요합니다. 실행마다 제공되는 `GITHUB_TOKEN`과 체크아웃 자격증명을 사용합니다. **Allow GitHub Actions to create and approve pull requests**는 이 프로젝트에 필요하지 않습니다.

브랜치 보호 규칙이 모든 브랜치 쓰기를 막고 있다면 `news-state`에 대한 허용 범위를 검토하세요. 코드가 보호 규칙을 우회하거나 강제 푸시하지는 않습니다.

참고: [GITHUB_TOKEN 권한](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication), [저장소 Actions 설정](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).

## 7. GitHub Pages 설정하기

1. 저장소의 **Settings → Pages**로 이동합니다.
2. **Build and deployment** 영역에서 **Source**를 찾습니다.
3. **GitHub Actions**를 선택합니다.

`Deploy from a branch`, `main /docs`, `gh-pages` 방식으로 바꾸지 마세요. 이 프로젝트는 제공된 워크플로가 `public/` 디렉터리만 업로드하는 구성입니다.

첫 배포 전에는 사이트 주소가 아직 안 보이거나 접속 시 404가 날 수 있습니다. 다음 단계에서 최초 배포를 완료해야 합니다.

참고: [Pages 배포 소스 설정](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site), [사용자 정의 Pages 워크플로](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).

## 8. 첫 수집을 수동 실행하기

1. 저장소 상단의 **Actions** 탭을 엽니다.
2. 왼쪽에서 **Update news & deploy**를 선택합니다. **Tests**는 검증용이므로 사이트를 배포하지 않습니다.
3. 오른쪽 **Run workflow**를 누릅니다.
4. **Branch: main**, **mode: collect**를 선택합니다.
5. 다시 **Run workflow**를 누릅니다.
6. 실행 항목을 열고 `build`와 `deploy` 작업을 확인합니다. 소요 시간은 기사 수, 사이트 응답, 모델 응답에 따라 달라집니다.

진행 순서는 다음과 같습니다.

```text
Checkout source
Set up Python
Install dependencies
Check secret configuration
Restore persistent data
Collect, summarize, select HOT, generate HTML
Save persistent data
Configure Pages
Upload generated site only
Wait for scheduled publication time (예약 실행만)
Deploy Pages
```

**Save persistent data**가 처음 성공하면 `news-state` 브랜치가 자동으로 생깁니다. 직접 만들거나 빈 파일을 넣을 필요가 없습니다. 이 브랜치를 삭제하면 보고서 누적·중복 판별 상태가 사라집니다.

일부 수집원이나 요약이 실패해도, 완료한 기사로 사이트를 게시할 수 있습니다. 따라서 Actions가 초록색이라는 것만으로 모든 기사가 성공한 것은 아닙니다. 실행의 **Summary**, 보고서의 **수집 안내**, **수집원별 상태**, **요약 보류**도 확인하세요.

배포 워크플로에서 테스트 실행은 생략하고, 별도 `Tests` 워크플로가 main 푸시와 PR에서 테스트합니다.

수동 실행은 `수동 1회`처럼 별도로 표시됩니다. 오전 예약을 기다리지 않고 실행했다고 해서 `06:00 완료`로 가짜 표시하지 않습니다.

## 9. 사이트 열기

`deploy`가 성공하면 배포 작업의 URL을 클릭하거나 **Settings → Pages → Visit site**에서 접속합니다.

일반 프로젝트 저장소의 주소 형태는 다음과 같습니다. 실제 주소는 GitHub가 표시하는 것을 사용하세요.

```text
https://YOUR_GITHUB_ID.github.io/ai-security-digest/
```

첫 화면에서 날짜별 보고서가 보이고, 카드를 누르면 해당 날짜의 보고서가 열립니다. 페이지에는 다음 정보가 있어야 합니다.

| 확인 항목 | 정상 동작 |
|---|---|
| 카드 | 실제 수집된 제목·한국어 요약·출처·원문 링크 |
| HOT | Gemini가 고른 이슈와 선정 이유 |
| 갱신 | 실제 실행한 예약 슬롯 또는 수동 실행 횟수 |
| 수집 상태 | 성공한 수집원과 실패·보류 사유 |
| 인기 | `stars today` 수치와 관측 시각 |
| 목록 | 동일한 날짜가 한 번만 표시 |

수집 전에는 빈 목록이 정상입니다. 샘플 기사와 가짜 완료 상태를 넣지 않았습니다. API 키가 틀리거나 모든 근거가 부족하면 첫 보고서가 생기지 않을 수 있으니 수집 상태와 Actions 로그를 확인하세요.

## 10. 하루 세 번 자동화 확인하기

첫 설정 이후 기본 브랜치에 워크플로가 있으면 예약 실행을 사용합니다.

| 목표 공개 시각(KST) | 수집 시작(KST) | 워크플로의 UTC cron |
|---|---|---|
| 06:00 | 05:07 | `7 20 * * *` — UTC 전날 20:07 |
| 13:00 | 12:07 | `7 3 * * *` |
| 19:00 | 18:07 | `7 9 * * *` |

각 실행은 같은 KST 날짜의 `reports/YYYY-MM-DD.html`을 갱신합니다. 오전 기사를 지우고 오후 기사로만 교체하는 방식이 아니라, 이미 요약한 기사에 새 기사를 추가하고 HOT을 다시 선정합니다.

수집·요약·HTML 생성은 목표 시각보다 53분 전에 시작하며, 예약 실행의 Pages 배포는 목표 시각까지 대기합니다. 수동 실행은 바로 배포합니다. **정확한 게시 시각은 보장하지 않습니다.** GitHub 부하에 따라 지연되거나 누락될 수 있으며, 예약은 기본 브랜치에서 실행됩니다. 공개 저장소는 60일간 저장소 활동이 없으면 예약 워크플로가 비활성화될 수 있으므로 장기간 운영 시 확인해야 합니다.

시간을 변경할 때는 `update-news.yml`의 cron·배포 게이트·외부 예약 호출 시각뿐 아니라 `digest/common.py`의 `SLOTS`, `CRON_SLOTS`, `templates/index.html`의 시간 안내도 함께 변경해야 표시가 일치합니다. 처음에는 기본값을 유지하세요.

참고: [GitHub schedule 이벤트](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

### 정시성이 중요할 때: 외부 예약 호출

이 저장소의 최근 GitHub 기본 예약 실행은 예정 시각보다 수 시간 늦게 시작했습니다. 비정각 선행 예약은 지연 가능성을 낮추지만, 정시성이 중요하면 비밀 헤더를 보관할 수 있는 별도 HTTPS 예약 서비스에서 GitHub의 `workflow_dispatch` API를 호출하세요. 외부 서비스도 실행 지연 가능성이 있으며 Pages 반영 시간까지 정확히 보장할 수는 없습니다.

1. GitHub에서 **이 저장소만 선택**하고 **Actions: write**만 허용한 fine-grained 개인 액세스 토큰을 만듭니다. 토큰은 외부 예약 서비스의 비밀 저장소에 보관하고 URL·웹페이지·저장소에 넣지 않습니다.
2. 외부 예약 서비스에 다음 HTTPS POST 요청을 세 개 등록합니다. `OWNER/REPO`와 `ref`는 실제 저장소·기본 브랜치로 바꿉니다. 각 요청은 목표보다 53분 앞선 **05:07, 12:07, 18:07 KST**에 보냅니다.

```text
POST https://api.github.com/repos/OWNER/REPO/actions/workflows/update-news.yml/dispatches
Authorization: Bearer <제한된 GitHub 토큰>
Accept: application/vnd.github+json
Content-Type: application/json
```

요청 본문은 순서대로 다음과 같습니다.

```json
{"ref":"main","inputs":{"mode":"collect","scheduled_slot":"06:00"}}
{"ref":"main","inputs":{"mode":"collect","scheduled_slot":"13:00"}}
{"ref":"main","inputs":{"mode":"collect","scheduled_slot":"19:00"}}
```

3. 한 슬롯을 먼저 시험하고 Actions의 `build`와 `deploy`가 완료되는지 확인합니다. 지정 슬롯의 배포는 목표 시각 전이면 대기하고, 목표가 지났으면 즉시 진행합니다. 목표보다 1시간 넘게 이른 호출은 게시를 막습니다.
4. 외부 예약이 정상 동작하면 저장소 **Settings → Secrets and variables → Actions → Variables**에 `EXTERNAL_SCHEDULER_ENABLED=true`를 추가해 GitHub 기본 예약 실행을 건너뛸 수 있습니다. 값을 설정하지 않으면 기본 예약도 계속 실행되어 같은 슬롯에 수집·배포가 중복될 수 있습니다. 이 변수는 외부 예약을 확인한 후에만 설정하세요.

참고: [GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event), [GitHub 예약 실행 지연](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows), [GitHub Pages 반영 시간](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site).

## 11. API 호출 없이 다시 배포하기

디자인을 수정했거나 기존 데이터로 HTML만 다시 만들 때 사용합니다.

**Actions → Update news & deploy → Run workflow → mode: rebuild → Run workflow**

이 모드는 `news-state`에서 기존 데이터를 읽고 최근 5일 범위로 HTML을 재생성합니다. 새 기사를 수집하거나 Gemini를 부르지 않습니다. 초기 데이터가 없다면 빈 목록만 게시됩니다.

소스 파일을 수정한 뒤 `main`에 올렸다고 해서 즉시 새 화면이 배포되는 것은 아닙니다. 코드 업로드는 테스트 워크플로를 실행합니다. 즉시 반영하려면 `rebuild`를 실행하고, 그렇지 않으면 다음 예약 수집 때 반영됩니다.

## 12. 수집 범위와 API 호출량 조정하기

### 사이트 추가·제외

`config/sources.json`을 수정합니다. 필요 없는 수집원은 `enabled`를 `false`로 바꿉니다. 새 주소를 넣을 때는 RSS/HTML 형식, 본문 선택자와 허용 도메인 `hosts`를 함께 확인해야 합니다.

RSS·본문 요청은 robots.txt와 사이트 제한을 따릅니다. 로그인, 유료벽, CAPTCHA를 우회하지 않습니다. 접근이 거절된 사이트를 계속 강제 요청하지 말고 공식 피드나 사용 허가된 다른 수집원을 사용하세요.

### 호출량 조절

`config/settings.json`의 `max_api_calls_per_run`은 기본 80회입니다. 재시도와 HOT도 포함하며, 한도를 넘긴 기사는 보류합니다. 기본 `batch_size`는 6입니다.

예를 들어 **새로 요약할 기사 60개**가 모두 한 번에 성공하면 요약 약 10회와 HOT 1회가 필요합니다. 실제 횟수는 캐시, 관련성 판정, 실패·재시도, 수집원 상황에 따라 달라집니다. 입력 길이가 다르므로 요청 수만으로 청구액을 계산할 수 없습니다.

429가 자주 나면 프로젝트 할당량을 먼저 확인하고 호출 간격을 늘리거나 불필요한 수집원을 줄이세요. 오류가 난다고 곧바로 재실행을 반복하지 마세요. 보류 항목은 다시 시도되지만 시간 범위를 넘기면 제외됩니다.

## 13. 자주 발생하는 문제

| 증상 | 확인할 곳 / 조치 |
|---|---|
| Actions에 워크플로가 안 보임 | 기본 브랜치의 최상위 `.github/workflows/` 아래에 YAML이 있는지 확인 |
| Run workflow 버튼이 없음 | `Update news & deploy`를 선택했는지, 기본 브랜치에 업로드했는지, 쓰기 권한이 있는지 확인 |
| `GEMINI_API_KEY ... 등록하세요` | 저장소 Secrets에 정확한 이름으로 등록. Variables나 계정 설정이 아님 |
| Gemini HTTP 400/403 | 키 종류·폐기/차단 여부·프로젝트 권한·API 설정·모델 입력 확인. 키를 로그에 출력하지 않음 |
| Gemini HTTP 404 | 모델 ID와 Interactions 지원 여부 확인 후 Variables 또는 설정 파일 수정 |
| Gemini HTTP 429 | AI Studio 할당량·사용량 확인. 호출 간격·요청 상한·수집 범위 조절 |
| HOT 실패 / 이전 선정 결과 | HOT 후보가 많으면 단계별 Gemini 선정. 429·5xx는 대체 모델 재시도, 계속 실패하면 할당량 확인 |
| Gemini HTTP 401 | AI Studio에서 Auth 키 유형·차단 상태 확인 후 `GEMINI_API_KEY` Secret 교체. `mode: check-api`로 검증. 인증 오류에서는 배포 중단 |
| `git push ... 403` | `contents: write`, 저장소 Workflow permissions, 조직 정책, `news-state` 보호 규칙 확인 |
| Pages 설정 오류 / 404 | Pages Source가 GitHub Actions인지, `deploy`까지 성공했는지 확인 |
| 특정 사이트만 403/robots 제한 | 사이트 수집 정책 또는 Actions IP 차단 가능. 상태 안내를 확인하고 허용된 수집원으로 대체 |
| `RSS/Atom이 아닌 응답` | 피드 주소가 홈페이지·오류·차단 화면으로 바뀌었는지 확인 |
| `본문 선택자 불일치` | 사이트 HTML 변경. `body_selector` 확인. 충분한 RSS가 있으면 해당 내용으로 요약 |
| `요약 근거 부족` | 제목만 있고 RSS 설명·본문을 확보하지 못함. 추측 요약하지 않고 보류한 상태 |
| 예약 시간이 지나도 갱신 안 됨 | 예약 지연·누락, 워크플로 비활성화, 기본 브랜치, 최근 Actions 실행 결과 확인 |
| 수동 실행했는데 시간 칩이 대기 | 정상. 예약 실행과 수동 실행은 분리 표시 |
| 소스를 수정했는데 화면이 그대로 | `rebuild` 실행 후 배포 완료 확인. 필요하면 브라우저 캐시 새로고침 |
| 보고서가 사라짐 | KST 오늘 포함 5일 보관 범위 확인. 브라우저 저장도 기간을 연장하지 않음 |

문제를 확인할 때 공유해도 되는 것은 **오류 코드, 실패한 단계 이름, 키를 지운 로그**입니다. Secret 값, 전체 인증 헤더, 키가 든 화면은 공유하지 마세요.

## 14. 로컬 실행·진단 — 필요한 경우에만

자동 배포만 사용할 때는 이 단계가 필요 없습니다. 직접 수집 코드를 시험하거나 소스를 수정할 때 사용합니다. Python 3.12를 기준으로 구성했고, 전달 환경의 로컬 검증은 Python 3.13에서 수행했습니다.

### Windows PowerShell

프로젝트 폴더에서 다음을 실행합니다. 가상환경 활성화 대신 Python 경로를 직접 써서 PowerShell 실행 정책 변경을 요구하지 않습니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m digest --build-only
.\.venv\Scripts\python.exe -m http.server 8000 --directory public
```

브라우저에서 `http://localhost:8000`을 엽니다. 서버는 `Ctrl+C`로 종료합니다.

실제 Gemini 연결 시험에는 환경변수가 필요합니다. 다음 명령은 키를 명령 기록에 직접 남기지 않도록 입력창을 사용합니다. 입력 후 키는 해당 프로세스 환경에 평문으로 존재하므로 이 터미널도 신뢰하는 환경에서만 사용합니다.

```powershell
$secureKey = Read-Host "Gemini API key" -AsSecureString
$env:GEMINI_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
.\.venv\Scripts\python.exe -m digest --check-api
.\.venv\Scripts\python.exe -m digest --check-sources
.\.venv\Scripts\python.exe -m digest
Remove-Item Env:GEMINI_API_KEY
Remove-Variable secureKey
```

`--check-api`는 기본 모델 두 개에 연결 확인 요청을 보내므로 API 사용량이 발생합니다. `--check-sources`는 Gemini를 호출하지 않지만 수집원 네트워크 요청을 보냅니다. 인자 없는 실행은 실제 수집·요약·HTML 생성을 수행합니다.

### Linux / macOS의 Bash

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
read -rsp "Gemini API key: " GEMINI_API_KEY; echo
export GEMINI_API_KEY
python -m digest --check-api
python -m digest --check-sources
python -m digest
unset GEMINI_API_KEY
python -m http.server 8000 --directory public
```

이 프로젝트는 `.env`를 자동으로 읽지 않습니다. `.env.example`은 변수 이름 참고용입니다. 로컬 `state/`를 저장소에 커밋하지 마세요. GitHub Actions의 `news-state`는 워크플로가 별도로 관리합니다.

## 15. 보관·개인정보·검증 범위

**5일은 120시간 타이머가 아니라 KST 달력 날짜 기준입니다.** 예를 들어 9월 22일에는 9월 18~22일을 유지합니다. 9월 23일 갱신에서는 9월 18일 보고서가 제거됩니다. 자정 즉시 별도 작업을 실행하는 것이 아니라 다음 성공한 생성·배포에서 서버 파일이 삭제됩니다. 브라우저는 날짜를 계산해 만료한 보고서를 숨기지만 클라이언트 코드가 서버 파일을 직접 삭제하는 것은 아닙니다.

5일 제거는 **현재 공개 사이트와 최신 상태 스냅샷**에 대한 정책입니다. Git 커밋 이력, GitHub의 과거 아티팩트·캐시, 별도 백업까지 완전히 지우는 기능은 아닙니다. 공개 저장소에서는 과거 `news-state` 이력에 요약과 짧은 수집 제공문이 남을 수 있습니다.

기사 근거는 Gemini 서비스에 전송됩니다. API 키는 Actions 환경변수에서만 읽고 브라우저에는 보내지 않습니다. 원문 전체 HTML·본문은 보고서에 게시하지 않습니다. `store:false`를 사용하지만 이것만으로 Google의 모든 로그·데이터 처리 정책을 비활성화한다는 의미는 아닙니다.

이 전달본에서는 로컬 단위·통합 테스트와 가상 화면 크기 검증을 수행했습니다. 실제 Gemini 키 요청, 전체 수집원의 실시간 접근, GitHub 계정에서의 배포, 폴드 8 울트라 실기기 테스트는 수행하지 않았습니다. 최초 `collect` 실행 결과와 기기 화면으로 최종 확인해야 합니다. 자세한 내용은 `docs/TESTING.md`에 있습니다.
