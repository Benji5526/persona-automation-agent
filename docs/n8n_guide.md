# n8n 연동 가이드 (M3)

M3 Workflow(`n8n/pa_*.json`)를 원격 n8n 서버에 올리고 Supabase·브릿지와 연결하는 방법이다. 설계는 [TECH_DESIGN.md](TECH_DESIGN.md) 14번(n8n 명세), 12.6·12.7(브릿지·Webhook API), 16.8(M3 구현 메모)에 있다.

```text
Lovable ─RPC─▶ Supabase ──DB Webhook──▶ n8n (원격 서버, Docker)
                  ▲                       │
                  │ Worker RPC            ├─▶ LLM (Claude API 또는 가짜 LLM)
                  │                       │
                  └──── Python 브릿지 ◀── Cloudflare Tunnel + Access ─┘
                        (로컬 PC, 127.0.0.1:8000) ──▶ ComfyUI (127.0.0.1:8188) ──▶ RTX 5080
                        └── 완료 콜백 ──▶ n8n WF-004
```

## 1. Workflow 파일

| 파일 | 이름 | 트리거 | 하는 일 |
|---|---|---|---|
| `pa_006_error_handler.json` | [PA] 006 - Error Handler | Error Trigger | 다른 Workflow의 예기치 못한 실패 → `fail_automation_job` 또는 `system_errors` |
| `pa_003_generation_dispatcher.json` | [PA] 003 - Generation Dispatcher | DB Webhook + 1분 | `pending` generation Job → 브릿지 `POST /v1/jobs` |
| `pa_001_content_job_dispatcher.json` | [PA] 001 - Content Job Dispatcher | DB Webhook + 1분 | `queued` Content Job 선점 → prompt Job → WF-002, n8n 상태 보고 |
| `pa_llm_structured_call.json` | [PA] LLM - Structured Call | WF-002·005가 호출 | LLM 호출 (가짜 LLM / Claude API) |
| `pa_002_prompt_generator.json` | [PA] 002 - Prompt Generator | WF-001 호출 + 1분 | LLM → `prompt_generation.v1` 검증 → `prompt_parts` 저장 → generation Job |
| `pa_004_generation_result_handler.json` | [PA] 004 - Generation Result Handler | 브릿지 콜백 + 5분 | 완료된 Asset마다 caption Job → WF-005 |
| `pa_005_caption_generator.json` | [PA] 005 - Caption Generator | WF-004 호출 + 1분 | LLM → `caption_generation.v1` 검증 → `posts` 초안 |

전체 흐름:

```text
content_jobs: queued ──WF-001──▶ generating
  └ prompt Job ──WF-002──▶ done ─┐
                                 └ generation Job ──WF-003──▶ 브릿지 ──▶ done ──(DB Rollup)──▶ Content Job ready
                                                                  └ 콜백 ──WF-004──▶ caption Job ──WF-005──▶ posts(draft)
```

재시도는 n8n이 아니라 DB(`fail_automation_job`)가 정한다. 재시도 대기 Job과 Heartbeat로 회수된 Job은 각 Workflow의 1분 안전망이 다시 가져간다.

## 2. 사전 준비

- [ ] Supabase에 마이그레이션 0001~0008 적용 ([supabase/README.md](../supabase/README.md))
- [ ] Supabase에서 **n8n 전용 secret key**를 새로 만든다 (브릿지 키와 따로, TECH 15.6)
- [ ] Cloudflare Tunnel이 브릿지(`127.0.0.1:8000`)에 연결돼 있고, Cloudflare Access에서 **Service Token**(Client ID·Secret)을 만들어 그 터널 앱에 허용 (TECH 15.7)
- [ ] 브릿지 `.env`의 `BRIDGE_TOKENS` 값 확인
- [ ] (실제 LLM을 쓸 때) Anthropic API Key, 월 사용량 알림 설정
- [ ] n8n 서버 하드닝 (TECH 15.9): `NODES_EXCLUDE`, `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`, Execution 데이터 14일 보관(`EXECUTIONS_DATA_MAX_AGE=336`)

## 3. Credential 만들기

n8n → **Credentials → Add credential**. 비밀값은 노드 파라미터에 넣지 않고 Credential로만 쓴다.

| 이름 (정확히 이대로) | 종류 | 값 | 쓰는 곳 |
|---|---|---|---|
| `PA Supabase` | **Custom Auth** | 아래 JSON | 모든 Supabase 노드 (LLM Workflow의 `reserve_llm_call` 포함) |
| `PA Bridge` | **Custom Auth** | 아래 JSON | WF-003 브릿지 노드 |
| `PA Webhook Secret` | Header Auth | Name `X-Webhook-Secret`, Value 무작위 32자 이상 | WF-001·003 Webhook |
| `PA Callback Token` | Header Auth | Name `X-Callback-Token`, Value = 브릿지 `.env`의 `N8N_CALLBACK_TOKEN` | WF-004 Webhook |
| `PA Anthropic` | Header Auth | Name `x-api-key`, Value = Anthropic API Key | LLM Workflow (`claude` 모드일 때만) |

