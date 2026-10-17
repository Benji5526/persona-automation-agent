-- =============================================================================
-- 0010 execution_targets (TECH_DESIGN 56): Execution Target을 Job에 고정한다
--
-- 세 개념을 분리한다 (56.2). 하나의 설정으로 합치지 않는다.
--   Execution Target  어디서 실행할지   content_jobs·automation_jobs.execution_target ('local'|'cloud')
--   Worker            실제 워커 인스턴스 worker_status.id ('python:local-1')
--   GPU Provider      인프라 제공자      worker_status.provider ('local'|'runpod' …)
--
--   1. app_settings.app_mode                 프로필 표시 ('personal', 읽기 전용)
--   2. app_settings.default_execution_target 새 Content Job의 기본 Target ('local')
--   3. content_jobs.execution_target         생성 시점에 고정된다. 이후 기본값을 바꿔도 움직이지 않는다
--   4. automation_jobs.execution_target      상위 Content Job에서 상속한다 (트리거가 강제)
--   5. claim_*                               generation Job은 Target이 같은 워커만 선점한다 (DB가 강제)
--   6. move_pending_jobs_to_target           대기 중인 Job만 옮기는 명시적 관리 동작
--   7. retry_automation_job(p_execution_target) 실패한 Job을 다른 Target으로 명시 재시도
--
-- 구 설계의 app_settings.active_worker(단일 활성 워커)는 제거한다. 대체 설정을 새로 만들지 않는다.
-- 이 파일은 SaaS 테이블(조직·멤버·구독 등)을 만들지 않는다.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1·2. 설정
-- -----------------------------------------------------------------------------
insert into public.app_settings (key, value, description) values
  ('app_mode', '"personal"'::jsonb,
   'Architecture profile (TECH_DESIGN 56.1). 읽기 전용 표시값: personal | saas'),
  ('default_execution_target', '"local"'::jsonb,
   '새 Content Job의 기본 Execution Target: local | cloud. 이미 만들어진 Job은 움직이지 않는다 (56.2)')
on conflict (key) do nothing;

-- 구 설계 잔재. 이 마이그레이션은 배포된 적이 없으므로 보통은 아무 행도 지우지 않는다.
delete from public.app_settings where key = 'active_worker';

