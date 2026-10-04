-- =============================================================================
-- 0003 Operator RPC (TECH_DESIGN 12.4, MVP)
--   Lovable이 상태를 바꾸는 유일한 방법이다. 모두 security definer이고,
--   함수 안에서 auth.uid()로 소유권을 확인한다. 전환 규칙은 0002 트리거가 다시 검사한다.
--   실행 권한(authenticated만)은 0005에서 부여한다.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 도우미
-- -----------------------------------------------------------------------------
create or replace function private.require_operator()
returns uuid
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_uid uuid := auth.uid();
begin
  if v_uid is null or not exists (select 1 from public.users u where u.id = v_uid) then
    perform private.raise_api_error('FORBIDDEN', 'operator session required');
  end if;
  return v_uid;
end;
$$;

create or replace function private.require_owned_persona(p_persona_id uuid)
returns void
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  if not exists (select 1 from public.personas p
                 where p.id = p_persona_id and p.user_id = private.require_operator()) then
    perform private.raise_api_error('NOT_FOUND', 'persona not found');
  end if;
end;
$$;

create or replace function private.set_reason(p_reason text)
returns void
language sql
set search_path = ''
as $$
  select set_config('app.transition_reason', coalesce(p_reason, ''), true);
$$;

create or replace function private.setting_int(p_key text, p_field text, p_default integer)
returns integer
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce((select (s.value ->> p_field)::integer from public.app_settings s where s.key = p_key), p_default);
$$;

