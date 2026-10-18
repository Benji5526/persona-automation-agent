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
| 1 | Google 로그인 (허용 목록 밖은 거부) | 1 | 24.3, 27.7 S3 | ☐ 부분 (허용된 계정의 로그인·새로고침 유지·로그아웃·보호 경로는 사용자가 화면에서 확인. 허용 목록 밖 계정의 실제 거부는 미확인) |
| 2 | Persona 생성 | 1 | 45장, 22장 | ☑ Lovable 화면 (2026-10-18, 사용자 확인). 생성한 Persona가 DB에서 본인 소유로 확인됨 |
| 3 | Persona 활성화 (`active`) | 1 | 45장 | ☑ 생성 시 `active`, 그 Persona로 Job 생성까지 이어짐 (2026-10-18). 보관 후 재활성화 경로는 미확인 |
| 4 | Content Job 생성 (`queued`) | 1 | 27.4 | ☑ Lovable 화면의 `create_content_job` RPC (2026-10-18). DB에서 `created_by`가 로그인 사용자, `source = operator`, `execution_target = local` 확인 |
| 5 | n8n Claim (Atomic, 중복 없음) | 1 | 27.6 D3, 49.5 3번 | ☐ |
| 6 | Python 실행 (브릿지 선점 → 생성) | 1 | 25.6 | ☑ Local (2026-10-18) |
| 7 | ComfyUI 생성 (활성 GPU: Local 또는 Cloud) | 1 | 25.6, 48.8 | ☑ Local (2026-10-18). Cloud는 호스팅 검증 전이라 미확인 |
| 8 | Asset 업로드 (Storage) | 1 | 27.4 | ☑ Local (2026-10-18). `media` 버킷의 PNG·썸네일(WebP) 공개 URL이 200이고 MIME·크기가 맞음. Lovable `/assets`에서 이미지 표시와 다운로드를 사용자가 확인 |
| 9 | Asset DB 등록 (lineage 칸 포함) | 1 | 27.3 확인 SQL, 50.3 | ☑ Local (2026-10-18). persona·content job·automation job·workflow(+버전)·model·seed·prompt·negative prompt·width/height 확인. LoRA는 쓰지 않아 null (N/A) |
| 10 | Asset 승인·반려 (사유) | 2 | 50.5 3번 | ☑ Lovable 화면 (2026-10-18, 사용자 확인). 백엔드 `review_asset`(0013)은 로컬 DB 테스트 24개와 호스팅 적용 점검(칸 생성, authenticated만 실행)을 확인했고, 승인·사유 있는 반려·재승인은 사용자가 화면에서 통과시킴. 다운스트림(Post 게시)은 아직 이 상태를 쓰지 않음 |
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
| 21 | Retry (일시 오류 재시도, 최종 실패 표시) | 1 | 27.5 F2·F6 | ☐ 부분 (재시도 불가 오류 `LORA_NOT_FOUND`가 1회 시도로 즉시 실패하고 화면이 실패 단계·시도 횟수·재시도 불가를 표시함을 확인. 일시 오류의 재시도·백오프는 미확인) |
| 22 | Idempotency (생성·게시·수집·결정) | 1~4 | 27.6 D1~D3, 43.8, 49.5 1번 | ☐ |
| 23 | RLS (교차 계정·교차 Persona) | 1~4 | 27.7 S2~S4, 50.5 1번, 51.5 3번 | ☐ |
| 24 | Error Monitoring (실패 Job·`system_errors` 확인) | 1 | 17.11 Error Center | ☑ Lovable `/automation/errors` (2026-10-18, 사용자 Phase 5 통과 보고). 실패 Job의 오류 코드·한국어 설명·해결됨 표시 경로. 항목별 화면 캡처는 남기지 않았다 |

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
| 2026-10-18 | Lovable 웹 화면 Phase 1~4 (로그인, Persona, Content Job, Asset) | Hook·UI·API·Error·Logging 확인(수동). RLS·Idempotency·Security는 아래 사유 | 아래 "Lovable 화면 Phase 1~4 기록" 참고 |
| 2026-10-18 | Lovable 웹 화면 Phase 5·6 (대시보드·워커·오류·설정, 최종 점검) | Hook·UI·API·Security 확인(수동 + 코드 읽기). RLS는 아래 사유 | 아래 "Lovable 화면 Phase 5·6 기록" 참고. Lovable 서버 환경의 서비스 키 건 포함 |

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
- 이미지를 화면에서 눈으로 본 확인, `state_transitions`, Lovable 화면 표시는 확인하지 못했다. (Lovable `/assets` 표시·다운로드는 아래 기록에서 사용자가 확인함. `state_transitions`는 여전히 미확인.)

