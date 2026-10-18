# MVP Checklist

> 두 가지를 한 곳에 둔다: ① **기능 하나를 끝낼 때마다 채우는 12칸** (UI만 만들고 "완료"라고 하는 것을 막는다), ② **MVP v1.0 완료 목록 24개** ([MVP_SCOPE_LOCK.md](MVP_SCOPE_LOCK.md) 8장).
> 규칙: 칸을 비워 둔 채 "완료"라고 하지 않는다. 해당 없으면 `N/A`와 이유를 적는다. 통과하지 못한 것은 결과와 함께 그대로 적는다.

## 1. 기능 완료 체크 (기능마다 복사해서 쓴다)

```text
기능: ____________________   Sprint/Milestone: ______   관련 TECH_DESIGN 절: ______

[ ] DB            마이그레이션(새 파일), 제약, 상태 값은 DB 값
[ ] RLS           소유 Persona 경유 읽기, 쓰기는 RPC, 교차 계정 테스트
[ ] Repository    (이 프로젝트는 Hook이 Supabase를 부른다) 쿼리·RPC 호출 함수, 생성한 타입
[ ] Hook          로딩·빈·오류·권한 상태, Realtime 구독 정리
[ ] UI            실제 상태만 표시(가짜 지표·가짜 진행률 없음), 위험한 동작은 확인, 한국어 문구
[ ] API           RPC / 브릿지 / Webhook 계약과 오류 코드
[ ] Error handling  error_code, retryable, 재시도 한도, 사람이 볼 수 있는 실패 표시
[ ] Idempotency   키, 중복 호출·응답 유실 시 결과, 불확실한 외부 호출은 확인 후 진행
[ ] Logging       execution_logs / state_transitions / system_errors, 비밀값·본문 없음
[ ] Tests         단위 + 통합 + 실패·복구, 동시 선점·멱등·교차 계정, 전체 pytest 통과 출력
[ ] Security      비밀값·RLS·입력 검증·인젝션·한도·긴급 정지 (.claude/skills/pa-security)
[ ] Documentation TECH_DESIGN / MVP_SCOPE_LOCK 갱신(아키텍처가 바뀌었다면 먼저), README 진행 상황
```

해당 없는 칸의 예: 순수 DB 함수 → `Hook`·`UI` N/A. 화면만 있는 읽기 전용 기능 → `Idempotency` N/A.

## 2. MVP v1.0 완료 목록 (24개)

범례: ☐ 미완 · ☑ 완료(근거는 4장 진행 기록) · "부분"은 일부만 확인한 것이라 완료가 아니다. 확인 방법은 기술 설계의 절이다. Sprint는 [MVP_SCOPE_LOCK.md](MVP_SCOPE_LOCK.md) 0장.

