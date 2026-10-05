# n8n 연동 가이드

> ⚠️ **이 문서는 M1 이전 구조(`media_queue` 테이블, `POST /jobs`) 기준입니다.** M2에서 브릿지가 바뀌었습니다: 엔드포인트 `POST /v1/jobs`, 테이블 `automation_jobs`, 콜백 본문 `event`·`asset_ids`·`error` (TECH_DESIGN 12.6·12.7), 터널은 Cloudflare Tunnel + Access (15.13). n8n Workflow JSON(`n8n/`)과 이 문서는 **M3에서 새 구조로 개편**합니다. 그 전까지 아래 내용은 참고용으로만 보세요.

이 문서는 아래 비전 파이프라인을 n8n으로 연결하는 방법을 다룹니다.

```
Supabase media_queue (INSERT)
      │  ① 변경 감지 (Database Webhook 또는 폴링)
      ▼
n8n  ── ② POST /jobs {job_id} ──▶ comfy_bridge.py (로컬, :8000)
                                        │ ③ /prompt, /history, /view
                                        ▼
                                   ComfyUI (로컬 RTX 5080, :8188)
                                        │
                                        ▼ ④ 결과 업로드 + status=done
                                   Supabase Storage / media_queue
      ┌─────────── ⑤ 콜백 POST (N8N_CALLBACK_URL) ────┘
      ▼
n8n  ── ⑥ posts 행 생성 → SNS 포스팅 스크립트
```

---

## 0. 먼저 구성 방식 정하기

터널이 어디에 필요한지는 n8n이 어디서 돌아가는지에 따라 달라집니다.

| 구성 | Supabase → n8n | n8n → 브릿지 | 필요한 터널 |
|---|---|---|---|
| **A. n8n Cloud** (또는 원격 서버) | 공개 URL이라 그대로 가능 | 로컬 PC에 닿아야 함 | **브릿지(:8000)** |
| **B. 로컬 n8n** (Docker 또는 npx) | Supabase가 로컬 PC에 닿아야 함 | `host.docker.internal:8000` 또는 `localhost:8000` | **n8n(:5678)** (Database Webhook을 쓸 때만) |
| **B'. 로컬 n8n + 폴링** | 불필요 (n8n이 Supabase를 조회) | 로컬 | **없음** |

> ⚠️ 어떤 구성이든 **ComfyUI(:8188)는 절대 외부에 노출하지 마세요.** ComfyUI API에는 인증이 없어서 누구나 GPU로 임의 워크플로우를 실행할 수 있습니다. 외부에는 토큰 검사가 있는 브릿지만 공개합니다.

처음 시작한다면 **B' (로컬 n8n + 폴링)**가 설정이 가장 적고 터널도 필요 없습니다. 실시간 반응이 필요해지면 A나 B의 Database Webhook 방식으로 옮기세요.

---

## 1. 사전 준비

### 1-1. Supabase 스키마

1. Supabase Dashboard → **SQL Editor** → `database/schema.sql` 전체를 붙여넣고 **Run**
2. **Table Editor**에서 `messages`, `media_queue`, `posts` 테이블이, **Storage**에서 `media` 버킷이 생겼는지 확인합니다.
3. **Project Settings → API**에서 다음 두 값을 복사해 둡니다.
   - Project URL → `SUPABASE_URL`
   - `service_role` secret → `SUPABASE_SERVICE_ROLE_KEY` (RLS를 우회하는 키입니다. 브릿지와 n8n credential에만 넣으세요)

### 1-2. ComfyUI

```bash
# ComfyUI 폴더에서 (localhost에만 바인딩)
python main.py --listen 127.0.0.1 --port 8188
```

