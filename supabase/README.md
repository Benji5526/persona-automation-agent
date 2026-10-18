# Supabase 마이그레이션

설계: [TECH_DESIGN.md](../docs/TECH_DESIGN.md) 10번(DB), 11번(State Machine), 12번(API), 15번(Security), 21번(Supabase 구현 정리)

| 파일 | 내용 |
|---|---|
| `migrations/0001_core_tables.sql` | MVP 테이블, 제약, 인덱스, Realtime |
| `migrations/0002_state_machine.sql` | 허용된 상태 전환, Guard, Rollup, `state_transitions` 기록 |
| `migrations/0003_rpc_operator.sql` | Lovable이 쓰는 Operator RPC |
| `migrations/0004_rpc_worker.sql` | n8n·브릿지가 쓰는 Worker RPC |
| `migrations/0005_security.sql` | 권한 회수, RLS, 가입 허용 목록, Storage 버킷·정책 |
| `migrations/0006_cron.sql` | pg_cron: 멈춘 Job 회수 (1분마다) |
| `migrations/0007_workers_settings.sql` | `worker_status`, 설정 RPC, 오류 해결 RPC |
| `migrations/0008_llm_limit_models.sql` | LLM 하루 호출 한도 `reserve_llm_call`, 설치된 모델 목록 `worker_status.models` |
| `migrations/0009_persona_isolation.sql` | Persona 격리 보강(Job·Post의 Persona 일치 트리거), 보관 Persona의 재시도·재생성 거부, Asset Library index (TECH_DESIGN 36.12, 47.3, 50.5) |
| `migrations/0010_execution_targets.sql` | Execution Target: `app_settings.default_execution_target`·`app_mode`, `content_jobs`·`automation_jobs.execution_target`(생성 시점 고정 + 상속), `worker_status.target`·`provider`, Target 기준 선점 gating, `move_pending_jobs_to_target`, `retry_automation_job(target)` (TECH_DESIGN 56.4) |
| `migrations/0011_worker_auth.sql` | Worker 인증: `worker_credentials`(발급 대장, 서명 키는 저장 안 함), `pa_worker` 역할(테이블 권한 없음), JWT 클레임 기반 신원, 세션 종류(깨진 자격은 오류)와 선점자 범위, `worker_get_job_context`, 자격 발급·폐기, **Storage 격리 정책** (TECH_DESIGN 56.5) |
| `migrations/0012_dispatch_queued_content_jobs.sql` | Local 디스패처: `dispatch_queued_content_jobs()`(프롬프트가 있는 `queued` Content Job → `generation` Job, 원자적·멱등·동시 실행 안전, 정지 스위치 존중)와 pg_cron 스케줄 `dispatch-queued-content-jobs` (TECH_DESIGN 56.10) |
| `migrations/0013_review_asset.sql` | Asset 검토: `review_asset(p_asset_id, p_decision, p_note)`(`generated·rejected → approved`, `generated·approved → rejected`, 내 Persona의 Asset만, 게시·예약 중인 Post가 있으면 반려 불가는 0002 가드), 반려 사유 `assets.review_note`(500자 이하, 다시 승인하면 비움). authenticated 전용 (TECH_DESIGN 12.4, 50.5 3번) |
| `migrations/0014_emergency_stop.sql` | 전역 긴급 정지: `app_settings.generation_enabled`(기본 켜짐, 값이 true가 아니거나 행이 없으면 닫힘), `emergency_stop_all()`(admin, 생성·게시 스위치를 한 번에 끄고 `security_events`에 기록, 멱등), 선점 함수(`claim_content_job`, `claim_automation_job`, `claim_next_automation_job`)가 꺼져 있으면 새 일을 시작하지 않음(진행 중인 작업은 끝냄), `get_app_settings`·`update_app_setting`이 `generation_enabled`를 다룸(스위치마다 따로 다시 켬). Persona·플랫폼 정지는 막을 대상이 생길 때 (TECH_DESIGN 32.6, 33.10) |
| `tests/stubs/supabase_stub.sql` | **로컬 테스트 전용.** 실제 프로젝트에 적용하지 않는다 |
| `verify_production.sql` | 실제 프로젝트 적용 후 점검 (읽기 전용, TECH_DESIGN 24.4) |

## 실제 프로젝트에 적용하기 (M0 이후)

전체 순서(Google OAuth, URL 설정, API Key 분리, 백업 포함)는 [TECH_DESIGN.md](../docs/TECH_DESIGN.md) 24.3이 정본이다. 아래는 그중 DB 부분이다.

