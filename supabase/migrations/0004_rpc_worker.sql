-- =============================================================================
-- 0004 Worker RPC (TECH_DESIGN 12.5, MVP)
--   n8n·Python(service_role)만 호출한다. 실행 권한은 0005에서 부여한다.
--   결과 보고 함수는 모두 (p_job_id, p_locked_at)으로 잠금을 확인하고,
--   잠금이 맞지 않으면 false/빈 결과를 돌려준다 (회수·취소된 Job의 늦은 결과 차단, 11.6).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 도우미
-- -----------------------------------------------------------------------------

-- n번째 실패 후 대기 시간 (14.11): 30초 → 2분 → 5분 → 15분
create or replace function private.backoff_seconds(p_attempts integer)
returns integer
language sql
stable
security definer
set search_path = ''
as $$
  with b as (
    select coalesce((select s.value from public.app_settings s where s.key = 'retry_backoff_seconds'),
                    '[30, 120, 300, 900]'::jsonb) as v
  )
  select (b.v ->> (least(greatest(p_attempts, 1), jsonb_array_length(b.v)) - 1))::integer from b
$$;

-- 처리 중이고 잠금이 맞는 Job을 잠그고 가져온다. 아니면 null.
create or replace function private.lock_processing_job(p_job_id uuid, p_locked_at timestamptz, p_job_type text default null)
returns public.automation_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.automation_jobs;
begin
  select * into v from public.automation_jobs j
   where j.id = p_job_id and j.status = 'processing' and j.locked_at = p_locked_at
     and (p_job_type is null or j.job_type = p_job_type)
     for update;
  return v;
end;
$$;

-- 로그에서 비밀값 가리기 (15.21)
create or replace function private.redact_text(p_text text)
returns text
language sql
immutable
set search_path = ''
as $$
  select regexp_replace(
           regexp_replace(
             regexp_replace(p_text,
               'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED]', 'g'),
             '(sb_secret_|sb_publishable_)[A-Za-z0-9_-]+', '[REDACTED]', 'g'),
           '(Bearer)\s+[A-Za-z0-9._~+/=-]+', '\1 [REDACTED]', 'gi')
$$;

create or replace function private.redact_jsonb(p_data jsonb)
returns jsonb
language sql
immutable
set search_path = ''
as $$
  select case when p_data is null then null else
    private.redact_text(
      regexp_replace(p_data::text,
        '"(authorization|x-bridge-token|x-callback-token|x-webhook-secret|apikey|api_key|access_token|refresh_token|password|secret|token|cf-access-client-secret)"\s*:\s*"[^"]*"',
        '"\1": "[REDACTED]"', 'gi')
    )::jsonb end
$$;

-- -----------------------------------------------------------------------------
-- claim_content_job: queued → generating (n8n WF-001)
-- -----------------------------------------------------------------------------
create or replace function public.claim_content_job(p_content_job_id uuid)
returns setof public.content_jobs
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform private.set_reason('claim');
  return query
    update public.content_jobs set status = 'generating'
     where id = p_content_job_id and status = 'queued'
    returning *;
  perform private.set_reason(null);
end;
$$;

-- -----------------------------------------------------------------------------
-- create_automation_job: 멱등 생성 (같은 idempotency_key 또는 같은 활성 단계가 있으면 기존 행 반환)
-- -----------------------------------------------------------------------------
create or replace function public.create_automation_job(
  p_job_type        text,
  p_persona_id      uuid,
  p_content_job_id  uuid    default null,
  p_post_id         uuid    default null,
  p_worker          text    default null,
  p_payload         jsonb   default '{}'::jsonb,
  p_priority        integer default null,
  p_max_attempts    integer default 3,
  p_idempotency_key text    default null
)
returns public.automation_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v        public.automation_jobs;
  v_cj     public.content_jobs;
  v_limit  integer;
  v_today  integer;