`PA Supabase` (Custom Auth):

```json
{ "headers": { "apikey": "sb_secret_…(n8n 전용)", "x-actor": "n8n" } }
```

- 새 secret key(`sb_secret_…`)는 `apikey` 헤더만 보낸다 (브릿지와 같은 방식).
- 예전 `service_role` 키(JWT)를 쓴다면 `"Authorization": "Bearer <같은 키>"`도 추가한다.
- `x-actor: n8n`은 `state_transitions`에 누가 상태를 바꿨는지 남기는 값이다.

`PA Bridge` (Custom Auth):

```json
{
  "headers": {
    "X-Bridge-Token": "<BRIDGE_TOKENS의 값 하나>",
    "CF-Access-Client-Id": "<Service Token Client ID>",
    "CF-Access-Client-Secret": "<Service Token Client Secret>"
  }
}
```

무작위 값 만들기:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 4. Import

### 4-1. 주소 바꾸기

파일 안의 자리표시자 두 개를 실제 값으로 바꾼 복사본을 import한다 (저장소의 원본은 그대로 둔다).

| 자리표시자 | 값 |
|---|---|
| `YOUR-PROJECT-REF` | Supabase 프로젝트 ref (`https://<ref>.supabase.co`) |
| `YOUR-BRIDGE-DOMAIN` | Cloudflare Tunnel로 연결한 브릿지 도메인 (예: `bridge.example.com`) |

```bash
mkdir -p /tmp/pa-n8n && cp n8n/pa_*.json /tmp/pa-n8n/ && sed -i -e 's/YOUR-PROJECT-REF/<ref>/g' -e 's/YOUR-BRIDGE-DOMAIN/<bridge-domain>/g' /tmp/pa-n8n/*.json
```

### 4-2. 순서대로 import

**Workflows → Import from File**로 아래 순서대로 올린다. 다른 Workflow를 부르는 노드가 있어서 불리는 쪽을 먼저 올린다.

1. `pa_006_error_handler.json`
2. `pa_llm_structured_call.json`
3. `pa_003_generation_dispatcher.json`
4. `pa_005_caption_generator.json`
5. `pa_004_generation_result_handler.json`
6. `pa_002_prompt_generator.json`
7. `pa_001_content_job_dispatcher.json`

### 4-3. Import 후 연결

| Workflow | 할 일 |
|---|---|
| 모든 Workflow | HTTP 노드마다 Credential 선택 (§3 표). 파일에는 Credential이 들어 있지 않다 |
| 006 제외 모두 | **Settings → Error Workflow** = `[PA] 006 - Error Handler` |
| 001 | `WF-002 실행` 노드 → Workflow = `[PA] 002 - Prompt Generator` |
| 002, 005 | `LLM 호출` 노드 → Workflow = `[PA] LLM - Structured Call` |
| 004 | `WF-005 실행` 노드 → Workflow = `[PA] 005 - Caption Generator` |

그다음 Workflow를 **Active**로 켠다. `[PA] LLM - Structured Call`은 다른 Workflow가 부르기만 하므로 켜지 않아도 된다.

> 이 JSON은 n8n에 실제로 import해 보지 않고 작성했다. import할 때 노드 버전 차이로 경고가 나오면 해당 노드를 열어 값을 확인한다 (특히 Execute Workflow 노드의 Workflow 선택, Custom Auth Credential).

## 5. Supabase Database Webhook 2개

Dashboard → **Database → Webhooks → Create a new hook**. 두 Webhook 모두 HTTP Headers에 `X-Webhook-Secret: <PA Webhook Secret 값>`을 넣는다.

| Name | Table | Events | URL (n8n Production URL) |
|---|---|---|---|
| `pa_content_jobs` | `public.content_jobs` | Insert, Update | `https://<n8n 도메인>/webhook/pa/content-jobs` |
| `pa_automation_jobs` | `public.automation_jobs` | Insert | `https://<n8n 도메인>/webhook/pa/automation-jobs` |

- Test URL(`/webhook-test/…`)이 아니라 **Production URL**을 쓴다. Workflow가 Active여야 동작한다.
- Webhook 전달이 실패해도 DB 변경은 되돌려지지 않는다. 그래서 1분 안전망이 함께 돈다.
- 전달 결과 확인: `select * from net._http_response order by created desc limit 10;`

## 6. 브릿지 콜백

WF-004 Webhook의 Production URL과 토큰을 브릿지 `.env`에 넣고 브릿지를 다시 시작한다.

```text
N8N_CALLBACK_URL=https://<n8n 도메인>/webhook/pa/generation-result
N8N_CALLBACK_TOKEN=<PA Callback Token 값>
```

브릿지 → n8n은 로컬에서 나가는 요청이라 터널이 필요 없다.

## 7. LLM 모드

