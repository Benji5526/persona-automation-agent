# Execution Target — Local ↔ Cloud GPU

> 정본: [TECH_DESIGN 56장](../TECH_DESIGN.md) (56.2 Target 고정, 56.3 Worker Pull, 56.4 DB, 56.5 Worker 인증·Capability, 56.6 Cloud 보안, 56.7 화면). 이 문서는 읽기 쉬운 요약이다.

## 용어 — 세 개념은 별개다

| 용어 | 뜻 | 어디에 |
|---|---|---|
| **Execution Target** | 일을 **어디서** 실행할지. `local` \| `cloud` | `content_jobs.execution_target`, `automation_jobs.execution_target` |
| **Worker** | 실제 워커 **인스턴스** | `worker_status.id`, `automation_jobs.claimed_by` (예 `python:local-1`) |
| **GPU Provider** | 인프라를 **누가** 제공하나 | `worker_status.provider` (`local`, `runpod` …) |

하나의 설정(`active_worker`)으로 합치지 않는다. 기본 Target만 설정에 있다: `app_settings.default_execution_target`.

## 규칙

1. **Target은 Content Job 생성 시점에 고정된다.** 기본값은 `default_execution_target`, `create_content_job(p_execution_target)`으로 그 Job만 바꿀 수 있다. `automation_jobs`는 상위에서 상속한다(트리거가 강제).
2. **기본 Target을 바꿔도 기존 Job은 움직이지 않는다.** 이동은 `move_pending_jobs_to_target(p_target)` 하나뿐이고, **대기 중 generation Job만** 옮기며 감사 이벤트를 남긴다.
3. **자동 failover 없음.** 다른 Target에서 다시 돌리려면 `retry_automation_job(job_id, target)`로 명시한다.
4. **선점은 DB가 정한다.** Python이 local/cloud를 고르지 않는다. Job의 Target과 **인증된** 워커 Target이 같아야 선점된다.
5. **GPU 모델을 코드·설정·문서에 고정하지 않는다.** 모델명·VRAM은 워커가 `/system_stats`로 보고한다.
6. **Frontend는 GPU 인프라를 모른다.** Lovable → Supabase(RPC)뿐이다. 주소·키·Pod ID·서명 키·SNS 토큰은 DB·Frontend에 두지 않는다.
7. **ComfyUI는 어디서도 인터넷에 노출하지 않는다** (`127.0.0.1:8188`).
8. 수동 생성과 AI 생성은 **같은 파이프라인**을 쓴다.

## 디스패치: Worker Pull이 기본

```text
브릿지 (5초마다): 놀고 있고 ComfyUI가 살아 있으면
   claim_next_automation_job('generation', WORKER_ID)
   → DB가 job.execution_target == 워커 Target 일 때만 Job을 준다
      워커 Target은 Worker JWT 클레임 → worker_status 보고값 → 'local' 순서로 서버가 정한다
```

- Cloud Pod는 **밖으로 나가는 연결만** 있으면 된다 (Supabase). 터널·Access·인바운드가 필요 없다.
- push(`POST /v1/jobs`, WF-003)는 **기본 비활성 local 전용 fallback**이다. Cloud는 Pull만 쓴다.

## 디스패치 앞 단계: `queued` → generation Job

Worker Pull은 **generation Job이 이미 있을 때** 가져간다. 그 Job을 만드는 구간(Local-Only MVP)은 n8n이 아니라 **DB 함수 `dispatch_queued_content_jobs()` + pg_cron**이 맡는다 (TECH_DESIGN 56.10). 프롬프트가 있는 주문만 다루며, Target·Workflow는 고르지 않고 Content Job의 것을 그대로 따른다. n8n WF-001·002는 삭제하지 않았고 이 구간에 필요하지 않다.

## Worker 인증

```text
Cloud Worker → Worker JWT (role=pa_worker) → PostgREST → pa_worker → 허용된 RPC만
```

