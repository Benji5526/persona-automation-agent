-- =============================================================================
-- 0014 Emergency Stop (전역): generation_enabled 스위치 + emergency_stop_all()
--   TECH_DESIGN 32.6, 33.10, CLAUDE.md 11. MVP 완료 목록 20번 중 "전역" 범위.
--
--   전역 정지 = 스위치 둘을 한 번에 끈다:
--     publishing_enabled  새 게시 없음 (0001에 있다, V1)
--     generation_enabled  새 Content Job을 시작하지 않고, 워커가 새 generation Job을 선점하지 않는다 (이 파일이 추가)
--   (agent_enabled는 AI Decision이 생길 때 더한다. 지금은 막을 대상이 없다.)
--
--   * 진행 중인 작업은 끊지 않고 끝낸다. 이미 선점한 Job은 complete·fail·register_asset이 그대로 동작한다.
--     끊으면 중복 게시나 고아 파일이 생긴다 (32.6).
--   * 수집·오류 기록·복구(recover_stale_jobs)는 멈추지 않는다. 데이터는 지우지 않는다.
--   * 다시 켤 때는 스위치마다 따로 켠다 (update_app_setting). "모두 다시 시작" RPC는 만들지 않는다.
--   * Fail Closed (33.12): 값이 정확히 true가 아니거나 행이 없으면 생성은 막힌다.
--   * 끄고 켤 때마다 security_events에 누가·언제를 남긴다.
--
--   Persona 정지(agent_paused)와 플랫폼 정지(platform_controls)는 만들지 않는다. 그것들이 막는 것(AI Decision,
--   게시·팬 전송)이 아직 없다 (33.10). 그 기능을 만들 때 같이 한다.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. 스위치. 기본은 켜짐 (생성은 이미 쓰고 있다)
-- -----------------------------------------------------------------------------
insert into public.app_settings (key, value, description) values
  ('generation_enabled', 'true'::jsonb,
   '긴급 생성 정지 스위치 (32.6). false면 새 Content Job을 시작하지 않고 워커가 새 generation Job을 선점하지 않는다. 진행 중인 작업은 끝낸다')
on conflict (key) do nothing;

create or replace function private.generation_enabled()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  -- Fail Closed: 행이 없거나 true가 아니면 막는다
  select coalesce((select s.value = 'true'::jsonb from public.app_settings s where s.key = 'generation_enabled'), false)
$$;

revoke all on function private.generation_enabled() from public, anon, authenticated;

-- -----------------------------------------------------------------------------
-- 2. 선점 함수: 꺼져 있으면 새 일을 시작하지 않는다 (진행 중인 일은 건드리지 않는다)
--    시그니처와 권한은 그대로다. 0004(claim_content_job)와 0011(claim_*_automation_job)에 한 줄씩 더한 것이다.
-- -----------------------------------------------------------------------------
create or replace function public.claim_content_job(p_content_job_id uuid)
returns setof public.content_jobs
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not private.generation_enabled() then
    return;                                    -- queued로 남는다. 켜지면 디스패처·WF-001이 가져간다
  end if;
  perform private.set_reason('claim');
  return query
    update public.content_jobs set status = 'generating'
     where id = p_content_job_id and status = 'queued'
    returning *;
  perform private.set_reason(null);
end;
$$;

create or replace function public.claim_automation_job(p_job_id uuid, p_worker text)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_cred   public.worker_credentials := private.session_worker();   -- 깨진 pa_worker 자격은 여기서 FORBIDDEN
  v_worker text := coalesce(v_cred.worker_id, p_worker);            -- pa_worker는 클레임이 신원이다. p_worker는 무시
  v_target text := case when v_cred.jti is not null then v_cred.target
                        else coalesce(private.worker_target(p_worker), 'local') end;
begin
  perform private.set_reason('claim');
  return query
    update public.automation_jobs j
       set status = 'processing', attempts = j.attempts + 1, claimed_by = v_worker,
           locked_at = now(), heartbeat_at = now(), started_at = coalesce(j.started_at, now())
     where j.id = p_job_id and j.status = 'pending' and j.run_after <= now()
       and (v_cred.jti is null or j.job_type = 'generation')                 -- 워커는 generation만
       and (j.job_type <> 'generation' or coalesce(j.execution_target, 'local') = v_target)
       and (j.job_type <> 'generation' or private.generation_enabled())      -- 긴급 정지: 새 generation은 선점하지 않는다
    returning j.*;
  perform private.set_reason(null);
end;
$$;

create or replace function public.claim_next_automation_job(p_job_type text, p_worker text)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_cred   public.worker_credentials := private.session_worker();
  v_worker text := coalesce(v_cred.worker_id, p_worker);
  v_target text := case when v_cred.jti is not null then v_cred.target
                        else coalesce(private.worker_target(p_worker), 'local') end;
