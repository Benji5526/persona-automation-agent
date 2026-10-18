-- =============================================================================
-- 실제 Supabase 적용 후 점검 (TECH_DESIGN 24.4)
--   읽기 전용이다. 아무것도 바꾸지 않는다. SQL Editor에서 블록마다 실행하고
--   각 블록의 "기대" 주석과 결과를 비교한다. 다르면 24.4 표의 조치를 따른다.
-- =============================================================================

-- 1. 마이그레이션 적용 버전
--    기대: 0001 ~ 0013 열세 줄 (파일 이름 앞부분이 version)
select version
  from supabase_migrations.schema_migrations
 order by version;

-- 2. RLS가 꺼진 public 테이블
--    기대: 0행
select c.relname as table_without_rls
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity;

-- 3. anon 역할의 테이블 권한
--    기대: 0행 (anon은 아무것도 못 한다)
select table_name, privilege_type
  from information_schema.role_table_grants
 where table_schema = 'public' and grantee = 'anon';

-- 4. anon이 실행할 수 있는 함수 (public, private 스키마)
--    기대: 0행
select n.nspname as schema, p.proname as function_name
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
 where n.nspname in ('public', 'private')
   and has_function_privilege('anon', p.oid, 'execute');

-- 5. authenticated(Lovable)가 실행할 수 있는 public 함수
--    기대: 16개 — archive_asset, cancel_content_job, create_content_job,
--          get_app_settings, get_dashboard_summary, regenerate_content_job, resolve_system_error,
--          retry_automation_job, retry_content_job, submit_content_job, update_app_setting
--          + 0010: move_pending_jobs_to_target (admin)
--          + 0011: issue_worker_credential, list_worker_credentials, revoke_worker_credential
--            (함수 안에서 private.require_admin()으로 막는다. revoke는 Worker가 자기 자격만 폐기할 수도 있다)
--          + 0013: review_asset (Asset 승인·반려. 내 Persona의 Asset만, 반려 사유는 assets.review_note)
--    claim_*, complete_*, fail_*, heartbeat_*, register_asset, report_worker_status,
--    reserve_llm_call, dispatch_queued_content_jobs, sync_workflow_registry 등이 보이면 안 된다.
select p.proname as function_name
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public'
   and has_function_privilege('authenticated', p.oid, 'execute')
 order by 1;

-- 6. service_role(n8n·브릿지)이 Worker RPC를 실행할 수 있는지
--    기대: 모든 칸 true
select
  has_function_privilege('service_role', 'public.claim_content_job(uuid)', 'execute')                       as claim_content_job,
  has_function_privilege('service_role', 'public.claim_automation_job(uuid, text)', 'execute')              as claim_automation_job,
  has_function_privilege('service_role', 'public.claim_next_automation_job(text, text)', 'execute')         as claim_next_automation_job,
  has_function_privilege('service_role', 'public.heartbeat_automation_job(uuid, timestamptz)', 'execute')   as heartbeat,
  has_function_privilege('service_role', 'public.complete_automation_job(uuid, timestamptz, jsonb)', 'execute') as complete,
  has_function_privilege('service_role',
    'public.fail_automation_job(uuid, timestamptz, text, text, text, boolean, integer, text)', 'execute')    as fail,
  has_function_privilege('service_role', 'public.register_asset(uuid, timestamptz, jsonb)', 'execute')       as register_asset,
  has_function_privilege('service_role', 'public.report_worker_status(text, text, jsonb)', 'execute')        as report_worker_status,
  has_function_privilege('service_role', 'public.reserve_llm_call(uuid, timestamptz)', 'execute')            as reserve_llm_call;