- 클레임: `worker_id`, `target`, `provider`, `jti`, `exp`, `role=pa_worker`. 수명 **≤ 24시간**.
- 매 호출마다 자격의 존재·폐기·만료·신원 일치·Target 일치를 확인한다. 신원을 요청 본문에서 받지 않는다.
- `pa_worker`는 **테이블 권한이 없다.** 실행 맥락은 `worker_get_job_context(job_id)`로만 받는다 (SNS 토큰·다른 Persona·무관한 Job 제외).
- 토큰 발급은 `scripts/mint_worker_token.py` (소유자 PC). **서명 키는 Pod에 넣지 않는다.**
- 폐기(`revoke_worker_credential`) 즉시 그 워커의 RPC가 막힌다. 워커는 새 Job 선점을 멈추고, 실행 중인 작업을 안전하게 멈추고(ComfyUI 작업 취소, DB의 Job은 Heartbeat가 끊겨 회수·재시도), 프로세스에 정상 종료를 요청한다. 거부된 자격으로 보고를 되풀이하지 않는다.
- Local은 Phase 0 동안 `WORKER_AUTH=secret_key`를 써도 된다. `worker_jwt`도 같은 코드 경로다.

### 세션 종류 — 신원이 깨져도 `local`로 폴백하지 않는다

| DB 역할 (PostgREST가 검증된 JWT로 정한다) | 종류 | 동작 |
|---|---|---|
| `pa_worker` | worker | **유효한 자격 필수.** 깨지면 `FORBIDDEN`(빈 결과가 아님). 자기가 선점한 **generation Job만** 다룬다 |
| `anon`, `authenticated` | untrusted | 워커 함수는 EXECUTE가 없다 (Lovable은 워커 RPC를 못 부른다) |
| `service_role` | trusted | 로컬 Phase 0 |

Worker 세션의 범위: 선점은 generation만 · heartbeat·complete·fail·register_asset은 **`claimed_by`가 자기**일 때만(`locked_at`이 새도) · 로그는 자기 Job에만 · 보안 이벤트는 정해진 3종·`actor_type=python`·자기 Persona로만 · 상태 보고는 자기 자신만(target·provider는 자격에서) · 입력 Asset은 Job이 참조한 것만.