| # | 항목 | Sprint | 확인 방법 | 상태 |
|---|---|---|---|---|
| 1 | Google 로그인 (허용 목록 밖은 거부) | 1 | 24.3, 27.7 S3 | ☐ |
| 2 | Persona 생성 | 1 | 45장, 22장 | ☐ |
| 3 | Persona 활성화 (`active`) | 1 | 45장 | ☐ |
| 4 | Content Job 생성 (`queued`) | 1 | 27.4 | ☐ 부분 (SQL 직접 INSERT로 `queued` 확인. RPC·화면 경로는 미확인) |
| 5 | n8n Claim (Atomic, 중복 없음) | 1 | 27.6 D3, 49.5 3번 | ☐ |
| 6 | Python 실행 (브릿지 선점 → 생성) | 1 | 25.6 | ☑ Local (2026-10-18) |
| 7 | ComfyUI 생성 (활성 GPU: Local 또는 Cloud) | 1 | 25.6, 48.8 | ☑ Local (2026-10-18). Cloud는 호스팅 검증 전이라 미확인 |
| 8 | Asset 업로드 (Storage) | 1 | 27.4 | ☑ Local (2026-10-18). `media` 버킷의 PNG·썸네일(WebP) 공개 URL이 200이고 MIME·크기가 맞음. 화면에서 눈으로 본 확인은 아님 |
| 9 | Asset DB 등록 (lineage 칸 포함) | 1 | 27.3 확인 SQL, 50.3 | ☑ Local (2026-10-18). persona·content job·automation job·workflow(+버전)·model·seed·prompt·negative prompt·width/height 확인. LoRA는 쓰지 않아 null (N/A) |
| 10 | Asset 승인·반려 (사유) | 2 | 50.5 3번 | ☐ |
| 11 | Post 생성 (캡션 초안 → 제출) | 2 | 28.7, 51장 | ☐ |
| 12 | SNS 게시 (Instagram 또는 X) | 2 | 28.15 E2E, 관문 G2 | ☐ |
| 13 | Post ID 저장 (`external_post_id`, `permalink`) | 2 | 11.8 CHECK | ☐ |
| 14 | 성과 수집 (24h·168h Snapshot) | 3 | 29.20 테스트, 52장 | ☐ |
| 15 | Analytics 표시 (실제 값만) | 3 | 29.18 | ☐ |
| 16 | AI Decision 생성 (Structured) | 4 | 30.18, 54장 | ☐ |
| 17 | Decision 검증 (n8n 조기 + DB 최종) | 4 | 54.5 1번 테스트 | ☐ |
| 18 | Decision → 새 Content Job (승인 후, `source = 'agent'`) | 4 | 30.18 E2E | ☐ |
| 19 | Audit Log | 1~4 | `state_transitions`, `execution_logs`, `ai_decisions` | ☐ 부분 (`execution_logs` 11행 확인. `state_transitions`·`ai_decisions`는 미확인) |
| 20 | Emergency Stop (전역·플랫폼·Persona) | 2~4 | 33.16, 30.18 (`agent_enabled = false`) | ☐ |
| 21 | Retry (일시 오류 재시도, 최종 실패 표시) | 1 | 27.5 F2·F6 | ☐ |
| 22 | Idempotency (생성·게시·수집·결정) | 1~4 | 27.6 D1~D3, 43.8, 49.5 1번 | ☐ |
| 23 | RLS (교차 계정·교차 Persona) | 1~4 | 27.7 S2~S4, 50.5 1번, 51.5 3번 | ☐ |
| 24 | Error Monitoring (실패 Job·`system_errors` 확인) | 1 | 17.11 Error Center | ☐ |

**최종 인수:** 사람 손 없이 `Persona → Content Job → 생성 → Asset → 승인 → 게시 → 성과 → AI Decision → 새 Content Job`이 한 바퀴 돈다 (MVP_SCOPE_LOCK 1장, TECH_DESIGN 27.8의 확장). 기록은 27장 형식으로 남긴다.

## 3. 기능별로 채울 칸 (예)

| 기능 | DB | RLS | Hook | UI | API | Error | Idem | Log | Test | Sec | Doc |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Persona CRUD | ✔ | ✔ | ✔ | ✔ | RPC | ✔ | N/A | ✔ | ✔ | ✔ | ✔ |
| Content Job 생성 | ✔ | ✔ | ✔ | ✔ | RPC | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| 브릿지 생성 | ✔ | ✔ | N/A | N/A | `/v1/jobs` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Asset Library | N/A (읽기) | ✔ | ✔ | ✔ | RPC(`archive_asset`) | ✔ | N/A | ✔ | ✔ | ✔ | ✔ |
| Post 게시 | ✔ | ✔ | ✔ | ✔ | n8n/Adapter | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| 성과 수집 | ✔ | ✔ | ✔ | ✔ | n8n/`record_metrics` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| AI Decision | ✔ | ✔ | ✔ | ✔ | RPC + n8n | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |

`✔`는 "필요하고 해야 한다"는 뜻이다 (완료 표시가 아니다). 실제 진행은 기능마다 1장의 양식을 복사해 채운다.

## 4. 진행 기록

| 날짜 | 기능 | 12칸 | 비고 (미통과·N/A 사유) |
|---|---|---|---|
| 2026-10-18 | 로컬 전용 생성 E2E (Content Job → Asset), Local Execution Target | 확인 4칸: DB·API·Logging·Tests. 나머지는 아래 사유 | 아래 "2026-10-18 기록" 참고 |

### 2026-10-18 기록: 로컬 전용 생성 E2E (수동 확인)

범위: Local Execution Target, DB 디스패처(0012) + Worker Pull. n8n은 쓰지 않았다 (`[PA] 006`만 Publish, 나머지 Workflow는 로컬 전용이라 Publish하지 않음).