-- 7. status 칸에 authenticated의 쓰기 권한이 있는지
--    기대: personas.status의 INSERT·UPDATE 두 줄만 (예외로 허용됨). 그 밖의 줄이 있으면 회수
select table_name, column_name, privilege_type
  from information_schema.column_privileges
 where table_schema = 'public' and grantee = 'authenticated'
   and column_name in ('status', 'role', 'user_id', 'created_by', 'source')
   and privilege_type in ('INSERT', 'UPDATE')
 order by 1, 2, 3;

-- 8. 가입 트리거
--    기대: on_auth_user_created 1행, 함수는 private.handle_new_user
select t.tgname, p.pronamespace::regnamespace as fn_schema, p.proname as fn
  from pg_trigger t
  join pg_proc p on p.oid = t.tgfoid
 where t.tgrelid = 'auth.users'::regclass and not t.tgisinternal;

-- 9. 가입 허용 목록과 계정
--    기대: allowed_emails에 Operator 이메일(소문자), users에 본인 1행 (첫 로그인 후 role = admin)
select value as allowed_emails from public.app_settings where key = 'allowed_emails';
select email, role, created_at from public.users order by created_at;

-- 10. Storage 버킷
--     기대: media (public = true), persona-private (public = false), 둘 다 52428800
select id, public, file_size_limit, allowed_mime_types
  from storage.buckets
 where id in ('media', 'persona-private');

-- 11. Storage 정책 (persona-private 4개)
--     기대: persona_private_select / insert / update / delete
select policyname, cmd
  from pg_policies
 where schemaname = 'storage' and tablename = 'objects'
 order by policyname;

-- 12. Realtime 대상
--     기대: assets, automation_jobs, content_jobs, posts, worker_status
select tablename
  from pg_publication_tables
 where pubname = 'supabase_realtime' and schemaname = 'public'
 order by 1;

-- 13. 확장과 pg_cron 작업
--     기대: pg_cron, pg_net 모두 있음. recover-stale-jobs는 '* * * * *', dispatch-queued-content-jobs는
--           '10 seconds'(pg_cron이 초 단위를 모르면 '* * * * *')로 각각 active (0012, TECH_DESIGN 56.10)
select extname, extversion from pg_extension where extname in ('pg_cron', 'pg_net');
select jobname, schedule, active from cron.job
 where jobname in ('recover-stale-jobs', 'dispatch-queued-content-jobs') order by jobname;

-- 14. pg_cron 최근 실행 결과 (적용 후 몇 분 지나서)
--     기대: status = 'succeeded'
select j.jobname, d.status, d.return_message, d.start_time
  from cron.job_run_details d
  join cron.job j on j.jobid = d.jobid
 where j.jobname in ('recover-stale-jobs', 'dispatch-queued-content-jobs')
 order by d.start_time desc
 limit 5;

-- 15. Database Webhook (n8n 연결 후, n8n_guide 5절)
--     기대: content_jobs에 pa_content_jobs, automation_jobs에 pa_automation_jobs 트리거
select c.relname as table_name, t.tgname as webhook
  from pg_trigger t
  join pg_class c on c.oid = t.tgrelid
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and not t.tgisinternal and t.tgname like 'pa\_%'
 order by 1;

-- 16. Webhook 전달 결과 (n8n 연결 후)
--     기대: 최근 응답 status_code 200
select id, status_code, error_msg, created
  from net._http_response
 order by created desc
 limit 10;

-- 17. Worker 상태 (브릿지·n8n 연결 후)
--     기대: python:local-1과 n8n, last_seen_at이 90초 이내. python 행의 models에 설치된 체크포인트·LoRA 수
select id, kind, comfyui_ok, gpu ->> 'name' as gpu, queue_size, last_seen_at,
       jsonb_array_length(coalesce(models -> 'checkpoints', '[]')) as checkpoints,
       jsonb_array_length(coalesce(models -> 'loras', '[]')) as loras,
       last_seen_at > now() - interval '90 seconds' as online
  from public.worker_status
 order by id;