- RTX 50 시리즈(Blackwell)는 CUDA 12.8 이상용 PyTorch 빌드가 필요합니다. ComfyUI 공식 포터블 최신판을 쓰면 포함되어 있습니다.
- 워크플로우 템플릿 만들기
  1. ComfyUI에서 워크플로우를 완성합니다.
  2. 설정에서 **Dev mode**를 켜고 **Workflow → Export (API)**로 JSON을 저장합니다.
  3. 파일을 `workflows/<이름>.json`에 두고, 바꾸고 싶은 값을 `"{{prompt}}"`, `"{{seed}}"` 같은 자리표시자로 바꿉니다.
  4. `media_queue.workflow`에 `<이름>`을 넣고, `media_queue.params`에 자리표시자 값을 넣습니다.
  - 예제: [`workflows/txt2img_basic.json`](../workflows/txt2img_basic.json) (`checkpoint`, `prompt` 필수)
  - 페이스스왑처럼 입력 이미지가 필요하면 `LoadImage` 노드의 `image` 값을 `"{{source_face}}"`로 두고, `media_queue.input_images`에 `{"source_face": "<URL 또는 Storage 경로>"}`를 넣습니다. 브릿지가 ComfyUI에 업로드한 뒤 파일명으로 바꿔 넣습니다.

### 1-3. 브릿지 실행

```bash
cd D:/Projects/persona-automation-agent
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env
```

`.env`를 채웁니다. `BRIDGE_TOKEN`은 아래 명령으로 만든 긴 무작위 문자열을 쓰세요.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```bash
.venv/Scripts/python -m uvicorn src.comfy_bridge:app --host 127.0.0.1 --port 8000
```

- 로컬 n8n을 **Docker**로 돌린다면 컨테이너에서 접근할 수 있도록 `--host 0.0.0.0`으로 띄우고, 방화벽은 사설망만 허용하세요.
- 확인: `curl http://127.0.0.1:8000/health` → `{"ok":true,"comfyui":true,...}`

### 1-4. 브릿지 API 요약

| 메서드 | 경로 | 헤더 | 바디 | 응답 |
|---|---|---|---|---|
| POST | `/jobs` | `X-Bridge-Token: <BRIDGE_TOKEN>` | `{"job_id": "<uuid>"}` (생략하면 다음 pending 작업) | `202` 접수 / `401` 토큰 오류 / `409` 선점할 작업 없음 |
| GET | `/health` | – | – | ComfyUI 연결 상태, 대기열 길이 |

- 브릿지는 작업을 **즉시 202로 응답**하고 백그라운드에서 처리합니다. 그래서 오래 걸리는 영상 생성도 n8n HTTP 타임아웃에 걸리지 않습니다.
- 같은 `job_id`를 두 번 보내도 DB 함수 `claim_media_job`이 한 번만 선점하므로 두 번째 요청은 `409`를 받습니다. n8n에서 재시도를 켜 두어도 안전합니다.

### 1-5. 자동 재시도

작업이 실패하면 브릿지가 오류 종류를 보고 다시 시도할지 정합니다.

| 오류 | 재시도 | 이유 |
|---|---|---|
| ComfyUI 시간 초과, 연결 끊김, Storage 업로드 실패, GPU 메모리 부족(OOM) | ✅ | 잠시 뒤에는 성공할 수 있음 |
| 템플릿 없음, 자리표시자 누락, ComfyUI 워크플로우 검증 실패(400), 모델 파일 없음 | ❌ 바로 `failed` | 다시 해도 결과가 같음 |

- 재시도할 오류면 `attempts < max_attempts`(기본 3)일 때 `status`를 `pending`으로 되돌리고, `run_after`를 **60초 → 120초 → 240초…** 뒤로 미룹니다. 간격은 `.env`의 `RETRY_BASE_DELAY_SEC`로 바꿀 수 있습니다.
- `error` 칸에는 `[시도 1/3] …` 형식으로 마지막 오류가 남습니다.
- 재시도 대기 중인 작업은 Database Webhook(INSERT)으로 다시 감지되지 않습니다. **3-2의 폴링이 켜져 있어야** `run_after`가 지난 작업을 다시 가져갑니다.
- 작업마다 횟수를 다르게 하려면 INSERT할 때 `max_attempts`를 지정하세요. (예: 영상은 `2`)
- n8n 콜백(4-1)은 최종 `done` 또는 `failed`일 때만 갑니다.

---

## 2. 로컬 환경 터널링

### 2-1. ngrok (권장: 고정 도메인 무료 제공)

1. https://dashboard.ngrok.com 에 가입하고 **Your Authtoken**을 복사합니다.
2. 설치하고 토큰을 등록합니다.
   ```bash
   winget install ngrok.ngrok
   ngrok config add-authtoken <YOUR_AUTHTOKEN>
   ```