**service_role 경로의 의도적 완화**: 보호 대상 F0 테스트가 **등록되지 않은** service_role 워커의 local 선점을 요구한다. 그래서 service_role 경로에서 미등록 워커는 `local`로만 취급한다 (cloud Job은 못 가져간다). service_role은 전권이고 스스로 등록할 수 있어 등록 강제가 보안을 늘리지 않는다. **실제 경계는 `pa_worker` 경로다.** 소유자가 받아들여야 하는 결정이다 (TECH_DESIGN 56.9 #9).

## Storage 격리

워커는 **자기가 지금 선점한 generation Job** 기준으로만 Storage에 닿는다 (`storage.objects` RLS 정책 2개).

- **읽기**: Job이 `input_images`로 참조한 파일 하나하나만. 경로 추측·같은 Persona의 다른 파일·목록 조회 모두 안 보인다.
- **쓰기**: `media/persona/{Job의 Persona}/assets/{uuid}[_thumb].{png|jpg|webp}`만. 등록된 Asset 경로·경로 조작·다른 확장자 불가.
- **UPDATE·DELETE 권한 없음** → 덮어쓰기·삭제 불가 (JWT 모드는 `x-upsert`를 보내지 않는다). Job이 끝나거나 자격이 폐기·만료되면 접근도 끝난다.
- **한계**: 경로에 Job id가 없어서 쓰기 경계는 Job이 아니라 "선점한 Job의 Persona"다 (등록은 `register_asset`이 선점한 Job에만 허용). 호스팅 Storage API가 `pa_worker`로 이 정책을 적용하는지는 로컬에서 검증할 수 없다.

## Capability 검증 (실행 전)

```text
Workflow → 구조 + 고정 파일     registry의 requirements
Persona  → 모델·LoRA 선택        visual_settings
Worker   → 실제 설치 상태        /object_info, /system_stats
```

```json
"requirements": { "min_vram_gb": 8, "required_files": [{"node": "…", "input": "…", "file": "…"}] }
```

- `required_models`·`required_loras`는 **두지 않는다** — 모델·LoRA 선택은 Persona의 몫이다.
- 실패 코드: `WORKFLOW_UNSUPPORTED_ON_TARGET`(VRAM 부족·고정 파일 없음), `MODEL_NOT_FOUND`, `LORA_NOT_FOUND`.
- **Capability 실패는 재시도하지 않는다.** 실행 중 `OUT_OF_MEMORY`(일시적, 재시도·축소 대상)와 섞지 않는다.

## 설정 예 (워커 `.env`, 비밀이므로 DB에 두지 않는다)

```env
# Local (이 PC) — Phase 0
WORKER_AUTH=secret_key
WORKER_ID=python:local-1
WORKER_TARGET=local
WORKER_PROVIDER=local
PULL_JOBS=true
COMFY_URL=http://127.0.0.1:8188
SUPABASE_SECRET_KEY=<로컬 전용 키>

# Cloud (Runpod Pod 안) — service_role 키도, 서명 키도 두지 않는다
WORKER_AUTH=worker_jwt
WORKER_JWT=<mint_worker_token.py로 발급, ≤24시간>
SUPABASE_ANON_KEY=<공개 키>
WORKER_ID=python:cloud-1
WORKER_TARGET=cloud
WORKER_PROVIDER=runpod
PULL_JOBS=true
COMFY_URL=http://127.0.0.1:8188
```

브릿지는 `WORKER_AUTH=worker_jwt`인데 `SUPABASE_SECRET_KEY`·`JWT_SIGNING_SECRET`·`SNS_*_TOKEN`이 환경에 있으면 **기동을 거부**한다.

## 남은 과제 / 위험

- **호스팅 검증 (외부 배포 게이트, NOT VERIFIED)**: 실제 Supabase 프로젝트에서 한 번 실행한다. 통과하기 전에는 Cloud를 켜지 않고, **`service_role`로 대체하지 않는다.**
  1. `supabase/verify_production.sql` 20~23번 블록 — 22번이 1행(authenticator → pa_worker)이어야 한다. 0행이면 `grant pa_worker to authenticator;`를 한 번 실행한다.
  2. `python scripts/smoke_worker_jwt.py` — PostgREST가 `role = pa_worker` JWT를 받아 전환하는지, 거부·사칭·만료·폐기·Storage 거부를 확인한다. `FAIL`이면 경계가 깨진 것이고, `BLOCKED`면 플랫폼이 이 방식을 받아들이지 않는 것이다(사유가 출력된다).
  3. 스크립트가 출력하는 수동 절차 — 선점된 cloud Job으로 Storage의 허용 쓰기·읽기와 거부(덮어쓰기·다른 경로)를 확인한다.
  - 알아 둘 것: Supabase가 비대칭 JWT Signing Keys만 쓰고 레거시 HS256 secret을 끈 프로젝트에서는 `mint_worker_token.py`의 토큰이 거부될 수 있다 (BLOCKED로 나온다). 레거시 secret 유지 또는 직접 가져온 서명 키가 필요하다.
- **Capability 요구사항은 배포 설정이다**: 레지스트리의 `requirements`는 대상 GPU에서 측정한 뒤 채운다. 현재 5개 Workflow 모두 `UNSET`이다 (`python scripts/report_capability_requirements.py`, 배포 점검은 `--strict`). `UNSET`이면 VRAM 사전 검사가 없고 실행 중 `OUT_OF_MEMORY`(재시도·축소)가 대신 잡는다.
- **거부된 자격 사용은 DB에 기록되지 않는다**: 폐기·만료된 자격의 RPC는 DB가 예외로 거부해서(롤백) 스스로 이벤트를 남길 수 없다. 워커 로그에만 남는다. 발급·폐기는 감사 이벤트로 남는다.
- **켜 둔 Pod의 비용**: 지금은 수동 시작·종료.
- **LoRA·모델 불일치**: 가장 흔한 전환 실패. 경고만 하고 자동 동기화는 하지 않는다.
- **VRAM 차이**: 이 PC는 RTX 3070 8GB다. 큰 모델은 Cloud에서 돌린다.
