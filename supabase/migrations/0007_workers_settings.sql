-- =============================================================================
-- 0007 Worker 상태, Settings RPC, 오류 해결 표시 (TECH_DESIGN 17.4, 17.5, 18.9, 18.19)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- worker_status (17.4): 브릿지·n8n이 30초~1분마다 보고
-- -----------------------------------------------------------------------------
create table public.worker_status (
  id             text primary key check (id ~ '^[a-z0-9:_.-]{1,64}$'),
  kind           text not null check (kind in ('python', 'n8n')),
  comfyui_ok     boolean,
  gpu            jsonb not null default '{}'::jsonb,
  current_job_id uuid,
  queue_size     integer not null default 0 check (queue_size >= 0),
  version        text,
  last_seen_at   timestamptz not null default now()
);

alter table public.worker_status enable row level security;
grant select on public.worker_status to authenticated;
grant all on public.worker_status to service_role;
create policy worker_status_select on public.worker_status
  for select to authenticated using (true);

alter publication supabase_realtime add table public.worker_status;

create or replace function public.report_worker_status(p_worker_id text, p_kind text, p_info jsonb default '{}'::jsonb)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.worker_status (id, kind, comfyui_ok, gpu, current_job_id, queue_size, version, last_seen_at)
  values (p_worker_id, p_kind,
          (p_info ->> 'comfyui_ok')::boolean,
          coalesce(p_info -> 'gpu', '{}'::jsonb),
          nullif(p_info ->> 'current_job_id', '')::uuid,
          coalesce((p_info ->> 'queue_size')::integer, 0),
          p_info ->> 'version',
          now())
  on conflict (id) do update
     set kind = excluded.kind, comfyui_ok = excluded.comfyui_ok, gpu = excluded.gpu,
         current_job_id = excluded.current_job_id, queue_size = excluded.queue_size,
         version = excluded.version, last_seen_at = excluded.last_seen_at;
exception
  when check_violation or invalid_text_representation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- get_dashboard_summary: workers 추가 (기존 정의를 교체)
-- -----------------------------------------------------------------------------
create or replace function public.get_dashboard_summary(p_persona_id uuid default null)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_uid      uuid := private.require_operator();
  v_personas uuid[];
begin
  if p_persona_id is not null then
    perform private.require_owned_persona(p_persona_id);
    v_personas := array[p_persona_id];
  else
    select coalesce(array_agg(p.id), '{}') into v_personas from public.personas p where p.user_id = v_uid;
  end if;

  return jsonb_build_object(
    'personas', cardinality(v_personas),
    'content_jobs', coalesce((
      select jsonb_object_agg(s.status, s.n) from (
        select c.status, count(*) as n from public.content_jobs c
         where c.persona_id = any (v_personas) group by c.status) s), '{}'::jsonb),
    'automation_jobs', (
      select jsonb_build_object(
        'active',  count(*) filter (where j.status = 'processing'),
        'pending', count(*) filter (where j.status = 'pending' and j.run_after <= now()),
        'retry',   count(*) filter (where j.status = 'pending' and j.run_after > now() and j.attempts > 0),
        'failed',  count(*) filter (where j.status = 'failed'))
        from public.automation_jobs j where j.persona_id = any (v_personas)),
    'recent_failures', coalesce((
      select jsonb_agg(f order by f.created_at desc) from (
        select e.id, e.automation_job_id, e.error_type, e.error_code, left(e.message, 300) as message, e.created_at
          from public.system_errors e
         where e.persona_id = any (v_personas) and not e.resolved
         order by e.created_at desc limit 5) f), '[]'::jsonb),
    'workers', coalesce((
      select jsonb_agg(jsonb_build_object(
               'id', w.id, 'kind', w.kind, 'comfyui_ok', w.comfyui_ok,
               'online', w.last_seen_at > now() - interval '90 seconds',
               'current_job_id', w.current_job_id, 'queue_size', w.queue_size,
               'last_seen_at', w.last_seen_at) order by w.id)
        from public.worker_status w), '[]'::jsonb),
    'publishing_enabled', coalesce((select s.value = 'true'::jsonb from public.app_settings s
                                    where s.key = 'publishing_enabled'), false)
  );
end;
$$;

-- -----------------------------------------------------------------------------
-- Settings RPC (17.5): admin만
-- -----------------------------------------------------------------------------
create or replace function private.require_admin()
returns uuid
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_uid uuid := private.require_operator();
begin
  if not exists (select 1 from public.users u where u.id = v_uid and u.role = 'admin') then
    perform private.raise_api_error('FORBIDDEN', 'admin role required');
  end if;
  return v_uid;
end;
$$;

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
           where s.key in ('allowed_emails', 'limits', 'publishing_enabled', 'retry_backoff_seconds'));
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
               else 'SETTINGS_CHANGED' end,
          'operator', v_uid, jsonb_build_object('key', p_key));

  return public.get_app_settings();
end;
$$;

-- -----------------------------------------------------------------------------
-- resolve_system_error (18.9)
-- -----------------------------------------------------------------------------
create or replace function public.resolve_system_error(p_error_id bigint)
returns public.system_errors
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.system_errors;
begin
  update public.system_errors e set resolved = true
   where e.id = p_error_id
     and e.persona_id in (select p.id from public.personas p where p.user_id = private.require_operator())
  returning * into v;
  if v.id is null then
    perform private.raise_api_error('NOT_FOUND', 'error not found');
  end if;
  return v;
end;
$$;

-- -----------------------------------------------------------------------------
-- 권한
-- -----------------------------------------------------------------------------
revoke execute on function
  public.report_worker_status(text, text, jsonb),
  public.get_app_settings(),
  public.update_app_setting(text, jsonb),
  public.resolve_system_error(bigint)
from public, anon, authenticated;

grant execute on function public.report_worker_status(text, text, jsonb) to service_role;
grant execute on function
  public.get_app_settings(),
  public.update_app_setting(text, jsonb),
  public.resolve_system_error(bigint)
to authenticated;
-- get_dashboard_summary는 create or replace라 기존 권한(authenticated)이 유지된다