begin
  if p_content_job_id is not null then
    -- 취소(cancel_content_job, FOR UPDATE)와 동시에 실행돼도 취소된 Job 아래 새 Job이 생기지 않게 잠근다
    select * into v_cj from public.content_jobs where id = p_content_job_id for share;
    if v_cj.id is null or v_cj.persona_id <> p_persona_id then
      perform private.raise_api_error('NOT_FOUND', 'content job not found for persona');
    end if;
    -- 단계별로 상위 Content Job이 맞는 상태인지 확인
    if p_job_type in ('prompt', 'generation') and v_cj.status <> 'generating' then
      perform private.raise_api_error('INVALID_TRANSITION',
        format('content job is %s (expected generating)', v_cj.status));
    end if;
    if p_job_type = 'caption' and v_cj.status not in ('ready', 'published') then
      perform private.raise_api_error('INVALID_TRANSITION',
        format('content job is %s (expected ready)', v_cj.status));
    end if;
  end if;

  -- 실행 한도 (15.18): 하루 생성 이미지 수
  if p_job_type = 'generation' then
    perform pg_advisory_xact_lock(hashtextextended('daily_generation_limit', 0));
    v_limit := private.setting_int('limits', 'daily_generation_limit', 300);
    select count(*) into v_today from public.assets a where a.created_at >= date_trunc('day', now());
    if v_today >= v_limit then
      perform private.raise_api_error('RATE_LIMITED', format('daily_generation_limit (%s) reached', v_limit));
    end if;
  end if;

  perform private.set_reason('create');
  insert into public.automation_jobs
    (persona_id, content_job_id, post_id, job_type, worker, payload, priority, max_attempts, idempotency_key)
  values
    (p_persona_id, p_content_job_id, p_post_id, p_job_type,
     coalesce(p_worker, case when p_job_type = 'generation' then 'python' else 'n8n' end),
     coalesce(p_payload, '{}'::jsonb),
     coalesce(p_priority, v_cj.priority, 5),
     p_max_attempts, p_idempotency_key)
  on conflict do nothing
  returning * into v;
  perform private.set_reason(null);

  if v.id is null and p_idempotency_key is not null then
    select * into v from public.automation_jobs where idempotency_key = p_idempotency_key;
  end if;
  if v.id is null and p_content_job_id is not null then
    select * into v from public.automation_jobs
     where content_job_id = p_content_job_id and job_type = p_job_type and status in ('pending', 'processing');
  end if;
  return v;
end;
$$;

-- -----------------------------------------------------------------------------
-- claim_automation_job / claim_next_automation_job: pending → processing (11.5)
-- -----------------------------------------------------------------------------
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
              order by priority desc, created_at
              limit 1
                for update skip locked)
    returning j.*;
  perform private.set_reason(null);
end;
$$;

-- -----------------------------------------------------------------------------
-- heartbeat_automation_job (11.6): false면 잠금을 잃음 → Worker는 즉시 중단
-- -----------------------------------------------------------------------------
create or replace function public.heartbeat_automation_job(p_job_id uuid, p_locked_at timestamptz)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.automation_jobs set heartbeat_at = now()
   where id = p_job_id and status = 'processing' and locked_at = p_locked_at;
  return found;
end;
$$;

