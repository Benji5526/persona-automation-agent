-- =============================================================================
-- 0010 execution_targets (TECH_DESIGN 56.2, 56.4): Personal Edition의 Local ↔ Cloud GPU 선택
--
-- 추가만 한다. 기본값(active_worker = null)에서는 지금 동작과 같다.
--   1. app_settings.app_mode      프로필 표시 ('personal', 읽기 전용)
--   2. app_settings.active_worker 활성 워커 id 또는 null(제한 없음). admin이 update_app_setting으로 바꾼다
--   3. worker_status.target·provider  워커가 상태 보고에 함께 보낸다 ('local'|'cloud', 'local'|'runpod' …)
--   4. claim_automation_job·claim_next_automation_job: generation Job은 활성 워커만 선점한다 (DB가 강제)
-- 이 파일은 SaaS 테이블(조직·멤버·구독 등)을 만들지 않는다.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1·2. 설정
-- -----------------------------------------------------------------------------
insert into public.app_settings (key, value, description) values
  ('app_mode', '"personal"'::jsonb,
   'Architecture profile (TECH_DESIGN 56.1). 읽기 전용 표시값: personal | saas'),
  ('active_worker', 'null'::jsonb,
   '활성 워커 id. generation Job은 이 워커만 선점한다. null이면 제한 없음 (TECH_DESIGN 56.2)')
on conflict (key) do nothing;

-- -----------------------------------------------------------------------------
-- 3. worker_status: 실행 Target과 공급자 (GPU 모델·VRAM은 이미 gpu jsonb로 보고된다)
-- -----------------------------------------------------------------------------
alter table public.worker_status
  add column if not exists target   text check (target in ('local', 'cloud')),
  add column if not exists provider text check (provider ~ '^[a-z0-9_-]{1,32}$');

create or replace function public.report_worker_status(p_worker_id text, p_kind text, p_info jsonb default '{}'::jsonb)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_models jsonb := p_info -> 'models';
begin
  if v_models is not null and (
       jsonb_typeof(v_models) <> 'object'
       or jsonb_typeof(coalesce(v_models -> 'checkpoints', '[]'::jsonb)) <> 'array'
       or jsonb_typeof(coalesce(v_models -> 'loras', '[]'::jsonb)) <> 'array') then
    perform private.raise_api_error('VALIDATION_FAILED',
      'models must be {"checkpoints": [...], "loras": [...]}');
  end if;

  insert into public.worker_status as w
    (id, kind, comfyui_ok, gpu, current_job_id, queue_size, version, models, target, provider, last_seen_at)
  values (p_worker_id, p_kind,
          (p_info ->> 'comfyui_ok')::boolean,
          coalesce(p_info -> 'gpu', '{}'::jsonb),
          nullif(p_info ->> 'current_job_id', '')::uuid,
          coalesce((p_info ->> 'queue_size')::integer, 0),
          p_info ->> 'version',
          coalesce(v_models, '{}'::jsonb),
          nullif(p_info ->> 'target', ''),
          nullif(p_info ->> 'provider', ''),
          now())
  on conflict (id) do update
     set kind = excluded.kind, comfyui_ok = excluded.comfyui_ok, gpu = excluded.gpu,
         current_job_id = excluded.current_job_id, queue_size = excluded.queue_size,
         version = excluded.version,
         models = coalesce(v_models, w.models),
         target = coalesce(excluded.target, w.target),        -- 보고에 없으면 이전 값 유지
         provider = coalesce(excluded.provider, w.provider),
         last_seen_at = excluded.last_seen_at;
exception
  when check_violation or invalid_text_representation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- 4. 선점 gating: generation Job은 활성 워커만 가져간다
