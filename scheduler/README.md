# 한국시간 뉴스 예약

이 Worker는 Cloudflare에서 실행되므로 PC나 개인 서버를 계속 켜둘 필요가 없습니다. 뉴스 수집과 Pages 게시에는 기존 GitHub Actions를 사용합니다.

| 한국시간 첫 준비 | 복구 점검 | 한국시간 공개 목표 | Cloudflare UTC cron (첫 준비 / 복구) |
|---|---|---|---|
| 05:07 | 05:27, 05:47 | 06:00 | `7 20 * * *` / `27 20 * * *`, `47 20 * * *` |
| 12:07 | 12:27, 12:47 | 13:00 | `7 3 * * *` / `27 3 * * *`, `47 3 * * *` |
| 18:07 | 18:27, 18:47 | 19:00 | `7 9 * * *` / `27 9 * * *`, `47 9 * * *` |

준비가 끝난 Actions runner가 공개 목표까지 기다렸다가 배포합니다. 복구 점검은 원래 목표 날짜·시각으로 GitHub 실행 기록을 조회합니다. 해당 실행이 없으면 같은 목표를 한 번 요청하고, 실행 중이거나 성공했으면 아무 작업도 하지 않습니다. 실패·취소·시간 초과로 끝났으면 원래 실행을 다시 시작하되 전체 시도를 최대 3회로 제한합니다. GitHub가 재실행 요청을 수락했는지 불확실할 때 POST를 반복하지 않습니다. 재실행은 원래 커밋과 입력값을 사용하며 Gemini 호출량이 추가될 수 있습니다.

Cloudflare 트리거, Actions runner 및 Pages 자체의 장애·전파 지연까지 초 단위로 보장하는 구성은 아닙니다. 복구 점검도 GitHub가 잡을 실행할 수 없거나 Gemini 요청이 계속 실패하면 게시를 완료하지 못합니다. GitHub 기본 cron에서 확인된 수 시간의 이벤트 생성 지연을 피하는 구성입니다.

GitHub 실행 기록을 조회하는 GET은 일시적인 5xx·429·통신 오류에 한해 최대 3회 시도합니다. 기본 대기는 1초·3초이며 서버가 보낸 `Retry-After`를 최대 30초까지 반영합니다. 실행 생성과 재실행 POST는 각각 한 번만 보내며, 불확실한 결과는 다음 예약 점검에서 실행 기록으로 확인합니다.

배포 설정에서 Workers Logs를 활성화합니다. Cloudflare의 **Workers & Pages → kiuk-news-scheduler → Observability**에서 예약 실행의 성공·중복·오류 기록을 확인할 수 있습니다. 토큰이나 응답 본문은 로그에 남기지 않습니다.

## 실제 활성화

처음 만든 Cloudflare 계정은 가입 이메일 인증을 완료한 뒤 대시보드의 **Workers & Pages** 메뉴를 한 번 엽니다. 이때 계정의 `workers.dev` 기본 설정이 생성됩니다. 이메일 인증 전에는 `10034`, 기본 설정 전에는 Cron 등록 시 `10063` 오류가 발생할 수 있습니다.

1. 무료 Cloudflare 계정에 로그인합니다. Workers & Pages에서 `kiuk-news-scheduler` Worker를 만듭니다. [worker.mjs](worker.mjs)를 전체 Worker 코드로 사용합니다.
2. Worker의 Settings → Variables and Secrets에 다음 값을 등록합니다.

   | 이름 | 종류 | 값 |
   |---|---|---|
   | `GITHUB_REPOSITORY` | 일반 변수 | `kiukmaster/kiuk_news` |
   | `GITHUB_REF` | 일반 변수 | `main` |
   | `GITHUB_TOKEN` | Secret | 아래에서 만든 제한된 토큰 |

   GitHub의 Settings → Developer settings → Fine-grained personal access tokens에서 `kiuk_news` 저장소만 선택하고 **Actions: Read and write**를 허용합니다. 만료 날짜를 기록하고 만료 전에 Worker Secret을 교체합니다. 토큰을 코드, 저장소, URL 또는 대화에 붙여 넣지 않습니다.

3. Worker Settings → Triggers → Cron Triggers에 위 표의 UTC cron 아홉 개를 등록합니다. 기존 세 개를 유지하고 `27`분·`47`분 점검 여섯 개를 추가합니다. HTTP 공개 주소는 필요하지 않습니다. 새 트리거가 전파되는 데 최대 15분이 걸릴 수 있습니다.
4. Worker 실행 로그와 GitHub의 `News 슬롯 전체시각` 실행을 확인합니다. `publish_at`은 `2026-10-01T19:00:00+09:00`처럼 날짜를 포함합니다. 같은 목표의 실행이 정상 진행 중이면 Worker는 새 요청을 보내지 않습니다. 실패한 실행의 재시도는 같은 실행 번호의 다음 attempt로 표시됩니다.
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