-- -----------------------------------------------------------------------------
-- complete_automation_job: processing → done (job_type별 done 조건 검사, 11.4)
-- -----------------------------------------------------------------------------
create or replace function public.complete_automation_job(
  p_job_id    uuid,
  p_locked_at timestamptz,
  p_result    jsonb default '{}'::jsonb
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.automation_jobs := private.lock_processing_job(p_job_id, p_locked_at);
begin
  if v.id is null then
    return false;
  end if;

  if v.job_type = 'generation' and not exists (
       select 1 from public.assets a where a.automation_job_id = v.id) then
    perform private.raise_api_error('VALIDATION_FAILED', 'generation job has no registered asset');
  elsif v.job_type = 'prompt' and not exists (
       select 1 from public.content_jobs c
        where c.id = v.content_job_id and (c.prompt_parts is not null or c.prompt is not null)) then
    perform private.raise_api_error('VALIDATION_FAILED', 'prompt job has no saved prompt');
  elsif v.job_type = 'caption' and not exists (
       select 1 from public.posts p where p.id = nullif(v.result ->> 'post_id', '')::uuid) then
    perform private.raise_api_error('VALIDATION_FAILED', 'caption job has no post draft');
  elsif v.job_type in ('publish', 'analytics') then
    perform private.raise_api_error('VALIDATION_FAILED',
      format('%s jobs are completed by their dedicated RPC (V1)', v.job_type));
  end if;

  perform private.set_reason('complete');
  update public.automation_jobs
     set status = 'done', result = result || coalesce(private.redact_jsonb(p_result), '{}'::jsonb),
         error_type = null, error_code = null, error_message = null
   where id = v.id;
  perform private.set_reason(null);
  return true;
end;
$$;

-- -----------------------------------------------------------------------------
-- fail_automation_job: 재시도 결정 (14.11)
-- -----------------------------------------------------------------------------
create or replace function public.fail_automation_job(
  p_job_id              uuid,
  p_locked_at           timestamptz,
  p_error_type          text,
  p_error_code          text,
  p_message             text,
  p_retryable           boolean,
  p_retry_after_seconds integer default null,
  p_step                text    default null
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v         public.automation_jobs := private.lock_processing_job(p_job_id, p_locked_at);
  v_message text := left(private.redact_text(p_message), 4000);
  v_delay   integer;
begin
  if v.id is null then
    return jsonb_build_object('applied', false, 'status', null, 'run_after', null);
  end if;

  insert into public.system_errors
    (automation_job_id, persona_id, service, step, error_type, error_code, message, retryable)
  values
    (v.id, v.persona_id, v.worker, p_step, p_error_type, p_error_code, v_message, p_retryable);

  if p_retryable and v.attempts < v.max_attempts then
    v_delay := coalesce(p_retry_after_seconds, private.backoff_seconds(v.attempts));
    perform private.set_reason('retry_backoff');
    update public.automation_jobs
       set status = 'pending', run_after = now() + make_interval(secs => v_delay),
           error_type = p_error_type, error_code = p_error_code,
           error_message = format('[시도 %s/%s] %s', v.attempts, v.max_attempts, v_message)
     where id = v.id returning * into v;
  else
    perform private.set_reason('failed:' || coalesce(p_error_code, p_error_type));
    update public.automation_jobs
       set status = 'failed', error_type = p_error_type, error_code = p_error_code, error_message = v_message
     where id = v.id returning * into v;
  end if;
  perform private.set_reason(null);

  return jsonb_build_object('applied', true, 'status', v.status,
                            'run_after', case when v.status = 'pending' then v.run_after end);
end;
$$;

-- -----------------------------------------------------------------------------
-- log_execution (10.16)
-- -----------------------------------------------------------------------------
create or replace function public.log_execution(
  p_job_id        uuid,
  p_step          text,
  p_service       text,
  p_status        text,
  p_input         jsonb   default null,
  p_output        jsonb   default null,
  p_duration_ms   bigint  default null,
  p_execution_ref text    default null,
  p_error         text    default null
)
returns void
language sql
security definer
set search_path = ''
as $$
  insert into public.execution_logs
    (automation_job_id, step, service, status, input_data, output_data, duration_ms, execution_ref, error)
  values
    (p_job_id, p_step, p_service, p_status, private.redact_jsonb(p_input), private.redact_jsonb(p_output),
     p_duration_ms, p_execution_ref, left(private.redact_text(p_error), 4000));
$$;

-- -----------------------------------------------------------------------------
-- save_prompt_parts (WF-002, 13.7)
-- -----------------------------------------------------------------------------
create or replace function public.save_prompt_parts(
  p_job_id             uuid,
  p_locked_at          timestamptz,
  p_prompt_parts       jsonb,
  p_negative_additions text[] default '{}'
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v     public.automation_jobs := private.lock_processing_job(p_job_id, p_locked_at, 'prompt');
  v_key text;
begin
  if v.id is null then
    return false;
  end if;
  if jsonb_typeof(p_prompt_parts) <> 'object' then
    perform private.raise_api_error('VALIDATION_FAILED', 'prompt_parts must be an object');
  end if;
  foreach v_key in array array['subject', 'location', 'action', 'style'] loop
    if coalesce(btrim(p_prompt_parts ->> v_key), '') = '' then
      perform private.raise_api_error('VALIDATION_FAILED', format('prompt_parts.%s is required', v_key));
    end if;
  end loop;

  update public.content_jobs
     set prompt_parts = p_prompt_parts,
         metadata = metadata || jsonb_build_object('negative_additions', to_jsonb(coalesce(p_negative_additions, '{}')))
   where id = v.content_job_id;
  return true;
end;
$$;

-- -----------------------------------------------------------------------------
-- register_asset (Python, 12.5): 실행 후 검증을 통과한 파일만 등록
-- -----------------------------------------------------------------------------
create or replace function public.register_asset(p_job_id uuid, p_locked_at timestamptz, p_asset jsonb)
returns setof public.assets
language plpgsql
security definer
set search_path = ''
as $$
declare
  v      public.automation_jobs := private.lock_processing_job(p_job_id, p_locked_at, 'generation');
  v_id   uuid;
  v_path text := p_asset ->> 'storage_path';
  a      public.assets;
begin
  if v.id is null then
    return;  -- 잠금을 잃음 → 빈 결과
  end if;
  v_id := coalesce(nullif(p_asset ->> 'id', '')::uuid, gen_random_uuid());
  if v_path is null or v_path not like format('persona/%s/assets/%%', v.persona_id) then
    perform private.raise_api_error('VALIDATION_FAILED',
      format('storage_path must start with persona/%s/assets/', v.persona_id));
  end if;

  perform private.set_reason('register');
  insert into public.assets
    (id, persona_id, content_job_id, automation_job_id, asset_type, file_name, storage_bucket, storage_path,
     public_url, thumbnail_url, mime_type, width, height, duration, prompt, workflow, generation_metadata)
  values
    (v_id, v.persona_id, v.content_job_id, v.id,
     p_asset ->> 'asset_type', p_asset ->> 'file_name', coalesce(p_asset ->> 'storage_bucket', 'media'), v_path,
     p_asset ->> 'public_url', p_asset ->> 'thumbnail_url', p_asset ->> 'mime_type',
     (p_asset ->> 'width')::integer, (p_asset ->> 'height')::integer, (p_asset ->> 'duration')::numeric,
     p_asset ->> 'prompt', p_asset -> 'workflow', coalesce(p_asset -> 'generation_metadata', '{}'::jsonb))
  on conflict (id) do nothing
  returning * into a;
  perform private.set_reason(null);

  if a.id is null then
    select * into a from public.assets where id = v_id and automation_job_id = v.id;
  end if;
  if a.id is not null then
    return next a;
  end if;
exception
  when check_violation or not_null_violation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- create_post_draft (WF-005): Caption 초안
-- -----------------------------------------------------------------------------
create or replace function public.create_post_draft(
  p_job_id    uuid,
  p_locked_at timestamptz,
  p_asset_id  uuid,
  p_platform  text,
  p_caption   text,
  p_hashtags  text[] default '{}'
)
returns setof public.posts
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.automation_jobs := private.lock_processing_job(p_job_id, p_locked_at, 'caption');
  p public.posts;
begin
  if v.id is null then
    return;  -- 잠금을 잃음 → 빈 결과
  end if;
  if not exists (select 1 from public.assets a
                 where a.id = p_asset_id and a.content_job_id = v.content_job_id and a.status <> 'archived') then
    perform private.raise_api_error('VALIDATION_FAILED', 'asset does not belong to this content job');
  end if;

  perform private.set_reason('caption_draft');
  insert into public.posts (persona_id, asset_id, platform, caption, hashtags)
  values (v.persona_id, p_asset_id, p_platform, p_caption, coalesce(p_hashtags, '{}'))
  returning * into p;
  perform private.set_reason(null);

  update public.automation_jobs set result = result || jsonb_build_object('post_id', p.id) where id = v.id;
  return next p;
exception
  when check_violation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- sync_workflow_registry (Python 시작 시, 12.5)
--   p_workflows: {"workflow_id": {"version", "type", "stage", "enabled", "params", "inputs"}, ...}
--   목록에 없는 기존 Workflow는 enabled = false
-- -----------------------------------------------------------------------------
create or replace function public.sync_workflow_registry(p_workflows jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_count integer;
begin
  if jsonb_typeof(p_workflows) <> 'object' then
    perform private.raise_api_error('VALIDATION_FAILED', 'p_workflows must be an object');
  end if;

  insert into public.comfy_workflows (id, version, type, stage, enabled, params, inputs, synced_at)
  select w.key, w.value ->> 'version', w.value ->> 'type', coalesce(w.value ->> 'stage', 'mvp'),
         coalesce((w.value ->> 'enabled')::boolean, true),
         coalesce(w.value -> 'params', '{}'::jsonb), coalesce(w.value -> 'inputs', '{}'::jsonb), now()
    from jsonb_each(p_workflows) w
  on conflict (id) do update
     set version = excluded.version, type = excluded.type, stage = excluded.stage,
         enabled = excluded.enabled, params = excluded.params, inputs = excluded.inputs,
         synced_at = excluded.synced_at;
  get diagnostics v_count = row_count;

  update public.comfy_workflows set enabled = false, synced_at = now()
   where not p_workflows ? id and enabled;
  return v_count;
exception
  when check_violation or not_null_violation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- log_security_event (15.22): 브릿지·n8n이 기록
-- -----------------------------------------------------------------------------
create or replace function public.log_security_event(
  p_event_type text,
  p_actor_type text,
  p_source_ip  text  default null,
  p_detail     jsonb default '{}'::jsonb,
  p_persona_id uuid  default null
)
returns void
language sql
security definer
set search_path = ''
as $$
  insert into public.security_events (event_type, actor_type, source_ip, detail, persona_id)
  values (p_event_type, p_actor_type, nullif(p_source_ip, '')::inet,
          coalesce(private.redact_jsonb(p_detail), '{}'::jsonb), p_persona_id);
$$;

-- -----------------------------------------------------------------------------
-- recover_stale_jobs (11.6): pg_cron이 1분마다 실행
-- -----------------------------------------------------------------------------
create or replace function public.recover_stale_jobs()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_timeouts jsonb := coalesce((select s.value from public.app_settings s where s.key = 'heartbeat_timeout_seconds'),
                               '{}'::jsonb);
  v_prev     jsonb;
  v_count    integer := 0;
  j          public.automation_jobs;
begin
  v_prev := private.begin_system_change('heartbeat_timeout');
  for j in
    select * from public.automation_jobs
     where status = 'processing'
       and coalesce(heartbeat_at, locked_at)
           < now() - make_interval(secs => coalesce((v_timeouts ->> job_type)::integer, 300))
     for update skip locked
  loop
    insert into public.system_errors
      (automation_job_id, persona_id, service, step, error_type, error_code, message, retryable)
    values
      (j.id, j.persona_id, j.worker, 'heartbeat', 'timeout', 'HEARTBEAT_TIMEOUT',
       format('no heartbeat since %s (claimed_by %s)', coalesce(j.heartbeat_at, j.locked_at), j.claimed_by),
       j.attempts < j.max_attempts);

    if j.attempts < j.max_attempts then
      update public.automation_jobs
         set status = 'pending',
             run_after = now() + make_interval(secs => private.backoff_seconds(j.attempts)),
             error_type = 'timeout', error_code = 'HEARTBEAT_TIMEOUT',
             error_message = format('[시도 %s/%s] heartbeat timeout', j.attempts, j.max_attempts)
       where id = j.id;
    else
      update public.automation_jobs
         set status = 'failed', error_type = 'timeout', error_code = 'HEARTBEAT_TIMEOUT',
             error_message = 'heartbeat timeout (attempts exhausted)'
       where id = j.id;
    end if;
    v_count := v_count + 1;
  end loop;
  perform private.end_system_change(v_prev);
  return v_count;
end;
$$;
