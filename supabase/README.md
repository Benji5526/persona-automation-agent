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

## 로컬 테스트

Docker 없이 내장 PostgreSQL(`pgserver`)에 Supabase 흉내 스키마와 0006을 뺀 모든 마이그레이션을 적용해 테스트한다. 0006(pg_cron)은 로컬에 확장이 없어 문법만 확인한다.

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest tests/db -q
```