1. [Supabase CLI](https://supabase.com/docs/guides/cli)를 설치하고 프로젝트에 연결한다.
   ```bash
   supabase login
   supabase link --project-ref <project-ref>
   ```
2. 마이그레이션을 적용한다.
   ```bash
   supabase db push
   ```
3. Dashboard → Authentication → Sign In / Providers에서 **Google만 켜고 Email·Phone·Anonymous 로그인은 끈다.** 이메일 가입이 켜져 있고 이메일 확인이 꺼져 있으면, 다른 사람이 허용 목록의 주소로 먼저 가입할 수 있다.
4. **Google 로그인 전에** Operator 이메일을 가입 허용 목록에 넣는다 (SQL Editor). 비어 있으면 아무도 가입할 수 없다.
   ```sql
   update public.app_settings
      set value = '["you@example.com"]'::jsonb
    where key = 'allowed_emails';
   ```
5. 첫 로그인 후 자기 계정을 admin으로 바꾼다 (Settings 화면의 설정 변경 권한, TECH_DESIGN 17.5).
   ```sql
   update public.users set role = 'admin' where email = 'you@example.com';
   ```
6. Dashboard → Advisors → Security Advisor에서 경고가 없는지 확인한다.
7. SQL Editor에서 `verify_production.sql`을 블록마다 실행하고 주석의 기대 결과와 비교한다 (TECH_DESIGN 24.4).

> 스키마는 이 폴더의 마이그레이션으로만 바꾼다. Dashboard·SQL Editor·Lovable에서 테이블이나 정책을 만들거나 고치지 않는다 (TECH_DESIGN 24.2, 24.7).

## Cloud 활성화 전 외부 검증 (실제 프로젝트에서 한 번, TECH_DESIGN 56.8)

0010·0011은 로컬 PostgreSQL에서 신규 설치와 업그레이드를 모두 검증했다. **호스팅 Supabase가 `role = pa_worker` JWT를 받아 주는지**는 로컬로 알 수 없다 — 아래를 실제 프로젝트에서 실행하고, 통과하기 전에는 Cloud를 켜지 않는다. 통과 전에는 이 항목을 "검증됨"이라고 쓰지 않는다.

1. **권한 상태** — `verify_production.sql` 20~23번 블록을 실행해 주석의 기대와 비교한다. 특히 22번은 `authenticator → pa_worker` 1행이어야 한다. 0행이면(0011 적용 시점에 그 역할이 없었다) 한 번 실행한다: `grant pa_worker to authenticator;`
2. **JWT 역할 매핑** — 소유자 PC에서 환경변수 `SUPABASE_URL`·`SUPABASE_ANON_KEY`·`SUPABASE_JWT_SECRET`·`PA_OWNER_USER_ID`를 채우고 실행한다. **service_role 키는 쓰지 않는다.**
   ```bash
   .venv/Scripts/python scripts/smoke_worker_jwt.py --dry-run   # 점검 항목과 수동 절차만 출력
   .venv/Scripts/python scripts/smoke_worker_jwt.py             # 실제 실행: PASS / FAIL / BLOCKED
   ```
   - `PASS` 전부 → 아래 3번으로. `FAIL`(종료 코드 2) → 보안 경계가 깨졌다. Cloud를 켜지 않는다. `BLOCKED`(종료 코드 3) → 플랫폼이 이 방식을 받아들이지 않는다. 출력된 사유를 기록하고 승인된 fallback을 쓴다.
   - JWT Signing Keys(비대칭)만 쓰고 레거시 HS256 secret을 끈 프로젝트에서는 토큰이 거부될 수 있다 (`BLOCKED`).
3. **Storage** — 스크립트가 마지막에 출력하는 수동 절차: 선점된 cloud Job 하나로, 그 Job의 Persona 경로에는 올릴 수 있고(`persona/{persona}/assets/{새 uuid}.png`) 같은 이름·다른 Persona·`..`·다른 확장자로는 올릴 수 없는지, 입력은 Job이 참조한 파일만 읽히는지 확인한다. Storage API가 버킷 조회에 `storage.buckets` SELECT를 요구하면 그 권한 하나만 새 마이그레이션(0012)으로 더하고 `tests/db/test_permission_surface.py`를 함께 고친다.
4. **Capability 요구사항** — `python scripts/report_capability_requirements.py`로 Workflow별 `UNSET`을 확인하고, 대상 GPU에서 측정한 값을 `workflows/registry.json`의 `requirements`에 적는다 (`--strict`는 UNSET이 남으면 실패한다).

## 로컬 테스트

Docker 없이 내장 PostgreSQL(`pgserver`)에 Supabase 흉내 스키마와 0006을 뺀 모든 마이그레이션을 적용해 테스트한다. 0006(pg_cron)은 로컬에 확장이 없어 문법만 확인한다.

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest tests/db -q
```