**흐름** (Content Job `5f1c6554…` → generation Job `474c732b…` → Asset `9d1d3537…`)

| 시각(UTC) | 단계 | 비고 |
|---|---|---|
| 10:46:11 | Content Job `queued` | SQL Editor에서 직접 INSERT (`created_by` null) |
| 10:46:14 | `DISPATCH` | DB 디스패처가 generation Job 생성, 주문 후 약 3초 |
| 10:46:16 | `BUILD` → `COMFYUI_QUEUE` | 브릿지(`python:local-1`)가 약 2초 만에 선점 |
| 10:46:16 → 10:47:32 | `COMFYUI_WAIT` | 이미지 생성 약 76초 |
| 10:47:32 → 10:47:33 | `VALIDATE` → `UPLOAD` → `COMPLETE` | |
| 10:47:33 | Content Job `ready`, Asset `generated` 1건 | `system_errors` 0건, 시도 1회 |

생성 정보: `image_generation_v1` v1.0, 모델 `sd_xl_base_1.0.safetensors`, seed 947396819, steps 30, cfg 7.0, `oom_downscaled` false. 워커 보고: target `local`, provider `local`, ComfyUI OK, GPU 이름과 VRAM은 워커가 `/system_stats`에서 보고한 값이다 (코드에 고정하지 않음).

**12칸 사유**
- DB ✔: 0012 디스패처와 pg_cron이 실제로 동작함 (동작으로 확인. `cron.job` 행은 직접 조회하지 못함).
- API ✔: 브릿지 Pull 경로 (`claim_next_automation_job`).
- Logging ✔: `execution_logs` 11행, 비밀값 없음.
- Tests ✔: `pytest tests -q` 470개 통과 (2026-10-18, 이 작업 시작 시점). 이번 E2E는 수동 확인이다.
- RLS: 이번 확인 범위 밖 (SQL Editor는 postgres 권한이라 RLS를 거치지 않음). 교차 계정 테스트는 `tests/db`에 있음.
- Repository·Hook·UI: N/A (Lovable을 거치지 않음).
- Error handling·Idempotency: 이번 실행에서는 실패·중복 경로를 일으키지 않아 확인하지 못함. 키 `generation:{content_job_id}:{run_number}`는 설계대로 쓰임.
- Security: 별도 검토 안 함.
- Documentation: `docs/n8n_guide.md` 정정 (n8n 2.x Publish 순서). 이 항목 추가.

**Storage·lineage 확인 (같은 날 추가)**
- 파일: 버킷 `media`(공개), 경로 `persona/{persona_id}/assets/{asset_id}.png`. PNG 공개 URL이 HTTP 200, `image/png`, 1,473,586 bytes. 썸네일 `{asset_id}_thumb.webp`가 200, `image/webp`, 22,240 bytes. 헤더만 확인했고 파일은 내려받지 않았다.
- DB: `assets`에 persona·content job·automation job·`file_name`·`storage_bucket`/`storage_path`·`mime_type`·`width`/`height`(1024×1024)·`workflow`가 채워져 있다. `generation_metadata` 키: workflow, workflow_version, model, lora, lora_strength, seed, steps, cfg, sampler, scheduler, width, height, prompt, negative_prompt, oom_downscaled, comfy_prompt_id, batch_index.

**남은 사실 (미통과 아님, 확인 필요)**
- `generation_metadata`에는 `resolution` 키가 없고 해상도는 `width`·`height`로 기록된다. `docs/lovable_master_prompt.md`의 Asset 상세 "생성 정보"가 `resolution`을 나열해 데이터와 어긋났으므로, 문서를 `width`·`height` 기준("{width}×{height}")으로 고쳤다 (2026-10-18, 코드와 데이터는 그대로). Lovable 화면 구현으로 확인하는 일은 남아 있다.
- 직접 INSERT한 Content Job은 `created_by`가 null이다 (SQL Editor에서는 `auth.uid()`가 없음). 이력 추적이 필요하면 INSERT에서 명시한다.
- 이미지를 화면에서 눈으로 본 확인, `state_transitions`, Lovable 화면 표시는 확인하지 못했다.