### Lovable 화면 Phase 1~4 기록 (2026-10-18)

범위: Lovable 프로젝트 `Benjiworld`(TanStack Start 기반)를 기존 Supabase 프로젝트에 연결해 Phase 1~4를 구현. 프롬프트는 `docs/lovable_master_prompt.md`에서 추출했고, 10,000자 제한 때문에 Project knowledge(규칙)와 `docs/brief/*.md`(데이터 모델, RPC, 화면 명세)로 나눠 넣었다. 진행 확인은 사용자가 화면에서, 코드는 Lovable MCP로 읽기 전용 점검했다.

| Phase | 내용 | 확인 |
|---|---|---|
| 1 | 쉘, Google 로그인, 보호 경로 | 사용자 확인: 로그인·새로고침 유지·로그아웃·메뉴 이동. 첫 시도는 Supabase에서 Google provider가 꺼져 있어(`provider is not enabled`) 막혔고, 로그인용 OAuth 클라이언트를 새로 만들어(n8n용과 분리) 해결 |
| 2 | Persona 목록·편집·Visual Identity·참조 이미지 | 사용자 통과. 코드: 하드 삭제 없음(보관만), 업로드 후 행 삽입과 실패 시 정리, 쓰기 가능 컬럼만 전송. LoRA 강도 범위를 명세(0~1.5)에 맞게 수정 |
| 3 | Content Job 목록·생성·상세 | 사용자 통과. 화면에서 만든 Job이 `준비됨`까지 감 (32초, 이미지 2장, `local`, 단계별 실제 시각 표시). 실패 경로(`LORA_NOT_FOUND`, 재시도 불가)도 단계·시도·재시도 불가를 표시. 코드: `create_content_job` RPC만 사용, in-flight ref로 중복 방지, Cloud 비활성 |
| 4 | Asset 갤러리·상세 | 사용자 통과(변형 만들기, 보관, 실시간 반영 포함). 이 시점 DB의 Asset은 10건. 코드: NULL-safe 숨김 필터, `archive_asset` RPC, 변형 만들기(`image_to_image_v1` + `init_image`), 승인·반려 비활성 |

**12칸 사유**
- RLS: Lovable 화면은 일반 사용자 토큰으로 접근하므로 RLS가 적용된다. 다른 계정으로의 교차 접근 확인은 허용 목록 때문에 UI로 할 수 없고, `tests/db`가 담당한다 (이번에 실행하지 않음).
- Idempotency: `create_content_job`은 멱등하지 않아 화면의 in-flight ref와 버튼 비활성에 기댄다. 의도적 더블클릭 시험의 결과는 기록하지 않았다.
- Security: 클라이언트에는 공개용 키만 있다. Lovable이 자동 생성한 `client.server.ts`는 서비스 키를 읽을 수 있게 만들어져 있으나 키가 설정돼 있지 않고 쓰는 코드도 없다 (Phase 6에서 다시 점검).
- Tests: Lovable이 보고한 자동 테스트(Phase 2는 11개, Phase 3은 20개, Phase 4는 31개 통과)는 이 기록의 작성자가 실행한 것이 아니다.

**확인했거나 정한 불일치**
- `LORA_NOT_FOUND` 한 코드가 "Persona에 LoRA가 선택되지 않음"과 "워커에 파일이 없음" 두 경우에 쓰여, 화면 안내가 앞의 경우에 부정확하다. Phase 6에서 판단한다.
- `[테스트 이미지 생성]`은 `content_jobs`에 draft를 직접 INSERT한 뒤 제출한다. 명세상 허용되지만 제출이 실패하면 제출 안 된 draft가 남을 수 있다.
- `LORA_NOT_FOUND` 문구 구분과 `[테스트 이미지 생성]` 중 앞의 것은 아래 "Phase 5·6 기록"에서 고쳤다.

### Lovable 화면 Phase 5·6 기록 (2026-10-18)