3. Dashboard → **Domains**에서 무료 고정 도메인을 하나 받습니다. (예: `your-name.ngrok-free.app`) 고정 도메인이 없으면 재시작할 때마다 URL이 바뀌어 n8n 노드를 매번 고쳐야 합니다.
4. 터널을 엽니다.
   ```bash
   # 구성 A: 브릿지를 공개
   ngrok http 8000 --url https://your-name.ngrok-free.app

   # 구성 B: 로컬 n8n을 공개 (Supabase Database Webhook 수신용)
   ngrok http 5678 --url https://your-name.ngrok-free.app
   ```
   ngrok 버전이 오래되어 `--url`을 인식하지 못하면 `--domain your-name.ngrok-free.app`을 쓰세요.
5. 확인
   ```bash
   curl https://your-name.ngrok-free.app/health
   ```

**참고**
- 무료 도메인에 **브라우저**로 접속하면 경고 페이지가 뜹니다. n8n이나 curl 같은 API 호출에는 보통 뜨지 않지만, HTML 경고 페이지가 응답으로 오면 요청 헤더에 `ngrok-skip-browser-warning: true`를 추가하세요.
- 구성 B에서 로컬 n8n을 터널로 공개할 때는 n8n이 외부 URL을 알도록 환경변수를 설정합니다. 그래야 Webhook 노드가 올바른 Production URL을 표시합니다.
  ```bash
  WEBHOOK_URL=https://your-name.ngrok-free.app/
  ```
- 무료 플랜은 동시 터널 수가 제한됩니다. 브릿지와 n8n을 둘 다 공개해야 한다면 `ngrok.yml`에 터널 두 개를 정의하고 `ngrok start --all`을 쓰거나, 아래 Cloudflare Tunnel을 함께 쓰세요.

### 2-2. 대안: Cloudflare Tunnel

```bash
winget install Cloudflare.cloudflared

# 빠른 임시 터널 (계정 불필요, URL이 매번 바뀜 → 테스트용)
cloudflared tunnel --url http://localhost:8000
```

고정 주소가 필요하면 Cloudflare에 도메인을 연결하고 **Zero Trust → Networks → Tunnels**에서 named tunnel을 만드세요. Cloudflare Access로 앞단 인증을 한 겹 더 둘 수도 있습니다.

### 2-3. 보안 체크리스트

- [ ] 터널은 **브릿지(:8000)** 또는 **n8n(:5678)** 에만 연결 (ComfyUI :8188 금지)
- [ ] `BRIDGE_TOKEN`은 32바이트 이상 무작위 값, n8n에는 **Credential**로만 저장
- [ ] n8n Webhook 노드에 **Header Auth** 설정 (아래 3-1 참고)
- [ ] `.env`는 `.gitignore`에 포함되어 있음 (커밋 금지)

---

## 3. 워크플로우 ①: 작업 감지 → 브릿지 호출

### 3-0. 바로 import 하기

[`n8n/01_media_dispatch.json`](../n8n/01_media_dispatch.json)에 아래 3-1(Webhook)과 3-2(1분 폴링)가 모두 들어 있습니다.

1. n8n → **Workflows → Import from File** (또는 JSON 내용을 캔버스에 붙여넣기)
2. 두 HTTP Request 노드 URL의 `YOUR-BRIDGE-DOMAIN`을 실제 주소로 바꿉니다.
3. 각 노드를 열어 Credential을 연결합니다. 파일에는 Credential이 들어 있지 않습니다.
   - Webhook 노드: Header Auth, Name `X-Webhook-Secret`
   - HTTP 노드 2개: Header Auth, Name `X-Bridge-Token`
4. 워크플로우를 **Active**로 켜고, Webhook의 Production URL을 Supabase Database Webhook에 등록합니다(3-1 "Supabase 쪽").

Database Webhook을 쓰지 않을 거라면 Webhook과 IF 노드를 지우고 폴링만 남기면 됩니다. 아래는 각 노드를 직접 만들 때의 설정 설명입니다.