`[PA] LLM - Structured Call`의 `LLM 모드 (fake | claude)` 노드에서 `llm_mode` 값을 바꾼다.

| 값 | 동작 |
|---|---|
| `fake` (기본) | 고정 JSON을 돌려준다. M3 완료 조건(가짜 LLM으로 `queued → ready`)과 비용 없는 테스트용 |
| `claude` | 먼저 `reserve_llm_call`(마이그레이션 0008)로 하루 호출 한도(`limits.daily_llm_calls_limit`)를 확인한다. 통과하면 Claude API (`claude-opus-5-5`, effort `low`, Structured Output `output_config.format`). 한도 초과면 Job은 다음 UTC 자정에 재시도된다 (남은 시도가 있을 때. 마지막 시도였다면 `failed`가 되고 다시 실행해야 한다, TECH 21.18). 안전 분류로 거절되면 `fallbacks: "default"`가 서버 쪽에서 다른 모델로 다시 시도한다 |

어느 모드든 n8n이 결과를 `prompt_generation.v1`·`caption_generation.v1` 스키마로 다시 검증한다 (TECH 12.9). LLM 요청 Timeout은 90초로, prompt·caption Job의 Heartbeat 제한(`app_settings.heartbeat_timeout_seconds`, 120초)보다 짧다. Timeout을 늘리면 Heartbeat 제한도 함께 늘린다.

## 8. 동작 확인 (M3 완료 조건)

> 가짜 LLM(고정 JSON)과 실제 브릿지로 `queued → … → ready`가 사람 손 없이 진행된다 (TECH 16.8).

1. 브릿지와 ComfyUI를 켜고 `GET https://<bridge-domain>/v1/health`가 `{"ok": true, "comfyui": true, …}`인지 확인한다.
2. SQL Editor에서 테스트 Persona와 Content Job을 만든다 (`user_id`는 Google 로그인한 자기 계정, `base_model`은 실제 체크포인트 파일명).
   ```sql
   insert into public.personas (user_id, name, slug, description, visual_settings)
   select id, 'Test', 'test', 'young Korean woman, long dark brown hair',
          '{"default_workflow": "image_generation_v1", "base_model": "<checkpoint>.safetensors", "style": "photorealistic"}'::jsonb
     from public.users where email = 'you@example.com'
   returning id;

   insert into public.content_jobs (persona_id, content_type, topic, platform, status)
   values ('<persona id>', 'image', '서울 카페에서 보내는 오후', 'instagram', 'queued')
   returning id;
   ```
3. 진행을 확인한다.
   ```sql
   select status, run_number, updated_at from public.content_jobs where id = '<content job id>';
   select job_type, status, attempts, error_code, error_message from public.automation_jobs
    where content_job_id = '<content job id>' order by created_at;
   select step, service, status, error, created_at from public.execution_logs
    where automation_job_id in (select id from public.automation_jobs where content_job_id = '<content job id>')
    order by created_at;
   ```
   기대 결과: Content Job `ready`, Automation Job `prompt`·`generation`·`caption` 모두 `done`, `assets` 1행, `posts` 1행(`draft`).
4. 실패 경로도 확인한다 (TECH 16.13): ComfyUI를 끈 상태(Job이 `pending`으로 남았다가 다시 켜면 진행), 없는 체크포인트(`MODEL_NOT_FOUND`로 바로 `failed`), Content Job 취소.

## 9. 문제 해결

| 증상 | 확인할 것 |
|---|---|
| Content Job이 `queued`에서 안 움직임 | WF-001이 Active인지, `pa_content_jobs` Webhook 응답(`net._http_response`), 1분 안전망 실행 기록 |
| prompt Job이 `pending`으로 남음 | WF-002 Active·`LLM 호출` 노드의 Workflow 선택. `run_after`가 미래면 재시도 대기 중 |
| generation Job이 `pending`으로 남음 | WF-003 실행 기록의 `응답 분류` 결과: `bridge_unreachable`(PC·터널 꺼짐), `bridge_unavailable`(ComfyUI 꺼짐), 401·403(`PA Bridge` Credential·Access Service Token) |
| generation `done`인데 caption Job이 없음 | 브릿지 `.env`의 `N8N_CALLBACK_URL`·토큰. 5분 안전망이 최근 1일 Asset을 다시 확인한다 |
| `system_errors`에 `N8N_WORKFLOW_ERROR` | `step` 칸에 노드 이름, `message` 앞에 Workflow 이름이 있다. n8n Executions에서 같은 시각의 실패 실행을 연다 |
| `LLM_OUTPUT_INVALID` 반복 | `execution_logs`의 `LLM` 단계 `error`. 실제 모드라면 프롬프트·모델 확인, 가짜 모드라면 고정 JSON이 스키마와 맞는지 |
| `RATE_LIMITED` | 하루 생성 한도(`app_settings.limits.daily_generation_limit`). prompt Job이 다음 UTC 자정(한국 시간 오전 9시) 뒤 다시 시도한다 |
