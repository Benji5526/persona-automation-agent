-- =============================================================================
-- 실제 Supabase 적용 후 점검 (TECH_DESIGN 24.4)
--   읽기 전용이다. 아무것도 바꾸지 않는다. SQL Editor에서 블록마다 실행하고
--   각 블록의 "기대" 주석과 결과를 비교한다. 다르면 24.4 표의 조치를 따른다.
-- =============================================================================

-- 1. 마이그레이션 적용 버전
--    기대: 0001 ~ 0008 여덟 줄 (파일 이름 앞부분이 version)
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
--    기대: Operator RPC만 — archive_asset, cancel_content_job, create_content_job,
--          get_app_settings, get_dashboard_summary, regenerate_content_job, resolve_system_error,
--          retry_automation_job, retry_content_job, submit_content_job, update_app_setting
--    claim_*, complete_*, fail_*, register_asset, report_worker_status 등이 보이면 안 된다.
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
--     기대: pg_cron, pg_net 모두 있음. recover-stale-jobs가 '* * * * *'로 active
select extname, extversion from pg_extension where extname in ('pg_cron', 'pg_net');
select jobname, schedule, active from cron.job where jobname = 'recover-stale-jobs';

-- 14. pg_cron 최근 실행 결과 (적용 후 몇 분 지나서)
--     기대: status = 'succeeded'
select j.jobname, d.status, d.return_message, d.start_time
  from cron.job_run_details d
  join cron.job j on j.jobid = d.jobid
 where j.jobname = 'recover-stale-jobs'
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
--     기대: python:rtx5080-1과 n8n, last_seen_at이 90초 이내. python 행의 models에 설치된 체크포인트·LoRA 수
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