**권장 구성**은 Webhook(즉시 반응)과 1분 폴링(놓친 작업, 재시도 대기 작업 처리)을 함께 쓰는 것입니다. 둘이 같은 작업을 보내도 선점 함수 덕분에 한 번만 처리되고, 나머지는 `409`를 받습니다.

### 3-1. 방법 A: Supabase Database Webhook (실시간)

**n8n 쪽**

1. **Webhook** 노드
   - HTTP Method: `POST`
   - Path: `media-queue`
   - Authentication: `Header Auth` → Credential 새로 만들기
     - Name: `X-Webhook-Secret`
     - Value: 무작위 문자열 (Supabase 설정에 같은 값을 넣습니다)
   - Respond: `Immediately`
   - 워크플로우를 **Active**로 켠 뒤 **Production URL**을 복사합니다. (Test URL은 에디터에서 "Listen" 중일 때만 동작합니다)

2. **IF** 노드: 새 pending 작업만 통과시킵니다.
   - `{{ $json.body.type }}` is equal to `INSERT`
   - AND `{{ $json.body.record.status }}` is equal to `pending`

3. **HTTP Request** 노드 (IF의 true 출력에 연결)
   - Method: `POST`
   - URL: `https://your-name.ngrok-free.app/jobs` (구성 A) 또는 `http://host.docker.internal:8000/jobs` (구성 B, Docker)
   - Authentication: `Generic Credential Type` → `Header Auth`
     - Name: `X-Bridge-Token`, Value: `.env`의 `BRIDGE_TOKEN`
   - Send Body: ON, Body Content Type: `JSON`, Specify Body: `Using JSON`
     ```json
     { "job_id": "{{ $json.body.record.id }}" }
     ```
   - Options → Response → **Never Error** ON: `409`("선점할 작업 없음")는 정상 응답이라 실패로 처리하지 않습니다.
   - Settings → **Retry On Fail** ON (Max Tries 3, Wait 5000ms): Never Error가 켜져 있으므로, 브릿지나 터널이 꺼져 연결 자체가 안 될 때만 재시도합니다.

**Supabase 쪽**

1. Dashboard → **Database → Webhooks** (또는 **Integrations → Database Webhooks**) → **Create a new hook**
2. 설정
   - Name: `media_queue_to_n8n`
   - Table: `public.media_queue`
   - Events: `Insert`
   - Type: `HTTP Request`
   - Method: `POST`
   - URL: n8n Webhook **Production URL**
   - HTTP Headers: `X-Webhook-Secret: <위에서 만든 값>`, `Content-Type: application/json`
3. Supabase가 보내는 페이로드 형식 (n8n에서는 `$json.body` 아래에 들어옵니다)
   ```json
   {
     "type": "INSERT",
     "table": "media_queue",
     "schema": "public",
     "record": { "id": "…", "status": "pending", "workflow": "txt2img_basic", "...": "..." },
     "old_record": null
   }
   ```

> Database Webhook은 내부적으로 `pg_net`을 쓰는 비동기 요청이라, 전달에 실패해도 INSERT 자체는 롤백되지 않습니다. 또 재시도 대기(`pending` + `run_after`)로 돌아간 작업은 INSERT가 아니라서 Webhook이 다시 오지 않습니다. 그래서 **3-2의 폴링을 함께 켜 두세요.**

### 3-2. 방법 B: 폴링 (터널 없이 가능)

가장 단순한 형태는 브릿지가 직접 "다음 작업"을 선점하게 하는 것입니다.

1. **Schedule Trigger** 노드: Every `1` minute
2. **HTTP Request** 노드
   - POST `http://localhost:8000/jobs` (Docker라면 `http://host.docker.internal:8000/jobs`)
   - Header Auth: `X-Bridge-Token`
   - Body: `{}` → 브릿지가 `claim_next_media_job()`으로 우선순위가 가장 높고 `run_after`가 지난 pending 작업 1건을 선점합니다.
   - Options → Response → **Never Error** ON. 대기 작업이 없으면 `409`가 정상 응답입니다.

한 번에 여러 건을 밀어 넣고 싶다면 이렇게 구성합니다.

1. **Schedule Trigger**
2. **Supabase** 노드
   - Credential: Host = `SUPABASE_URL`, Service Role Secret = `SUPABASE_SERVICE_ROLE_KEY`
   - Resource: Row, Operation: **Get Many**, Table: `media_queue`
   - Filter: `status` **Equal** `pending`, Limit: `10`