-- 18. 실행 설정
--     기대: limits, retry_backoff_seconds [30,120,300,900], heartbeat_timeout_seconds
select key, value from public.app_settings
 where key in ('limits', 'retry_backoff_seconds', 'heartbeat_timeout_seconds', 'publishing_enabled')
 order by key;

-- 19. 0009 persona_isolation (TECH_DESIGN 36.12, 50.5)
--     기대: automation_jobs_persona_match, posts_persona_match 트리거 2개, index assets_persona_created_idx 1개
select 'trigger' as kind, tgname as name from pg_trigger
 where tgname in ('automation_jobs_persona_match', 'posts_persona_match') and not tgisinternal
union all
select 'index', indexname from pg_indexes
 where schemaname = 'public' and indexname = 'assets_persona_created_idx'
 order by 1, 2;

-- 20. 0010 execution_targets (TECH_DESIGN 56.4)
--     기대: app_mode = "personal", default_execution_target = "local",
--           active_worker 행은 **없어야 한다** (구 설계 제거)
select key, value from public.app_settings
 where key in ('app_mode', 'default_execution_target', 'active_worker') order by key;

--     기대: content_jobs.execution_target(not null), automation_jobs.execution_target, worker_status.target·provider
select table_name, column_name, is_nullable from information_schema.columns
 where table_schema = 'public'
   and (table_name, column_name) in (('content_jobs', 'execution_target'),
                                     ('automation_jobs', 'execution_target'),
                                     ('worker_status', 'target'), ('worker_status', 'provider'))
 order by 1, 2;

--     기대: execution_target이 authenticated의 INSERT·UPDATE 권한에 **없어야 한다** (RPC 우회 차단)
select privilege_type, count(*) as leaks from information_schema.column_privileges
 where table_schema = 'public' and table_name = 'content_jobs' and column_name = 'execution_target'
   and grantee = 'authenticated' and privilege_type in ('INSERT', 'UPDATE')
 group by 1;

--     기대: 상속 트리거 1개, Target RPC 3개 (move_pending_jobs_to_target, 2인자 retry, 13인자 create_content_job)
select tgname from pg_trigger where tgname = 'automation_jobs_inherit_execution_target';
select p.proname, p.pronargs from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public'
   and p.proname in ('move_pending_jobs_to_target', 'retry_automation_job', 'create_content_job')
 order by 1, 2;

-- 21. 0011 worker_auth (TECH_DESIGN 56.5)
--     기대: worker_credentials 테이블 + RLS 켜짐, 정책 0개 (직접 읽기 불가)
select c.relname, c.relrowsecurity, count(pol.polname) as policies
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
  left join pg_policy pol on pol.polrelid = c.oid
 where n.nspname = 'public' and c.relname = 'worker_credentials'
 group by 1, 2;

--     기대: worker_credentials에 secret·token·key 이름의 열이 **없어야 한다** (서명 키 미저장)
select count(*) as secret_like_columns from information_schema.columns
 where table_schema = 'public' and table_name = 'worker_credentials'
   and (column_name like '%secret%' or column_name like '%token%' or column_name like '%key%');

--     기대: pa_worker 역할 존재, 로그인 불가, public 스키마 테이블 권한 0건
select rolname, rolcanlogin from pg_roles where rolname = 'pa_worker';
select count(*) as pa_worker_table_grants from information_schema.role_table_grants
 where grantee = 'pa_worker' and table_schema = 'public';

--     기대: pa_worker가 실행할 수 있는 public 함수는 **정확히 12개** (create_content_job·update_app_setting 등은 없어야 한다):
--           claim_automation_job, claim_next_automation_job, complete_automation_job, fail_automation_job,
--           heartbeat_automation_job, log_execution, log_security_event, register_asset, report_worker_status,
--           revoke_worker_credential, worker_get_input_asset, worker_get_job_context
select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public' and has_function_privilege('pa_worker', p.oid, 'EXECUTE')
 order by 1;

