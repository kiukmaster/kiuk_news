# 한국시간 뉴스 예약

이 Worker는 Cloudflare에서 실행되므로 PC나 개인 서버를 계속 켜둘 필요가 없습니다. 뉴스 수집과 Pages 게시에는 기존 GitHub Actions를 사용합니다.

| 한국시간 준비 | 한국시간 공개 목표 | Cloudflare UTC cron |
|---|---|---|
| 05:07 | 06:00 | `7 20 * * *` |
| 12:07 | 13:00 | `7 3 * * *` |
| 18:07 | 19:00 | `7 9 * * *` |

준비가 끝난 Actions runner가 공개 목표까지 기다렸다가 배포합니다. Cloudflare 트리거, Actions runner 및 Pages 자체의 장애·전파 지연까지 초 단위로 보장하는 구성은 아닙니다. GitHub 기본 cron에서 확인된 수 시간의 이벤트 생성 지연을 피하는 구성입니다.

## 실제 활성화

1. 무료 Cloudflare 계정에 로그인합니다. Workers & Pages에서 `kiuk-news-scheduler` Worker를 만듭니다. [worker.mjs](worker.mjs)를 전체 Worker 코드로 사용합니다.
2. Worker의 Settings → Variables and Secrets에 다음 값을 등록합니다.

   | 이름 | 종류 | 값 |
   |---|---|---|
   | `GITHUB_REPOSITORY` | 일반 변수 | `kiukmaster/kiuk_news` |
   | `GITHUB_REF` | 일반 변수 | `main` |
   | `GITHUB_TOKEN` | Secret | 아래에서 만든 제한된 토큰 |

   GitHub의 Settings → Developer settings → Fine-grained personal access tokens에서 `kiuk_news` 저장소만 선택하고 **Actions: Read and write**를 허용합니다. 만료 날짜를 기록하고 만료 전에 Worker Secret을 교체합니다. 토큰을 코드, 저장소, URL 또는 대화에 붙여 넣지 않습니다.

3. Worker Settings → Triggers → Cron Triggers에 위 표의 UTC cron 세 개를 등록합니다. HTTP 공개 주소는 필요하지 않습니다. 새 트리거가 전파되는 데 최대 15분이 걸릴 수 있습니다.
4. Worker 실행 로그와 GitHub의 `News 슬롯 전체시각` 실행을 확인합니다. `publish_at`은 `2026-10-01T19:00:00+09:00`처럼 날짜를 포함합니다. 같은 목표의 요청이 이미 있으면 Worker는 새 요청을 보내지 않습니다.
5. 실제 외부 예약 요청이 확인된 후 GitHub 저장소 변수 `EXTERNAL_SCHEDULER_ENABLED=true`를 설정합니다. 확인 전에는 기본 예약을 끄지 않습니다.

CLI를 사용하는 경우 Cloudflare에 로그인한 뒤 저장소 루트에서 아래 명령으로 코드와 트리거를 배포할 수도 있습니다. Secret 입력은 CLI의 비밀 입력란에서 합니다.

```powershell
npx wrangler login
npx wrangler deploy --config scheduler/wrangler.jsonc
npx wrangler secret put GITHUB_TOKEN --config scheduler/wrangler.jsonc
```

## 누락된 배치 즉시 복구

이 PC의 인증된 `gh` CLI를 사용할 수 있으면 다음 명령으로 13시 배치를 수집부터 재실행합니다. 게시 목표의 날짜가 바뀌지 않도록 명시합니다.

```powershell
.venv/Scripts/python.exe -X utf8 scripts/dispatch_news.py --slot 13:00 --publish-at 2026-10-01T13:00:00+09:00
```

실행 전 `--dry-run`을 추가하면 계획만 출력합니다. 기본 로컬 영수증은 Git에서 제외된 `state/local-scheduler/`에 저장됩니다. 불확실한 전송 결과는 실행 기록에서 확인하며 확인 없이 자동으로 재요청하지 않습니다.

## 검증

```text
node --test scheduler/worker.test.mjs
.venv/Scripts/python.exe -X utf8 -m pytest -q
```

운영 참고: [Cloudflare Cron Trigger](https://developers.cloudflare.com/workers/configuration/cron-triggers/), [GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event), [GitHub 예약 지연](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