3. **HTTP Request** 노드: n8n은 아이템마다 한 번씩 실행하므로 Body는 `{ "job_id": "{{ $json.id }}" }`로 둡니다. 아직 `run_after`가 지나지 않은 재시도 대기 작업은 `409`를 받고 다음 주기에 다시 처리됩니다.

브릿지 내부 워커는 1개라 넣은 순서대로 GPU에서 차례로 처리됩니다.

---

## 4. 워크플로우 ②: 완료 → SNS 포스팅

### 4-1. 브릿지 콜백 받기 (권장)

> **바로 import:** [`n8n/02_media_done.json`](../n8n/02_media_done.json)에 아래 1~3단계와 `posts` 초안 생성(5단계, `status='draft'`)이 들어 있습니다. import한 뒤 `YOUR-PROJECT-REF`를 실제 프로젝트 ref로 바꾸고, Credential 두 개를 연결하세요.
> - Webhook 노드: Header Auth, Name `X-Callback-Token`
> - `posts 초안 생성` 노드: Supabase API (Host = `SUPABASE_URL`, Service Role Secret)

1. **Webhook** 노드
   - POST, Path: `media-done`, Header Auth → Name `X-Callback-Token`
   - Production URL을 `.env`의 `N8N_CALLBACK_URL`에, 토큰을 `N8N_CALLBACK_TOKEN`에 넣고 브릿지를 재시작합니다.
   - n8n이 클라우드에 있어도 이 방향(브릿지 → n8n)은 로컬에서 나가는 요청이라 터널이 필요 없습니다.
2. 브릿지가 보내는 바디
   ```json
   {
     "job_id": "…",
     "persona_id": "mina",
     "job_type": "image",
     "status": "done",
     "attempts": 1,
     "output_urls": ["https://<ref>.supabase.co/storage/v1/object/public/media/…png"],
     "output_paths": ["mina/image/2026/10/…/…png"],
     "error": null,
     "source_message_id": null
   }
   ```
3. **IF** 노드: `{{ $json.body.status }}` equal `done`
   - false 쪽(failed) → Slack/Discord/Telegram 알림 노드로 연결하면 실패를 바로 알 수 있습니다.
4. (선택) **AI / LLM** 노드로 캡션과 해시태그를 생성합니다.
5. `posts` 행 생성
   - n8n Supabase 노드의 필드 입력은 문자열만 받아서 `media_urls`(text[]) 같은 배열을 넣기 어렵습니다. 그래서 import 파일에서는 **HTTP Request** 노드로 Supabase REST API(`POST /rest/v1/posts`)를 직접 호출합니다.
   - Authentication은 `Predefined Credential Type` → `Supabase API`로 두면 `apikey`와 `Authorization` 헤더가 자동으로 붙습니다.
   - Body는 `JSON.stringify({...})`로 만들어 배열을 그대로 보냅니다.

   | 필드 | 값 |
   |---|---|
   | `persona_id` | `{{ $('Webhook').item.json.body.persona_id }}` |
   | `media_job_id` | `{{ $('Webhook').item.json.body.job_id }}` |
   | `platform` | `instagram` |
   | `media_urls` | `{{ $('Webhook').item.json.body.output_urls }}` |
   | `caption` | 이전 노드 결과 |
   | `status` | `scheduled` |
   | `scheduled_at` | `{{ $now.plus({ hours: 1 }).toISO() }}` |
6. **SNS 포스팅 스크립트 호출**
   - 스크립트를 HTTP 서비스로 만들었다면 **HTTP Request** 노드로 `posts.id`를 넘깁니다.
   - self-hosted n8n에서 로컬 Python 스크립트를 직접 실행하려면 **Execute Command** 노드를 씁니다.
     ```bash
     python D:/Projects/persona-automation-agent/src/post_publisher.py --post-id {{ $json.id }}
     ```
     `post_publisher.py`는 아직 없는 다음 단계 스크립트입니다. 최신 n8n은 보안상 Execute Command 노드가 기본으로 비활성화되어 있을 수 있으니, 이 경우 `NODES_EXCLUDE` 환경변수를 확인하세요.