--     기대: private 스키마에서는 storage 정책 술어 2개뿐 (worker_may_read_object, worker_may_write_object)
select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'private' and has_function_privilege('pa_worker', p.oid, 'EXECUTE')
 order by 1;

--     기대: authenticated(Lovable)·anon이 워커 RPC를 실행할 수 없다 → 모든 칸 false
select
  has_function_privilege('authenticated', 'public.claim_next_automation_job(text, text)', 'execute') as auth_claim,
  has_function_privilege('authenticated', 'public.worker_get_job_context(uuid)', 'execute')          as auth_context,
  has_function_privilege('authenticated', 'public.report_worker_status(text, text, jsonb)', 'execute') as auth_report,
  has_function_privilege('anon', 'public.claim_next_automation_job(text, text)', 'execute')          as anon_claim,
  has_function_privilege('anon', 'public.worker_get_job_context(uuid)', 'execute')                   as anon_context;

-- 22. 호스팅 JWT 역할 매핑 (TECH_DESIGN 56.5) — 이 SQL만으로는 확인할 수 없다
--     PostgREST가 JWT의 role 클레임으로 `SET ROLE`을 하려면 authenticator가 pa_worker의 멤버여야 한다.
--     기대: 1행 (authenticator → pa_worker). 0행이면 0011이 grant를 건너뛴 것이다 (그 시점에 authenticator가 없었다).
select r.rolname as member, g.rolname as granted_role
  from pg_auth_members m
  join pg_roles r on r.oid = m.member
  join pg_roles g on g.oid = m.roleid
 where g.rolname = 'pa_worker';
--     0행이면 다음을 **한 번** 실행한다: grant pa_worker to authenticator;
--     그다음 `python scripts/smoke_worker_jwt.py` 로 실제 JWT가 pa_worker로 전환되는지 확인한다 (설명은 그 파일 머리말).

-- 23. Storage 격리 (TECH_DESIGN 56.5, 0011 8절)
--     기대: pa_worker의 storage 권한은 storage.objects의 INSERT·SELECT **두 건뿐** (UPDATE·DELETE·TRUNCATE 없음, buckets 없음)
select table_schema, table_name, privilege_type from information_schema.role_table_grants
 where grantee = 'pa_worker' and table_schema = 'storage' order by 1, 2, 3;

--     기대: pa_worker 정책 정확히 2개 — pa_worker_read_inputs(SELECT), pa_worker_write_results(INSERT)
select policyname, cmd, roles from pg_policies
 where schemaname = 'storage' and tablename = 'objects' and 'pa_worker' = any (roles)
 order by policyname;

--     기대: 위 두 정책 말고 pa_worker·PUBLIC 대상의 UPDATE·DELETE·ALL 정책이 **없어야 한다** (0행)
select policyname, cmd, roles from pg_policies
 where schemaname = 'storage' and tablename = 'objects'
   and cmd in ('UPDATE', 'DELETE', 'ALL') and ('pa_worker' = any (roles) or 'public' = any (roles));

--     기대: RLS가 켜져 있다 (relrowsecurity = true)
select relname, relrowsecurity from pg_class where oid = 'storage.objects'::regclass;

-- 24. Capability 요구사항은 배포 설정이다 (측정 전에는 UNSET) — DB가 아니라 workflows/registry.json에 있다.
--     확인: python scripts/report_capability_requirements.py   (--strict 는 UNSET이 있으면 실패)

-- 25. 0013 review_asset (Asset 승인·반려)
--     기대: has_review_note = true, authenticated_can_execute = true, anon_can_execute = false
select
  exists (select 1 from information_schema.columns
           where table_schema = 'public' and table_name = 'assets' and column_name = 'review_note') as has_review_note,
  has_function_privilege('authenticated', 'public.review_asset(uuid, text, text)', 'execute') as authenticated_can_execute,
  has_function_privilege('anon', 'public.review_asset(uuid, text, text)', 'execute') as anon_can_execute;