-- -----------------------------------------------------------------------------
-- Target 해석 (컬럼 기본값으로도 쓰인다)
-- -----------------------------------------------------------------------------
create or replace function private.default_execution_target()
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    (select s.value #>> '{}' from public.app_settings s
      where s.key = 'default_execution_target' and jsonb_typeof(s.value) = 'string'
        and (s.value #>> '{}') in ('local', 'cloud')),
    'local')
$$;

-- 컬럼 기본값으로 평가되므로 INSERT 하는 역할이 실행할 수 있어야 한다. 반환값은 'local'|'cloud'뿐이다.
grant execute on function private.default_execution_target() to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- 3. content_jobs.execution_target: 생성 시점에 고정
--    authenticated의 컬럼 단위 INSERT·UPDATE 권한(0005)에 넣지 않는다 →
--    직접 INSERT로는 Target을 지정할 수 없고, 기본값(설정값)만 들어간다. 변경은 RPC로만.
-- -----------------------------------------------------------------------------
alter table public.content_jobs
  add column if not exists execution_target text not null
    default private.default_execution_target()
    check (execution_target in ('local', 'cloud'));

-- -----------------------------------------------------------------------------
-- 4. automation_jobs.execution_target: 상위 Content Job에서 상속
--    Content Job이 있는 Job은 트리거가 상위 값으로 덮어쓴다 → 두 값이 조용히 어긋날 수 없다.
--    Post만 있는 Job(publish·analytics)은 null이다.
-- -----------------------------------------------------------------------------
alter table public.automation_jobs
  add column if not exists execution_target text
    check (execution_target in ('local', 'cloud'));

create or replace function private.inherit_execution_target()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.content_job_id is not null then
    select c.execution_target into new.execution_target
      from public.content_jobs c where c.id = new.content_job_id;
  end if;
  return new;
end;
$$;

revoke all on function private.inherit_execution_target() from public, anon, authenticated;

drop trigger if exists automation_jobs_inherit_execution_target on public.automation_jobs;
create trigger automation_jobs_inherit_execution_target
  before insert or update of content_job_id, execution_target on public.automation_jobs
  for each row execute function private.inherit_execution_target();

-- 이미 만들어진 Job이 있는 DB에 적용돼도 Target이 상위 Content Job과 어긋나지 않게 맞춘다 (새 DB에서는 0행이다).
-- 컬럼을 새로 더하면 기존 automation_jobs 행은 null이고, 선점은 null을 local로 보기 때문에 cloud로 만든 Job이 있었다면 틀어진다.
update public.automation_jobs j
   set execution_target = c.execution_target
  from public.content_jobs c
 where c.id = j.content_job_id and j.execution_target is distinct from c.execution_target;

-- 선점 질의가 Target으로 거르므로 인덱스를 맞춘다
create index if not exists automation_jobs_claim_idx
  on public.automation_jobs (job_type, execution_target, priority desc, created_at)
  where status = 'pending';

-- -----------------------------------------------------------------------------
-- 5. worker_status: 실행 Target과 공급자 (GPU 모델·VRAM은 이미 gpu jsonb로 보고된다)
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
-- 6. 선점 gating: generation Job은 Target이 같은 워커만 가져간다
--
--    워커의 Target은 요청 본문이 아니라 서버가 worker_status에서 읽는다 (56.5).
--    등록되지 않았거나 Target을 보고하지 않은 워커는 generation Job을 가져갈 수 없다.
--    0011에서 워커 신원을 JWT 클레임으로 좁힌다. 다른 job_type(prompt·caption·publish …)은 영향 없다.
-- -----------------------------------------------------------------------------
drop function if exists private.worker_may_claim(text, text);

create or replace function private.worker_target(p_worker text)
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select w.target from public.worker_status w where w.id = p_worker and w.kind = 'python'
$$;

revoke all on function private.worker_target(text) from public, anon, authenticated;

-- 등록되지 않았거나 Target을 보고하지 않은 워커는 'local'로 본다 (Personal Edition의 기본값이자
-- 신뢰하는 자기 PC). 조이는 방향은 cloud다: cloud Job은 target='cloud'로 **등록된** 워커만 가져갈 수
-- 있고, cloud 워커는 local Job을 가져갈 수 없다. 0011에서 p_worker 대신 JWT 클레임으로 신원을 좁힌다.
create or replace function private.worker_may_claim(p_job_type text, p_worker text, p_job_target text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select case
    when p_job_type <> 'generation' then true
    else coalesce(p_job_target, 'local') = coalesce(private.worker_target(p_worker), 'local')
  end
$$;

revoke all on function private.worker_may_claim(text, text, text) from public, anon, authenticated;

create or replace function public.claim_automation_job(p_job_id uuid, p_worker text)
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
     where j.id = p_job_id and j.status = 'pending' and j.run_after <= now()
       and private.worker_may_claim(j.job_type, p_worker, j.execution_target)
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
begin
  perform private.set_reason('claim');
  return query
    update public.automation_jobs j
       set status = 'processing', attempts = j.attempts + 1, claimed_by = p_worker,
           locked_at = now(), heartbeat_at = now(), started_at = coalesce(j.started_at, now())
     where j.id = (
             select c.id from public.automation_jobs c
              where c.status = 'pending' and c.job_type = p_job_type and c.run_after <= now()
                and private.worker_may_claim(p_job_type, p_worker, c.execution_target)
              order by c.priority desc, c.created_at
              limit 1
                for update skip locked)
    returning j.*;
  perform private.set_reason(null);
end;
$$;

-- -----------------------------------------------------------------------------
-- 7. create_content_job: Target을 생성 시점에 정한다
--    인자가 하나 늘어나므로 예전 시그니처를 지우고 다시 만든다 (0003 파일은 고치지 않는다).
--    오버로드를 남기면 예전 경로가 Target 로직을 건너뛸 수 있다.
-- -----------------------------------------------------------------------------
drop function if exists public.create_content_job(
  uuid, text, text, text, text, text, jsonb, jsonb, integer, integer, text, boolean);

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
  p_submit          boolean default true,
  p_execution_target text   default null
)
returns public.content_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid    uuid := private.require_operator();
  v        public.content_jobs;
  v_target text;
begin
  perform private.require_owned_persona(p_persona_id);
  perform private.check_content_job_rate_limit(p_persona_id);
  perform private.validate_input_images(p_persona_id, p_input_images);
  if p_params is not null and jsonb_typeof(p_params) <> 'object' then
    perform private.raise_api_error('VALIDATION_FAILED', 'params must be an object');
  end if;

  -- Target: 요청값이 없으면 설정 기본값. 임의 문자열은 받지 않는다.
  v_target := coalesce(p_execution_target, private.default_execution_target());
  if v_target not in ('local', 'cloud') then
    perform private.raise_api_error('VALIDATION_FAILED',
      format('execution_target must be local or cloud (got %s)', p_execution_target));
  end if;

  perform private.set_reason(case when p_submit then 'operator:create_and_submit' else 'operator:create' end);
  insert into public.content_jobs
    (persona_id, source, created_by, content_type, topic, prompt, negative_prompt, workflow,
     params, input_images, variants, priority, platform, status, execution_target)
  values
    (p_persona_id, 'operator', v_uid, p_content_type, p_topic, p_prompt, p_negative_prompt, p_workflow,
     coalesce(p_params, '{}'::jsonb), coalesce(p_input_images, '{}'::jsonb), p_variants, p_priority, p_platform,
     case when p_submit then 'queued' else 'draft' end, v_target)
  returning * into v;
  perform private.set_reason(null);
  return v;
exception
  when check_violation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- 8. create_automation_job: generation Job은 상위 Content Job의 Target을 그대로 받는다
--    (트리거가 최종 강제한다. 여기서 명시해 의도를 분명히 한다.)
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
    (persona_id, content_job_id, post_id, job_type, worker, payload, priority, max_attempts,
     idempotency_key, execution_target)
  values
    (p_persona_id, p_content_job_id, p_post_id, p_job_type,
     coalesce(p_worker, case when p_job_type = 'generation' then 'python' else 'n8n' end),
     coalesce(p_payload, '{}'::jsonb),
     coalesce(p_priority, v_cj.priority, 5),
     p_max_attempts, p_idempotency_key, v_cj.execution_target)
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
-- 9. move_pending_jobs_to_target: 대기 중인 generation Job만 옮기는 명시적 관리 동작
--    기본 Target 변경은 이 함수를 자동으로 부르지 않는다 (D3).
--    실행 중(processing)·완료·실패·취소된 Job은 절대 옮기지 않는다.
-- -----------------------------------------------------------------------------
create or replace function public.move_pending_jobs_to_target(p_target text)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid   uuid := private.require_admin();
  v_count integer;
begin
  if p_target is null or p_target not in ('local', 'cloud') then
    perform private.raise_api_error('VALIDATION_FAILED',
      format('execution_target must be local or cloud (got %s)', p_target));
  end if;

  perform private.set_reason('operator:move_execution_target');

  -- 상위 Content Job 먼저. 이후 automation_jobs 갱신 시 상속 트리거가 같은 값을 읽는다.
  update public.content_jobs c
     set execution_target = p_target
   where c.execution_target is distinct from p_target
     and c.id in (select j.content_job_id
                    from public.automation_jobs j
                   where j.job_type = 'generation' and j.status = 'pending'
                     and j.content_job_id is not null);

  update public.automation_jobs j
     set execution_target = p_target
   where j.job_type = 'generation' and j.status = 'pending'
     and j.execution_target is distinct from p_target;
  get diagnostics v_count = row_count;

  perform private.set_reason(null);

  insert into public.security_events (event_type, actor_type, actor_id, detail)
  values ('EXECUTION_TARGET_JOBS_MOVED', 'operator', v_uid,
          jsonb_build_object('target', p_target, 'moved', v_count));

  return v_count;
end;
$$;

-- -----------------------------------------------------------------------------
-- 10. retry_automation_job: 실패한 Job을 다른 Target으로 명시 재시도
--     자동 failover는 하지 않는다. Target을 바꾸려면 호출자가 분명히 지정해야 한다 (D6·8).
--     인자가 늘어나므로 예전 시그니처를 지운다 (0003·0009 파일은 고치지 않는다).
-- -----------------------------------------------------------------------------
drop function if exists public.retry_automation_job(uuid);

create or replace function public.retry_automation_job(p_job_id uuid, p_execution_target text default null)
returns public.automation_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v        public.automation_jobs;
  v_parent text;
begin
  if p_execution_target is not null and p_execution_target not in ('local', 'cloud') then
    perform private.raise_api_error('VALIDATION_FAILED',
      format('execution_target must be local or cloud (got %s)', p_execution_target));
  end if;

  select j.* into v
    from public.automation_jobs j
    join public.personas p on p.id = j.persona_id
   where j.id = p_job_id and p.user_id = private.require_operator()
     for update of j;
  if v.id is null then
    perform private.raise_api_error('NOT_FOUND', 'automation job not found');
  end if;
  perform private.expect_status(v.status, array['failed'], 'automation job');
  perform private.require_active_persona(v.persona_id);

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

  -- Target 변경은 명시했을 때만. 상위 Content Job까지 함께 옮겨 두 값이 어긋나지 않게 한다.
  if p_execution_target is not null and v.content_job_id is not null then
    update public.content_jobs set execution_target = p_execution_target where id = v.content_job_id;
  end if;

  -- 상위 Content Job이 이 단계 때문에 failed가 됐다면 다시 generating으로 (11.3)
  if v.job_type in ('prompt', 'generation') and v_parent = 'failed' then
    update public.content_jobs set status = 'generating', completed_at = null
     where id = v.content_job_id;
  end if;
  update public.automation_jobs
     set status = 'pending', attempts = 0, run_after = now(),
         locked_at = null, heartbeat_at = null, completed_at = null,
         error_type = null, error_code = null, error_message = null,
         execution_target = coalesce(p_execution_target, execution_target)
   where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
exception
  when unique_violation then
    perform private.raise_api_error('INVALID_TRANSITION', 'another job for this step is already active');
end;
$$;

-- -----------------------------------------------------------------------------
-- 11. 설정 RPC: default_execution_target 변경(admin), app_mode는 읽기 전용
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
                           'default_execution_target', 'app_mode'));
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
    update public.app_settings set value = p_value where key = p_key;
  end if;

  -- 기본값을 바꿔도 이미 만들어진 Job은 옮기지 않는다 (D3). 이동은 move_pending_jobs_to_target뿐이다.
  insert into public.security_events (event_type, actor_type, actor_id, detail)
  values (case when p_key = 'publishing_enabled'
               then case when p_value = 'true'::jsonb then 'PUBLISHING_ENABLED' else 'PUBLISHING_DISABLED' end
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
-- 12. 함수 실행 권한
--     create or replace는 기존 권한을 유지하지만, drop 후 다시 만든 함수는 다시 부여해야 한다.
-- -----------------------------------------------------------------------------
grant execute on function
  public.create_content_job(uuid, text, text, text, text, text, jsonb, jsonb, integer, integer, text, boolean, text),
  public.retry_automation_job(uuid, text),
  public.move_pending_jobs_to_target(text)
to authenticated;