### 4-2. 대안: Database Webhook으로 완료 감지

콜백 대신 Supabase Webhook을 `media_queue`의 **Update** 이벤트에 걸고, n8n IF 노드에서 다음 조건을 확인합니다.

- `{{ $json.body.record.status }}` equal `done`
- AND `{{ $json.body.old_record.status }}` not equal `done`

### 4-3. 예약 발행

`scheduled_at`에 맞춰 발행하려면 워크플로우를 하나 더 만듭니다.

1. **Schedule Trigger** (Every 5 minutes)
2. **Supabase** Get Many `posts`: `status` = `scheduled`, `scheduled_at` ≤ `{{ $now.toISO() }}`
3. 각 행의 `status`를 `publishing`으로 **Update** → 포스팅 스크립트 호출 → 결과에 따라 `published`(`external_post_id`, `published_at` 채움) 또는 `failed`(`error` 채움)로 **Update**

---

## 5. 전체 흐름 테스트

Supabase **SQL Editor**에서 작업을 하나 넣습니다.

```sql
insert into public.media_queue (persona_id, job_type, workflow, params)
values (
  'mina',
  'image',
  'txt2img_basic',
  '{
    "checkpoint": "sd_xl_base_1.0.safetensors",
    "prompt": "portrait of a young korean woman, cafe, soft light, photorealistic",
    "width": 832,
    "height": 1216
  }'::jsonb
)
returning id;
```

진행 상황을 확인합니다.

```sql
select id, status, attempts, error, output_urls, started_at, completed_at
from public.media_queue
order by created_at desc
limit 5;
```

폴링만 쓰거나 Webhook 없이 직접 테스트하려면 브릿지를 바로 호출합니다.

```bash
curl -X POST http://127.0.0.1:8000/jobs -H "X-Bridge-Token: <BRIDGE_TOKEN>" -H "Content-Type: application/json" -d "{\"job_id\": \"<위에서 받은 id>\"}"
```

---

## 6. 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `401 invalid bridge token` | n8n Header Auth의 Name이 정확히 `X-Bridge-Token`인지, 값에 공백이 섞이지 않았는지 확인 |
| `409 no claimable pending job` | 이미 processing/done인 작업, 재시도 대기 중(`run_after`가 아직 미래)인 작업, 또는 틀린 ID. 직접 다시 돌리려면 `update media_queue set status='pending', attempts=0, run_after=now() where id=…` |
| 작업이 `pending`인데 처리되지 않음 | `run_after`가 미래인 재시도 대기 상태일 수 있음. `error`의 `[시도 n/m]`을 확인하고, 3-2의 폴링이 켜져 있는지 확인 |
| `error`: `ComfyUI 가 워크플로우를 거부함 (400)` | 본문의 `node_errors`에서 어떤 노드가 문제인지 확인. 체크포인트 파일명 오타가 가장 흔함 |
| `error`: `params 에 값이 없는 자리표시자` | 템플릿의 `{{…}}` 키가 `params`에 없음. 예제 템플릿은 `checkpoint`, `prompt`가 필수 (또는 `.env`의 `DEFAULT_CHECKPOINT`) |
| `error`: `출력 파일이 없음` | 워크플로우에 `SaveImage` / `VHS_VideoCombine` 같은 저장 노드가 없거나 `PreviewImage`(temp)만 있음 |
| 작업이 `processing`에서 멈춤 | 처리 도중 브릿지가 종료됨. `status='pending', run_after=now()`로 되돌리면 다시 처리됨 |
| Supabase Webhook이 n8n에 도착하지 않음 | Test URL이 아니라 **Production URL**인지, 워크플로우가 Active인지 확인. SQL `select * from net._http_response order by created desc limit 10;`로 응답 코드 확인 |
| ngrok URL로 HTML 경고 페이지가 옴 | 요청 헤더에 `ngrok-skip-browser-warning: true` 추가 |
| Docker n8n에서 `ECONNREFUSED localhost:8000` | 컨테이너 안의 localhost는 컨테이너 자신. `host.docker.internal:8000`을 쓰고 브릿지는 `--host 0.0.0.0`으로 실행 |