begin
  perform private.set_reason('claim');
  return query
    update public.automation_jobs j
       set status = 'processing', attempts = j.attempts + 1, claimed_by = v_worker,
           locked_at = now(), heartbeat_at = now(), started_at = coalesce(j.started_at, now())
     where j.id = (
             select c.id from public.automation_jobs c
              where c.status = 'pending' and c.job_type = p_job_type and c.run_after <= now()
                and (v_cred.jti is null or p_job_type = 'generation')
                and (p_job_type <> 'generation' or coalesce(c.execution_target, 'local') = v_target)
                and (p_job_type <> 'generation' or private.generation_enabled())   -- 긴급 정지
              order by c.priority desc, c.created_at
              limit 1
                for update skip locked)
    returning j.*;
  perform private.set_reason(null);
end;
$$;

-- -----------------------------------------------------------------------------
-- 3. 설정 RPC: generation_enabled를 읽고 쓴다 (스위치마다 따로 다시 켠다)
--    0010의 정의에 generation_enabled만 더했다.
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
           where s.key in ('allowed_emails', 'limits', 'publishing_enabled', 'generation_enabled',
                           'retry_backoff_seconds', 'default_execution_target', 'app_mode'));
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
  elsif p_key in ('publishing_enabled', 'generation_enabled') then
    if jsonb_typeof(p_value) <> 'boolean' then
      perform private.raise_api_error('VALIDATION_FAILED', format('%s must be a boolean', p_key));
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
  elsif p_key = 'default_execution_target' then
    if jsonb_typeof(p_value) <> 'string' or (p_value #>> '{}') not in ('local', 'cloud') then
      perform private.raise_api_error('VALIDATION_FAILED',
        'default_execution_target must be "local" or "cloud"');
    end if;
  else
    perform private.raise_api_error('VALIDATION_FAILED', format('setting %s cannot be changed here', p_key));
  end if;

  if p_key = 'limits' then
    update public.app_settings set value = value || p_value where key = p_key;
  else
    -- generation_enabled 행이 지워졌어도 다시 켤 수 있게 upsert (다른 키는 이미 있다)
    insert into public.app_settings (key, value) values (p_key, p_value)
    on conflict (key) do update set value = excluded.value, updated_at = now();
  end if;

  -- 기본값을 바꿔도 이미 만들어진 Job은 옮기지 않는다 (D3). 이동은 move_pending_jobs_to_target뿐이다.
  insert into public.security_events (event_type, actor_type, actor_id, detail)
  values (case when p_key = 'publishing_enabled'
               then case when p_value = 'true'::jsonb then 'PUBLISHING_ENABLED' else 'PUBLISHING_DISABLED' end
               when p_key = 'generation_enabled'
               then case when p_value = 'true'::jsonb then 'GENERATION_ENABLED' else 'GENERATION_DISABLED' end
               when p_key = 'default_execution_target' then 'DEFAULT_EXECUTION_TARGET_CHANGED'
               else 'SETTINGS_CHANGED' end,
          'operator', v_uid,
          case when p_key = 'default_execution_target'
               then jsonb_build_object('key', p_key, 'target', p_value)
               else jsonb_build_object('key', p_key) end);

  return public.get_app_settings();
end;
$$;

-- -----------------------------------------------------------------------------
-- 4. 전역 긴급 정지 RPC (admin)
--    한 트랜잭션에서 스위치 둘을 끈다. 이미 꺼져 있어도 오류 없이 같은 결과(멱등).
--    다시 켜는 RPC는 없다: 스위치마다 update_app_setting으로 따로 켠다.
-- -----------------------------------------------------------------------------
create or replace function public.emergency_stop_all()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid    uuid := private.require_admin();
  v_before jsonb;
begin
  select jsonb_object_agg(s.key, s.value) into v_before
    from public.app_settings s
   where s.key in ('publishing_enabled', 'generation_enabled');

  insert into public.app_settings (key, value) values
    ('publishing_enabled', 'false'::jsonb),
    ('generation_enabled', 'false'::jsonb)
  on conflict (key) do update set value = 'false'::jsonb, updated_at = now();

  insert into public.security_events (event_type, actor_type, actor_id, detail)
  values ('EMERGENCY_STOP_ALL', 'operator', v_uid,
          jsonb_build_object('before', coalesce(v_before, '{}'::jsonb)));

  return jsonb_build_object('publishing_enabled', false, 'generation_enabled', false);
end;
$$;

-- Lovable(authenticated)만. 관리자 확인은 함수 안에서 한다. 워커·n8n·anon은 부르지 못한다.
revoke all on function public.emergency_stop_all() from public, anon;
grant execute on function public.emergency_stop_all() to authenticated;