범위: Phase 5(대시보드, 워커, 오류 센터, 설정)와 Phase 6(전체 점검). 진행 확인은 사용자가 화면에서, 코드는 Lovable MCP로 읽기 전용 점검했다.

| Phase | 내용 | 확인 |
|---|---|---|
| 5 | 대시보드·헤더 상태, 워커·큐·자동화 Job 표, 오류 센터, 설정(프로필·테마·관리자 설정) | 사용자 통과. 코드: 모든 집계가 DB 조회값(가짜 지표 없음), 헤더의 실패 수는 `completed_at` 24시간 창(누적 실패 수 사용 안 함), 워커 응답 없음 기준 90초. 관리자 영역은 `role = admin`일 때만 보이고, 모든 설정 변경과 워커 Credential 폐기는 확인 창을 거침. Cloud는 선택 불가이고 기본 Target 변경은 항상 `local`만 보냄. 사용하는 RPC는 `get_app_settings`, `update_app_setting`, `list_worker_credentials`, `revoke_worker_credential`, `resolve_system_error` |
| 6 | 전체 점검과 합의한 수정 3건 | 사용자 통과. 수정: (1) 실행 한도의 JSON 입력칸을 기존 숫자 키별 입력칸으로 교체(바뀐 키만 전송, 0 이상). (2) `LORA_NOT_FOUND` 안내를 "Persona에 LoRA가 선택되지 않음"과 "워커에 파일 없음"으로 구분(재시도 판단은 브라우저가 하지 않음). (3) 허용 이메일 목록이 비면 확인 창에 가입 차단 경고. 변경은 요청 범위에 한정됐고 자동 생성 파일 `client.server.ts`는 수정되지 않음 |

**보안 발견과 조치**
- Phase 6 점검에서 Lovable 서버 환경에 `SUPABASE_SERVICE_ROLE_KEY`가 설정돼 있는 것이 드러났다. 저장소와 `.env`에는 없었고(공개용 키만 있었음), 이 키를 읽는 자동 생성 파일을 가져다 쓰는 코드도 없었다. 프로젝트 규칙(Lovable에 서비스 키를 두지 않는다)에 어긋나므로 조치했다.
- 그 값은 Supabase의 이름이 `default`인 secret 키였다 (사용자 확인). 이 PC의 브릿지는 `bridge` 키, n8n은 별도로 만든 키를 쓰므로 영향이 없음을 확인한 뒤 `default`를 삭제했다.
- 삭제 후 확인: 브릿지 정상(`ok`, ComfyUI 연결), `bridge` 키로 DB 조회 성공, 워커 보고 갱신. 확인하지 못한 것: `default`가 실제로 무효가 됐는지는 값을 갖고 있지 않아 직접 시험하지 못했다 (다른 키가 정상임만 확인).
- 정리가 남은 것: Lovable의 서버 환경 변수 자체를 지우는 일(플랫폼 관리 설정이라 사용자가 해야 함). 값은 이미 무효이므로 위험은 해소됐다.

**12칸 사유**
- RLS: 관리자 RPC는 DB 함수가 `require_admin()`으로 막는다. 화면의 숨김은 편의이고 보안 기준이 아니다. 운영자(비관리자) 계정으로 접근 차단을 확인하는 일은 하지 않았다.
- Tests: Lovable이 보고한 자동 테스트(Phase 5는 43개, Phase 6은 45개 통과)는 이 기록의 작성자가 실행한 것이 아니다.

**남은 발견 사항 (보안 이슈는 아님, 이번에 고치지 않음)**
- Asset 다운로드 주소를 Supabase 도메인으로 제한하지 않는다 (주소는 DB의 `public_url`에서만 오므로 위험은 낮다).
- 기본 오류·404 화면에 영어가 남아 있다.
- 일부 이미지·입력 데이터의 로딩·오류 처리가 부족하다.
- 일부 코드가 상태 상수 대신 문자열을 직접 쓴다 (값은 DB와 일치).
- 전체 번들에 금지 문자열이 없는지는 배포 산출물이 없어 확정하지 못했다. 앱을 게시한 뒤 다시 확인해야 한다.
- 오류 목록 쿼리가 `system_errors`와 `automation_jobs`를 연결해 가져온다. 이 연결이 실패하면 오류 목록 전체가 실패할 수 있어 사용자가 오류 센터를 열어 확인해야 한다 (Phase 6 통과 보고에 포함됨).

### Asset 승인·반려 백엔드 기록 (2026-10-18)