-- 실행 한도 (15.18): Persona별 시간당 Content Job 수
create or replace function private.check_content_job_rate_limit(p_persona_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_limit integer := private.setting_int('limits', 'max_content_jobs_per_hour', 30);
  v_count integer;
begin
  perform pg_advisory_xact_lock(hashtextextended('content_job_rate:' || p_persona_id::text, 0));
  select count(*) into v_count
    from public.content_jobs c
   where c.persona_id = p_persona_id and c.created_at > now() - interval '1 hour';
  if v_count >= v_limit then
    perform private.raise_api_error('RATE_LIMITED',
      format('max_content_jobs_per_hour (%s) exceeded for persona %s', v_limit, p_persona_id));
  end if;
end;
$$;

-- input_images 검증 (13.6): {"자리": {"asset_id": uuid} | {"persona_asset_id": uuid}}, 같은 Persona 소속
create or replace function private.validate_input_images(p_persona_id uuid, p_input_images jsonb)
returns void
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_key text;
  v_ref jsonb;
  v_id  uuid;
begin
  if p_input_images is null or p_input_images = '{}'::jsonb then
    return;
  end if;
  if jsonb_typeof(p_input_images) <> 'object' then
    perform private.raise_api_error('VALIDATION_FAILED', 'input_images must be an object');
  end if;
  for v_key, v_ref in select * from jsonb_each(p_input_images) loop
    if v_key !~ '^[a-z0-9_]{1,40}$' or jsonb_typeof(v_ref) <> 'object' then
      perform private.raise_api_error('VALIDATION_FAILED', format('invalid input_images entry: %s', v_key));
    end if;
    begin
      if v_ref ? 'asset_id' and not v_ref ? 'persona_asset_id' then
        v_id := (v_ref ->> 'asset_id')::uuid;
        if not exists (select 1 from public.assets a where a.id = v_id and a.persona_id = p_persona_id) then
          perform private.raise_api_error('VALIDATION_FAILED', format('asset %s not found for persona', v_id));
        end if;
      elsif v_ref ? 'persona_asset_id' and not v_ref ? 'asset_id' then
        v_id := (v_ref ->> 'persona_asset_id')::uuid;
        if not exists (select 1 from public.persona_assets pa
                       where pa.id = v_id and pa.persona_id = p_persona_id and pa.is_active) then
          perform private.raise_api_error('VALIDATION_FAILED', format('persona_asset %s not found for persona', v_id));
        end if;
      else
        perform private.raise_api_error('VALIDATION_FAILED',
          format('input_images.%s needs exactly one of asset_id, persona_asset_id', v_key));
      end if;
    exception when invalid_text_representation then
      perform private.raise_api_error('VALIDATION_FAILED', format('input_images.%s has an invalid uuid', v_key));
    end;
  end loop;
end;
$$;

-- content_jobs 입력 검증 (BEFORE INSERT OR UPDATE): RPC를 거치지 않는 직접 쓰기도 검사한다
create or replace function private.validate_content_job()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' or new.input_images is distinct from old.input_images then
    perform private.validate_input_images(new.persona_id, new.input_images);
  end if;
  if jsonb_typeof(new.params) <> 'object' then
    perform private.raise_api_error('VALIDATION_FAILED', 'params must be an object');
  end if;
  if tg_op = 'INSERT' and not exists (
       select 1 from public.personas p where p.id = new.persona_id and p.status = 'active') then
    perform private.raise_api_error('VALIDATION_FAILED', 'persona is not active');
  end if;
  return new;
end;
$$;

create trigger content_jobs_validate
  before insert or update of input_images, params on public.content_jobs
  for each row execute function private.validate_content_job();

-- persona_assets.storage_path는 자기 Persona의 refs 폴더만 (15.5)
create or replace function private.validate_persona_asset()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.storage_path is not null
     and new.storage_path not like format('persona/%s/refs/%%', new.persona_id) then
    perform private.raise_api_error('VALIDATION_FAILED',
      format('storage_path must start with persona/%s/refs/', new.persona_id));
  end if;
  if new.storage_path like '%..%' then
    perform private.raise_api_error('VALIDATION_FAILED', 'storage_path must not contain ..');
  end if;
  return new;
end;
$$;

create trigger persona_assets_validate
  before insert or update of storage_path, persona_id on public.persona_assets
  for each row execute function private.validate_persona_asset();

-- 소유한 Content Job을 잠그고 가져온다
create or replace function private.lock_owned_content_job(p_content_job_id uuid)
returns public.content_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.content_jobs;
begin
  select c.* into v
    from public.content_jobs c
    join public.personas p on p.id = c.persona_id
   where c.id = p_content_job_id and p.user_id = private.require_operator()
     for update of c;
  if v.id is null then
    perform private.raise_api_error('NOT_FOUND', 'content job not found');
  end if;
  return v;
end;
$$;

create or replace function private.expect_status(p_actual text, p_expected text[], p_what text)
returns void
language plpgsql
set search_path = ''
as $$
begin
  if not (p_actual = any (p_expected)) then
    perform private.raise_api_error('INVALID_TRANSITION',
      format('%s is %s (expected %s)', p_what, p_actual, array_to_string(p_expected, ' or ')));
  end if;
end;
$$;

-- -----------------------------------------------------------------------------
-- create_content_job: 생성 (+ 기본은 바로 제출 → queued)
-- -----------------------------------------------------------------------------
create or replace function public.create_content_job(
  p_persona_id      uuid,
  p_content_type    text,
  p_topic           text    default null,
  p_prompt          text    default null,
  p_negative_prompt text    default null,
  p_workflow        text    default null,
  p_params          jsonb   default '{}'::jsonb,
  p_input_images    jsonb   default '{}'::jsonb,
  p_variants        integer default 1,
  p_priority        integer default 5,
  p_platform        text    default null,
  p_submit          boolean default true
)
returns public.content_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := private.require_operator();
  v     public.content_jobs;
begin
  perform private.require_owned_persona(p_persona_id);
  perform private.check_content_job_rate_limit(p_persona_id);
  perform private.validate_input_images(p_persona_id, p_input_images);
  if p_params is not null and jsonb_typeof(p_params) <> 'object' then
    perform private.raise_api_error('VALIDATION_FAILED', 'params must be an object');
  end if;

  perform private.set_reason(case when p_submit then 'operator:create_and_submit' else 'operator:create' end);
  insert into public.content_jobs
    (persona_id, source, created_by, content_type, topic, prompt, negative_prompt, workflow,
     params, input_images, variants, priority, platform, status)
  values
    (p_persona_id, 'operator', v_uid, p_content_type, p_topic, p_prompt, p_negative_prompt, p_workflow,
     coalesce(p_params, '{}'::jsonb), coalesce(p_input_images, '{}'::jsonb), p_variants, p_priority, p_platform,
     case when p_submit then 'queued' else 'draft' end)
  returning * into v;
  perform private.set_reason(null);
  return v;
exception
  when check_violation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- submit_content_job: draft → queued
-- -----------------------------------------------------------------------------
create or replace function public.submit_content_job(p_content_job_id uuid)
returns public.content_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.content_jobs := private.lock_owned_content_job(p_content_job_id);
begin
  perform private.expect_status(v.status, array['draft'], 'content job');
  if not exists (select 1 from public.personas p where p.id = v.persona_id and p.status = 'active') then
    perform private.raise_api_error('VALIDATION_FAILED', 'persona is not active');
  end if;
  perform private.check_content_job_rate_limit(v.persona_id);
  perform private.set_reason('operator:submit');
  update public.content_jobs set status = 'queued' where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
end;
$$;

-- -----------------------------------------------------------------------------
-- cancel_content_job: → cancelled (진행 중인 Automation Job도 취소, 11.9 R5)
-- -----------------------------------------------------------------------------
create or replace function public.cancel_content_job(p_content_job_id uuid, p_reason text default null)
returns public.content_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.content_jobs := private.lock_owned_content_job(p_content_job_id);
begin
  perform private.expect_status(v.status, array['draft', 'queued', 'generating', 'ready', 'failed'], 'content job');
  perform private.set_reason('operator:cancel' || coalesce(': ' || left(p_reason, 200), ''));
  update public.content_jobs set status = 'cancelled' where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
end;
$$;

-- -----------------------------------------------------------------------------
-- retry_content_job: failed → queued (새 회차로 처음부터)
-- -----------------------------------------------------------------------------
create or replace function public.retry_content_job(p_content_job_id uuid)
returns public.content_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.content_jobs := private.lock_owned_content_job(p_content_job_id);
begin
  perform private.expect_status(v.status, array['failed'], 'content job');
  perform private.set_reason('operator:retry');
  update public.content_jobs
     set status = 'queued', run_number = run_number + 1, completed_at = null
   where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
end;
$$;

-- -----------------------------------------------------------------------------
-- regenerate_content_job: ready → queued (같은 프롬프트로 Variant 추가)
-- -----------------------------------------------------------------------------
create or replace function public.regenerate_content_job(p_content_job_id uuid, p_variants integer default null)
returns public.content_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.content_jobs := private.lock_owned_content_job(p_content_job_id);
begin
  perform private.expect_status(v.status, array['ready'], 'content job');
  perform private.check_content_job_rate_limit(v.persona_id);
  perform private.set_reason('operator:regenerate');
  update public.content_jobs
     set status = 'queued', run_number = run_number + 1, completed_at = null,
         variants = coalesce(p_variants, variants)
   where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
exception
  when check_violation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- retry_automation_job: failed → pending (실패한 단계만 다시 실행)
-- -----------------------------------------------------------------------------
create or replace function public.retry_automation_job(p_job_id uuid)
returns public.automation_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v        public.automation_jobs;
  v_parent text;
begin
  select j.* into v
    from public.automation_jobs j
    join public.personas p on p.id = j.persona_id
   where j.id = p_job_id and p.user_id = private.require_operator()
     for update of j;
  if v.id is null then
    perform private.raise_api_error('NOT_FOUND', 'automation job not found');
  end if;
  perform private.expect_status(v.status, array['failed'], 'automation job');

  -- 상위 객체가 이 단계를 다시 실행할 수 있는 상태인지 확인하고 잠근다
  if v.job_type in ('prompt', 'generation') then
    select c.status into v_parent from public.content_jobs c where c.id = v.content_job_id for update;
    perform private.expect_status(v_parent, array['failed', 'generating'], 'content job');
  elsif v.job_type = 'caption' then
    select c.status into v_parent from public.content_jobs c where c.id = v.content_job_id for update;
    perform private.expect_status(v_parent, array['ready', 'published'], 'content job');
  else
    select p.status into v_parent from public.posts p where p.id = v.post_id for update;
    perform private.expect_status(v_parent, array['publishing', 'published'], 'post');
  end if;

  perform private.set_reason('operator:retry_step');
  -- 상위 Content Job이 이 단계 때문에 failed가 됐다면 다시 generating으로 (11.3)
  if v.job_type in ('prompt', 'generation') and v_parent = 'failed' then
    update public.content_jobs set status = 'generating', completed_at = null
     where id = v.content_job_id;
  end if;
  update public.automation_jobs
     set status = 'pending', attempts = 0, run_after = now(),
         locked_at = null, heartbeat_at = null, completed_at = null,
         error_type = null, error_code = null, error_message = null
   where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
exception
  when unique_violation then
    perform private.raise_api_error('INVALID_TRANSITION', 'another job for this step is already active');
end;
$$;

-- -----------------------------------------------------------------------------
-- archive_asset: → archived
-- -----------------------------------------------------------------------------
create or replace function public.archive_asset(p_asset_id uuid)
returns public.assets
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.assets;
begin
  select a.* into v
    from public.assets a
    join public.personas p on p.id = a.persona_id
   where a.id = p_asset_id and p.user_id = private.require_operator()
     for update of a;
  if v.id is null then
    perform private.raise_api_error('NOT_FOUND', 'asset not found');
  end if;
  perform private.expect_status(v.status, array['generated', 'approved', 'rejected'], 'asset');
  perform private.set_reason('operator:archive');
  update public.assets set status = 'archived' where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
end;
$$;

-- -----------------------------------------------------------------------------
-- get_dashboard_summary (12.3)
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
         where e.persona_id = any (v_personas)
         order by e.created_at desc limit 5) f), '[]'::jsonb),
    'bridge', jsonb_build_object(
      'last_heartbeat_at', (select max(j.heartbeat_at) from public.automation_jobs j where j.worker = 'python'),
      'registry_synced_at', (select max(w.synced_at) from public.comfy_workflows w)),
    'publishing_enabled', coalesce((select s.value = 'true'::jsonb from public.app_settings s
                                    where s.key = 'publishing_enabled'), false)
  );
end;
$$;