--    active_worker가 null이거나 문자열이 아니면 제한 없음. 다른 job_type(prompt·caption·publish …)은 영향 없다.
-- -----------------------------------------------------------------------------
create or replace function private.worker_may_claim(p_job_type text, p_worker text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select case
    when p_job_type <> 'generation' then true
    else coalesce(
      (select case when jsonb_typeof(s.value) = 'string' then coalesce((s.value #>> '{}') = p_worker, false) else true end
         from public.app_settings s where s.key = 'active_worker'),
      true)
  end
$$;

revoke all on function private.worker_may_claim(text, text) from public, anon, authenticated;

create or replace function public.claim_automation_job(p_job_id uuid, p_worker text)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform private.set_reason('claim');
  return query
    update public.automation_jobs
       set status = 'processing', attempts = attempts + 1, claimed_by = p_worker,
           locked_at = now(), heartbeat_at = now(), started_at = coalesce(started_at, now())
     where id = p_job_id and status = 'pending' and run_after <= now()
       and private.worker_may_claim(job_type, p_worker)
    returning *;
  perform private.set_reason(null);
end;
$$;

create or replace function public.claim_next_automation_job(p_job_type text, p_worker text)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform private.set_reason('claim');
  return query
    update public.automation_jobs j
       set status = 'processing', attempts = j.attempts + 1, claimed_by = p_worker,
           locked_at = now(), heartbeat_at = now(), started_at = coalesce(j.started_at, now())
     where j.id = (
             select id from public.automation_jobs
              where status = 'pending' and job_type = p_job_type and run_after <= now()
                and private.worker_may_claim(p_job_type, p_worker)
              order by priority desc, created_at
              limit 1
                for update skip locked)
    returning j.*;
  perform private.set_reason(null);
end;
$$;

-- -----------------------------------------------------------------------------
-- 설정 RPC: active_worker 변경(admin), app_mode는 읽기 전용
-- -----------------------------------------------------------------------------
create or replace function public.get_app_settings()
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  perform private.require_admin();
  return (select jsonb_object_agg(s.key, s.value)
            from public.app_settings s
           where s.key in ('allowed_emails', 'limits', 'publishing_enabled', 'retry_backoff_seconds',
                           'active_worker', 'app_mode'));
end;
$$;

create or replace function public.update_app_setting(p_key text, p_value jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid  uuid := private.require_admin();
  v_item jsonb;
begin
  -- 키별 형식 검증
  if p_key = 'allowed_emails' then
    if jsonb_typeof(p_value) <> 'array' then
      perform private.raise_api_error('VALIDATION_FAILED', 'allowed_emails must be an array');
    end if;
    for v_item in select * from jsonb_array_elements(p_value) loop
      if jsonb_typeof(v_item) <> 'string' or (v_item #>> '{}') !~ '^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$' then
        perform private.raise_api_error('VALIDATION_FAILED', format('invalid lowercase email: %s', v_item));
      end if;
    end loop;
  elsif p_key = 'publishing_enabled' then
    if jsonb_typeof(p_value) <> 'boolean' then
      perform private.raise_api_error('VALIDATION_FAILED', 'publishing_enabled must be a boolean');
    end if;
  elsif p_key = 'limits' then
    if jsonb_typeof(p_value) <> 'object' or exists (
         select 1 from jsonb_each(p_value) e
          where e.key not in ('max_content_jobs_per_hour', 'daily_generation_limit',
                              'daily_llm_calls_limit', 'daily_publish_limit')
             or jsonb_typeof(e.value) <> 'number' or (e.value #>> '{}')::numeric < 0) then
      perform private.raise_api_error('VALIDATION_FAILED', 'limits must be an object of known non-negative numbers');
    end if;
  elsif p_key = 'retry_backoff_seconds' then
    if jsonb_typeof(p_value) <> 'array' or jsonb_array_length(p_value) = 0 or exists (
         select 1 from jsonb_array_elements(p_value) e
          where jsonb_typeof(e) <> 'number' or (e #>> '{}')::numeric < 1) then
      perform private.raise_api_error('VALIDATION_FAILED', 'retry_backoff_seconds must be a non-empty array of positive numbers');
    end if;
  elsif p_key = 'active_worker' then
    p_value := coalesce(p_value, 'null'::jsonb);   -- PostgREST는 JSON null을 SQL NULL로 넘긴다
    if jsonb_typeof(p_value) <> 'null' and not (
         jsonb_typeof(p_value) = 'string'
         and exists (select 1 from public.worker_status w where w.id = (p_value #>> '{}') and w.kind = 'python')) then
      perform private.raise_api_error('VALIDATION_FAILED',
        'active_worker must be null or the id of a registered python worker');
    end if;
  else
    perform private.raise_api_error('VALIDATION_FAILED', format('setting %s cannot be changed here', p_key));
  end if;

  if p_key = 'limits' then
    update public.app_settings set value = value || p_value where key = p_key;
  else
    update public.app_settings set value = p_value where key = p_key;
  end if;

  insert into public.security_events (event_type, actor_type, actor_id, detail)
  values (case when p_key = 'publishing_enabled'
               then case when p_value = 'true'::jsonb then 'PUBLISHING_ENABLED' else 'PUBLISHING_DISABLED' end
               when p_key = 'active_worker' then 'ACTIVE_WORKER_CHANGED'
               else 'SETTINGS_CHANGED' end,
          'operator', v_uid,
          case when p_key = 'active_worker'
               then jsonb_build_object('key', p_key, 'worker', p_value)
               else jsonb_build_object('key', p_key) end);

  return public.get_app_settings();
end;
$$;
-- create or replace는 기존 EXECUTE 권한(authenticated: get_app_settings·update_app_setting,
-- service_role: report_worker_status·claim_*)을 유지한다.