범위: 마이그레이션 `0013_review_asset.sql` — `review_asset(p_asset_id, p_decision, p_note)`와 `assets.review_note`(500자 이하). 설계는 TECH_DESIGN 12.4(V1 RPC 표), 50.5 3번. 호스팅 Supabase에는 아직 적용하지 않았고 Lovable 화면도 연결하지 않았다.

**동작**
- 전이: `generated·rejected → approved`, `generated·approved → rejected`. 같은 결정을 두 번, 보관된 Asset은 `PT409`. 허용 전이표와 가드(게시·예약·게시 중인 Post가 있으면 승인된 Asset은 반려 불가)는 0002에 이미 있어 새로 만들지 않았다.
- 반려할 때 사유를 저장하고(앞뒤 공백 제거, 빈 값은 null), 다시 승인하면 비운다. 승인에 사유를 보내면 `PT422`. 누가·언제는 `state_transitions`(actor, created_at, reason `operator:approve`/`operator:reject`).
- 내 Persona의 Asset만. 남의 Asset은 `PT404`. authenticated 전용이고 anon·워커는 실행할 수 없다. authenticated에게는 `assets` UPDATE 권한이 없어 직접 쓰기는 `42501`.

**12칸**
- DB ✔ 새 마이그레이션, 제약(`review_note ≤ 500`), 상태 값은 DB 값. RLS ✔ 내 Persona 경유, 쓰기는 RPC, 교차 계정 테스트.
- API ✔ 오류 코드 `PT404`·`PT409`·`PT422`. Error handling ✔. Logging ✔ `state_transitions`.
- Idempotency: N/A 같은 결정의 재호출은 `PT409`로 거부되고 아무것도 바꾸지 않는다(테스트로 확인).
- Tests ✔ `tests/db/test_review_asset.py` 24개. 로직을 일부러 망가뜨려(사유 초기화 제거, 소유 확인 제거, 승인에 사유 허용) 해당 테스트가 실패하는 것을 확인했다. 전체 `pytest tests -q` 496개 통과(이전 470개 + `review_asset` 24개 + 점검 블록 25 테스트 2개).
- Security ✔ 권한 표면 테스트(`test_permission_surface.py`)의 authenticated 허용 목록과 워커 거부 목록에 `review_asset` 추가, `verify_production.sql` 블록 5(16개)와 새 블록 25.
- Documentation ✔ `supabase/README.md`, `README.md`, `verify_production.sql`, 이 기록.
- Hook·UI: 이번 범위 밖. Lovable 화면의 승인·반려 버튼 연결은 호스팅 적용과 확인 뒤에 한다.

**진행 결과 (같은 날 이어서)**
- 호스팅 Supabase에 0013을 적용했다 (사용자 실행). 점검 SQL(블록 25)이 `true, true, false`로 나왔고, `assets.review_note` 칸이 생긴 것을 읽기 전용으로 확인했다. 블록 5의 전체 16개 목록은 호스팅에서 따로 확인하지 않았다.
- Lovable 브리프(규칙 13번, 데이터 모델, RPC 목록, `/assets/:id`)를 바꾸고 Project knowledge와 `docs/brief/` 파일을 갱신한 뒤, 승인·반려를 `review_asset`에 연결하는 화면 수정을 적용했다. 코드는 읽기 전용으로 점검했다(호출 RPC는 `review_asset`뿐, 사유는 반려에서만 전달하고 빈 값은 null, 버튼 노출 조건, 중복 클릭 방지).
- 사용자가 화면에서 승인, 사유 있는 반려, 사유 보기, 재승인을 통과시켰다. Lovable이 보고한 자동 테스트 60개는 이 기록의 작성자가 실행한 것이 아니다.
- 승인·반려는 지금은 Asset의 상태와 사유를 남기는 단계다. 이 상태를 읽어 게시 대상을 거르는 일은 Post 게시(항목 11~13)를 만들 때 한다.

**남은 사실**
- 반려로 Content Job에 유효한 Asset이 하나도 남지 않아도 Job은 `ready`로 남는다(설계에 이를 막는 규칙이 없다). 필요하면 별도로 결정한다.
- Google 로그인은 Lovable 편집기 안의 미리보기 패널(iframe)에서는 열리지 않는다(일반 403). 새 탭이나 새 창에서 열어야 한다.
