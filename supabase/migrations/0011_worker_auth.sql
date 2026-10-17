-- =============================================================================
-- 0011 worker_auth (TECH_DESIGN 56.5): 제한된 Worker 신원과 RPC 화이트리스트
--
-- Cloud Worker는 service_role을 쓰지 않는다. 대신 수명이 짧은 Worker JWT로 들어오고,
-- Postgres 역할 pa_worker로 전환되어 **명시적으로 허용된 RPC만** 실행한다.
--
--   Cloud Worker → Worker JWT(role=pa_worker) → PostgREST → pa_worker → 허용된 RPC
--
--   1. worker_credentials  발급한 자격의 대장(jti). **서명 키는 저장하지 않는다** (17조)
--   2. pa_worker           NOLOGIN, 테이블 권한 없음, 화이트리스트 RPC만 EXECUTE
--   3. private.worker_identity() / require_worker()
--                          신원은 요청 본문이 아니라 JWT 클레임에서 온다. 존재·폐기·만료·Target 일치를 확인
--   4. claim_*             pa_worker 세션이면 p_worker를 무시하고 클레임의 신원을 쓴다
--   5. worker_get_job_context  실행에 필요한 것만 돌려준다. SNS 토큰·다른 Persona·무관한 Asset은 제외
--   6. revoke_worker_credential  폐기하면 이후 모든 RPC가 실패한다
--
-- Local Worker는 Phase 0 동안 service_role(secret_key)을 그대로 쓸 수 있다 (19조).
-- 두 경로는 같은 RPC를 쓴다 — 구현을 둘로 나누지 않는다.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. worker_credentials: 발급한 자격의 대장
-- -----------------------------------------------------------------------------
create table if not exists public.worker_credentials (
  jti        uuid primary key,
  worker_id  text not null check (worker_id ~ '^[a-z0-9:_.-]{1,64}$'),
  target     text not null check (target in ('local', 'cloud')),
  provider   text check (provider ~ '^[a-z0-9_-]{1,32}$'),
  issued_at  timestamptz not null default now(),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  created_by uuid references public.users (id) on delete set null,
  created_at timestamptz not null default now(),
  -- 16조: Worker JWT 수명은 24시간 이하
  constraint worker_credentials_lifetime
    check (expires_at > issued_at and expires_at <= issued_at + interval '24 hours')
);

create index if not exists worker_credentials_worker_id_idx on public.worker_credentials (worker_id);
create index if not exists worker_credentials_live_idx
  on public.worker_credentials (expires_at) where revoked_at is null;

alter table public.worker_credentials enable row level security;
-- 정책을 만들지 않는다 → operator는 직접 읽을 수 없다. 조회는 admin RPC로만.
revoke all on public.worker_credentials from anon, authenticated;

-- -----------------------------------------------------------------------------
-- 2. pa_worker 역할: 로그인 불가, 테이블 권한 없음
-- -----------------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'pa_worker') then
    create role pa_worker nologin noinherit;
  end if;
end
$$;

grant usage on schema public to pa_worker;
revoke all on all tables in schema public from pa_worker;
revoke all on all sequences in schema public from pa_worker;
revoke all on all functions in schema public from pa_worker;
alter default privileges in schema public revoke all on tables from pa_worker;
alter default privileges in schema public revoke all on functions from pa_worker;

-- PostgREST가 JWT의 role 클레임으로 역할을 바꾸려면 authenticator가 pa_worker의 멤버여야 한다.
-- authenticator는 Supabase에만 있다 (로컬 테스트 스텁에는 없다).
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'authenticator') then
    execute 'grant pa_worker to authenticator';
  end if;
end
$$;

-- -----------------------------------------------------------------------------
-- 3. Worker 신원: JWT 클레임에서만 온다 (14조)
-- -----------------------------------------------------------------------------
create or replace function private.worker_claims()
returns jsonb
language sql
stable
set search_path = ''
as $$
  select nullif(current_setting('request.jwt.claims', true), '')::jsonb
$$;

-- 유효하면 자격 행을, 아니면 null을 돌려준다. 예외를 던지지 않는다.
create or replace function private.worker_identity()
returns public.worker_credentials
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_claims jsonb := private.worker_claims();
  v_cred   public.worker_credentials;
  v_jti    uuid;
begin
  if v_claims is null or coalesce(v_claims ->> 'role', '') <> 'pa_worker' then
    return null;
  end if;

  begin
    v_jti := (v_claims ->> 'jti')::uuid;
  exception when others then
    return null;
  end;
  if v_jti is null then
    return null;
  end if;

  select * into v_cred from public.worker_credentials c where c.jti = v_jti;
  if v_cred.jti is null                                    -- 자격이 존재하는가
     or v_cred.revoked_at is not null                      -- 폐기되지 않았는가
     or v_cred.expires_at <= now()                         -- DB 기준 만료
     or coalesce((v_claims ->> 'exp')::numeric, 0) <= extract(epoch from now())  -- 토큰 자체의 만료
     or coalesce(v_claims ->> 'worker_id', '') <> v_cred.worker_id               -- 신원 일치
     or coalesce(v_claims ->> 'target', '') <> v_cred.target then                -- Target 일치
    return null;
  end if;

  return v_cred;
end;
$$;

revoke all on function private.worker_claims() from public, anon, authenticated;
revoke all on function private.worker_identity() from public, anon, authenticated;

-- 유효하지 않으면 FORBIDDEN. pa_worker 세션인데 자격이 깨졌을 때만 쓴다.
create or replace function private.require_worker()
returns public.worker_credentials
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_cred public.worker_credentials := private.worker_identity();
begin
  if v_cred.jti is null then
    perform private.raise_api_error('FORBIDDEN', 'worker credential is missing, expired or revoked');
  end if;
  return v_cred;
end;
$$;

revoke all on function private.require_worker() from public, anon, authenticated;

-- -----------------------------------------------------------------------------
-- 4. 세션 종류와 Worker 범위 (최종 보안 게이트에서 보강)
--
--   DB 역할은 PostgREST가 **검증된 JWT**의 role 클레임으로 정한다 (SET LOCAL ROLE). 클라이언트가 바꿀 수 없다.
--     pa_worker           → 'worker'    : 유효한 자격이 반드시 있어야 하고, 자기가 선점한 generation Job만 다룬다
--     anon, authenticated → 'untrusted' : 워커 RPC를 쓸 수 없다 (EXECUTE도 없고, 여기서 한 번 더 막는다)
--     그 밖 (service_role, 소유자 연결) → 'trusted' : 로컬 Phase 0. 전권 키이므로 p_worker를 믿는다
--
--   pa_worker 세션에서 신원이 깨졌을 때(폐기·만료·클레임 누락) **'local'로 폴백하지 않는다.** 오류를 낸다.
--   빈 결과로 넘기면 워커가 같은 자격으로 계속 두드리고, local Job은 그대로 가져갈 수 있었다 (게이트에서 발견).
--
--   service_role 경로의 의도적 완화: 등록되지 않은 워커도 local Job은 선점한다 (F0 테스트의 전제).
--   service_role은 어차피 전권이고 report_worker_status로 스스로 등록할 수 있어서 등록을 강제해도 보안이 늘지 않는다.
-- -----------------------------------------------------------------------------
create or replace function private.worker_session_kind()
returns text
language sql
stable
set search_path = ''
as $$
  select case coalesce(nullif(current_setting('role', true), ''), 'none')
           when 'pa_worker' then 'worker'
           when 'anon' then 'untrusted'
           when 'authenticated' then 'untrusted'
           else 'trusted'
         end
$$;

-- Worker 세션이면 유효한 자격(없으면 FORBIDDEN), trusted면 모든 칸이 null인 행, untrusted면 FORBIDDEN.
-- 호출한 쪽은 `v_cred.jti is not null`로 Worker 세션인지 구분한다.
create or replace function private.session_worker()
returns public.worker_credentials
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_kind text := private.worker_session_kind();
  v_none public.worker_credentials;
begin
  if v_kind = 'untrusted' then
    perform private.raise_api_error('FORBIDDEN', 'not a worker session');
  end if;
  if v_kind = 'worker' then
    return private.require_worker();
  end if;
  return v_none;
end;
$$;

revoke all on function private.worker_session_kind() from public, anon, authenticated;
revoke all on function private.session_worker() from public, anon, authenticated;

-- Worker 세션이 볼 수 있는 Job: 자기가 선점한 generation Job뿐이다. 존재 여부도 알려 주지 않는다 (둘 다 FORBIDDEN).
-- trusted 세션은 있는 Job이면 된다. p_processing = true면 실행 중(processing)이어야 한다.
create or replace function private.scoped_job(p_job_id uuid, p_processing boolean)
returns public.automation_jobs
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_cred public.worker_credentials := private.session_worker();
  v_job  public.automation_jobs;
begin
  select * into v_job from public.automation_jobs j where j.id = p_job_id;
  if v_cred.jti is not null then
    if v_job.id is null or v_job.claimed_by is distinct from v_cred.worker_id or v_job.job_type <> 'generation'
       or (p_processing and v_job.status <> 'processing') then
      perform private.raise_api_error('FORBIDDEN', 'job is not claimed by this worker');
    end if;
  elsif v_job.id is null then
    perform private.raise_api_error('NOT_FOUND', 'automation job not found');
  elsif p_processing and v_job.status <> 'processing' then
    perform private.raise_api_error('FORBIDDEN', 'job is not claimed by this worker');
  end if;
  return v_job;
end;
$$;

revoke all on function private.scoped_job(uuid, boolean) from public, anon, authenticated;

-- 이 Content Job이 입력으로 가리키는 Asset id / Persona Asset id (input_images = {slot: {asset_id}|{persona_asset_id}})
create or replace function private.job_input_refs(p_input_images jsonb, p_key text)
returns setof uuid
language sql
immutable
set search_path = ''
as $$
  select (s.value ->> p_key)::uuid
    from jsonb_each(case when jsonb_typeof(p_input_images) = 'object' then p_input_images else '{}'::jsonb end) s
   where jsonb_typeof(s.value) = 'object'
     and (s.value ->> p_key) ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
$$;

revoke all on function private.job_input_refs(jsonb, text) from public, anon, authenticated;

-- 0010의 임시 도우미를 없앤다: Target 판단은 아래 claim 함수가 서버에서 직접 한다.
drop function if exists private.worker_may_claim(text, text, text);

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
              order by c.priority desc, c.created_at
              limit 1
                for update skip locked)
    returning j.*;
  perform private.set_reason(null);
end;
$$;

drop function if exists private.effective_worker_id(text);
drop function if exists private.effective_worker_target(text);

-- 결과 보고(complete·fail·register_asset …)가 쓰는 잠금. Worker 세션이면 자기가 선점한 generation Job이어야 한다.
-- locked_at이 새어 나가도 다른 워커는 남의 Job에 결과를 쓸 수 없다. 맞지 않으면 "잠금을 잃음"(null)이다.
create or replace function private.lock_processing_job(p_job_id uuid, p_locked_at timestamptz, p_job_type text default null)
returns public.automation_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v      public.automation_jobs;
  v_cred public.worker_credentials := private.session_worker();
begin
  select * into v from public.automation_jobs j
   where j.id = p_job_id and j.status = 'processing' and j.locked_at = p_locked_at
     and (p_job_type is null or j.job_type = p_job_type)
     for update;
  if v.id is not null and v_cred.jti is not null
     and (v.claimed_by is distinct from v_cred.worker_id or v.job_type <> 'generation') then
    v := null;
  end if;
  return v;
end;
$$;

create or replace function public.heartbeat_automation_job(p_job_id uuid, p_locked_at timestamptz)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_cred public.worker_credentials := private.session_worker();
begin
  update public.automation_jobs set heartbeat_at = now()
   where id = p_job_id and status = 'processing' and locked_at = p_locked_at
     and (v_cred.jti is null or (claimed_by = v_cred.worker_id and job_type = 'generation'));
  return found;
end;
$$;

-- -----------------------------------------------------------------------------
-- 5. worker_get_job_context: 실행에 필요한 것만 (13조)
--    자기가 선점한 Job만 볼 수 있다. SNS 토큰·다른 Persona·무관한 Asset·설정은 포함하지 않는다.
-- -----------------------------------------------------------------------------
create or replace function public.worker_get_job_context(p_job_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_job public.automation_jobs := private.scoped_job(p_job_id, true);
  v_cj  public.content_jobs;
begin
  select * into v_cj from public.content_jobs c where c.id = v_job.content_job_id;

  return jsonb_build_object(
    'job', jsonb_build_object(
      'id', v_job.id, 'job_type', v_job.job_type, 'status', v_job.status,
      'attempts', v_job.attempts, 'max_attempts', v_job.max_attempts,
      'locked_at', v_job.locked_at, 'payload', v_job.payload,
      'execution_target', v_job.execution_target, 'persona_id', v_job.persona_id),
    'content_job', case when v_cj.id is null then null else jsonb_build_object(
      'id', v_cj.id, 'content_type', v_cj.content_type, 'topic', v_cj.topic,
      'prompt', v_cj.prompt, 'prompt_parts', v_cj.prompt_parts,
      'negative_prompt', v_cj.negative_prompt, 'workflow', v_cj.workflow,
      'params', v_cj.params, 'input_images', v_cj.input_images,
      'variants', v_cj.variants, 'run_number', v_cj.run_number,
      'metadata', v_cj.metadata, 'persona_id', v_cj.persona_id,
      'execution_target', v_cj.execution_target) end,
    -- Persona는 실행에 쓰는 설정만 (visual_settings·content_rules).
    -- personality·speaking_style·interaction_rules 같은 대화용 설정은 생성에 필요 없으므로 주지 않는다.
    'persona', (select jsonb_build_object('id', p.id, 'slug', p.slug,
                                          'visual_settings', p.visual_settings,
                                          'content_rules', p.content_rules)
                  from public.personas p where p.id = v_job.persona_id),
    'persona_assets', coalesce((
      select jsonb_agg(jsonb_build_object('id', a.id, 'asset_type', a.asset_type,
                                          'name', a.name, 'storage_path', a.storage_path,
                                          'metadata', a.metadata)
                       order by a.asset_type, a.name)
        from public.persona_assets a
       where a.persona_id = v_job.persona_id and a.is_active), '[]'::jsonb),
    -- 이 Job의 입력으로 **참조된** Asset만. 같은 Persona의 다른 Asset은 주지 않는다.
    'input_assets', coalesce((
      select jsonb_agg(jsonb_build_object('id', s.id, 'storage_bucket', s.storage_bucket,
                                          'storage_path', s.storage_path, 'mime_type', s.mime_type)
                       order by s.created_at)
        from public.assets s
       where s.persona_id = v_job.persona_id
         and s.id in (select private.job_input_refs(v_cj.input_images, 'asset_id'))), '[]'::jsonb),
    -- 이 Job이 이미 등록한 Asset. 회수·재시도로 다시 선점됐을 때 또 만들지 않기 위해 (49.5 1번).
    'existing_assets', coalesce((
      select jsonb_agg(jsonb_build_object('id', s.id, 'asset_type', s.asset_type,
                                          'file_name', s.file_name, 'storage_bucket', s.storage_bucket,
                                          'storage_path', s.storage_path, 'public_url', s.public_url,
                                          'status', s.status)
                       order by s.created_at)
        from public.assets s where s.automation_job_id = v_job.id), '[]'::jsonb)
  );
end;
$$;

-- 입력 Asset 하나. **이 Job의 입력으로 참조된** 같은 Persona의 Asset만 돌려준다. 아니면 null.
create or replace function public.worker_get_input_asset(p_job_id uuid, p_asset_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_job public.automation_jobs := private.scoped_job(p_job_id, true);
  v_cj  public.content_jobs;
begin
  select * into v_cj from public.content_jobs c where c.id = v_job.content_job_id;

  return (select jsonb_build_object('id', a.id, 'persona_id', a.persona_id,
                                    'storage_bucket', a.storage_bucket,
                                    'storage_path', a.storage_path, 'mime_type', a.mime_type,
                                    'width', a.width, 'height', a.height, 'status', a.status)
            from public.assets a
           where a.id = p_asset_id and a.persona_id = v_job.persona_id
             and a.id in (select private.job_input_refs(v_cj.input_images, 'asset_id')));
end;
$$;

-- 실행 기록: Worker 세션이면 자기가 선점한 Job에만 쓸 수 있다 (남의 Job에 로그를 위조하지 못한다)
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
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_cred public.worker_credentials := private.session_worker();
begin
  if v_cred.jti is not null then
    perform private.scoped_job(p_job_id, false);   -- 소유 확인. 아니면 FORBIDDEN
  end if;
  insert into public.execution_logs
    (automation_job_id, step, service, status, input_data, output_data, duration_ms, execution_ref, error)
  values
    (p_job_id, p_step, p_service, p_status, private.redact_jsonb(p_input), private.redact_jsonb(p_output),
     p_duration_ms, p_execution_ref, left(private.redact_text(p_error), 4000));
end;
$$;

-- 보안 이벤트: Worker 세션이 쓸 수 있는 종류는 정해져 있고, actor_type은 서버가 python으로 정하며,
-- persona는 자기가 선점한 Job의 Persona뿐이다. 감사 기록을 위조하지 못한다.
create or replace function public.log_security_event(
  p_event_type text,
  p_actor_type text,
  p_source_ip  text  default null,
  p_detail     jsonb default '{}'::jsonb,
  p_persona_id uuid  default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_cred   public.worker_credentials := private.session_worker();
  v_actor  text  := p_actor_type;
  v_detail jsonb := case when jsonb_typeof(coalesce(p_detail, '{}'::jsonb)) = 'object'
                         then coalesce(p_detail, '{}'::jsonb)
                         else jsonb_build_object('value', p_detail) end;
begin
  if v_cred.jti is not null then
    if p_event_type not in ('OUTPUT_UNEXPECTED', 'WORKER_AUTH_FAILED', 'API_AUTH_FAILED') then
      perform private.raise_api_error('FORBIDDEN', 'security event type is not allowed for a worker');
    end if;
    if p_persona_id is not null and not exists (
         select 1 from public.automation_jobs j
          where j.persona_id = p_persona_id and j.claimed_by = v_cred.worker_id and j.job_type = 'generation') then
      perform private.raise_api_error('FORBIDDEN', 'persona is not served by this worker');
    end if;
    v_actor := 'python';
    v_detail := v_detail || jsonb_build_object('worker_id', v_cred.worker_id);
  end if;

  insert into public.security_events (event_type, actor_type, source_ip, detail, persona_id)
  values (p_event_type, v_actor, nullif(p_source_ip, '')::inet,
          coalesce(private.redact_jsonb(v_detail), '{}'::jsonb), p_persona_id);
end;
$$;

-- 상태 보고: Worker 세션은 **자기 자신만** 보고할 수 있고, target·provider는 자격에서 온다 (본문 값은 무시).
-- 다른 워커의 상태 행을 덮어쓰거나 Target을 속이지 못한다.
create or replace function public.report_worker_status(p_worker_id text, p_kind text, p_info jsonb default '{}'::jsonb)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_cred   public.worker_credentials := private.session_worker();
  v_info   jsonb := coalesce(p_info, '{}'::jsonb);
  v_models jsonb;
begin
  if v_cred.jti is not null then
    if p_worker_id is distinct from v_cred.worker_id or p_kind is distinct from 'python' then
      perform private.raise_api_error('FORBIDDEN', 'a worker may only report its own status');
    end if;
    v_info := v_info || jsonb_build_object('target', v_cred.target, 'provider', coalesce(v_cred.provider, v_cred.target));
  end if;
  v_models := v_info -> 'models';

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
          (v_info ->> 'comfyui_ok')::boolean,
          coalesce(v_info -> 'gpu', '{}'::jsonb),
          nullif(v_info ->> 'current_job_id', '')::uuid,
          coalesce((v_info ->> 'queue_size')::integer, 0),
          v_info ->> 'version',
          coalesce(v_models, '{}'::jsonb),
          nullif(v_info ->> 'target', ''),
          nullif(v_info ->> 'provider', ''),
          now())
  on conflict (id) do update
     set kind = excluded.kind, comfyui_ok = excluded.comfyui_ok, gpu = excluded.gpu,
         current_job_id = excluded.current_job_id, queue_size = excluded.queue_size,
         version = excluded.version,
         models = coalesce(v_models, w.models),
         target = coalesce(excluded.target, w.target),
         provider = coalesce(excluded.provider, w.provider),
         last_seen_at = excluded.last_seen_at;
exception
  when check_violation or invalid_text_representation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- 6. 자격 발급·폐기
--    발급은 admin만. 서명은 소유자 PC의 스크립트가 하고, 여기에는 대장만 남는다 (17조).
-- -----------------------------------------------------------------------------
create or replace function public.issue_worker_credential(
  p_jti        uuid,
  p_worker_id  text,
  p_target     text,
  p_expires_at timestamptz,
  p_provider   text default null
)
returns public.worker_credentials
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid  uuid := private.require_admin();
  v_cred public.worker_credentials;
begin
  if p_target not in ('local', 'cloud') then
    perform private.raise_api_error('VALIDATION_FAILED', 'target must be local or cloud');
  end if;
  insert into public.worker_credentials (jti, worker_id, target, provider, expires_at, created_by)
  values (p_jti, p_worker_id, p_target, p_provider, p_expires_at, v_uid)
  returning * into v_cred;

  insert into public.security_events (event_type, actor_type, actor_id, detail)
  values ('WORKER_CREDENTIAL_ISSUED', 'operator', v_uid,
          jsonb_build_object('jti', p_jti, 'worker_id', p_worker_id,
                             'target', p_target, 'expires_at', p_expires_at));
  return v_cred;
exception
  when check_violation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
  when unique_violation then
    perform private.raise_api_error('VALIDATION_FAILED', 'jti already issued');
end;
$$;

-- admin은 아무 자격이나, Worker는 자기 자격만 폐기할 수 있다.
create or replace function public.revoke_worker_credential(p_jti uuid)
returns public.worker_credentials
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_cred   public.worker_credentials := private.worker_identity();
  v_actor  text := 'operator';
  v_uid    uuid;
  v_target public.worker_credentials;
begin
  if v_cred.jti is not null then
    v_actor := 'python';
    if v_cred.jti <> p_jti then
      perform private.raise_api_error('FORBIDDEN', 'a worker may only revoke its own credential');
    end if;
  else
    v_uid := private.require_admin();
  end if;

  update public.worker_credentials
     set revoked_at = coalesce(revoked_at, now())
   where jti = p_jti
  returning * into v_target;
  if v_target.jti is null then
    perform private.raise_api_error('NOT_FOUND', 'worker credential not found');
  end if;

  insert into public.security_events (event_type, actor_type, actor_id, detail)
  values ('WORKER_CREDENTIAL_REVOKED', v_actor, v_uid,
          jsonb_build_object('jti', p_jti, 'worker_id', v_target.worker_id, 'target', v_target.target));
  return v_target;
end;
$$;

-- admin이 발급 현황을 본다 (토큰 자체는 어디에도 저장되지 않는다).
create or replace function public.list_worker_credentials()
returns setof public.worker_credentials
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  perform private.require_admin();
  return query select * from public.worker_credentials order by issued_at desc;
end;
$$;

-- -----------------------------------------------------------------------------
-- 8. Storage 격리 (최종 보안 게이트)
--
--   Worker가 Storage에 닿는 경로는 두 개뿐이다: 입력 파일 읽기, 결과 파일 쓰기.
--   둘 다 **자기가 지금 선점한 generation Job**을 기준으로 DB가 판단한다 (storage.objects RLS).
--
--   읽기  : 선점한 Job이 입력으로 **참조한** 파일 하나하나 (Job.input_images의 asset_id / persona_asset_id).
--           경로를 추측해도, 같은 Persona의 다른 파일도, 목록 조회(list)도 그 밖은 보이지 않는다.
--   쓰기  : media 버킷의 persona/{선점한 Job의 Persona}/assets/{uuid}[_thumb].{png|jpg|webp} 뿐이다.
--           이미 등록된 Asset의 경로는 쓸 수 없다. UPDATE·DELETE 권한은 아예 없다 → 덮어쓰기·삭제 불가.
--   기타  : 경로 조작(..·중첩·대문자 UUID·다른 확장자)은 정규식에서 막힌다. 해지·만료된 자격은 아무것도 못 한다.
--
--   한계: 경로에 Job id가 없다 (TECH_DESIGN 15.5의 persona/{id}/assets/{asset_id}, 공개 URL과 테스트가 이 형태에 의존).
--   그래서 쓰기 경계는 "선점한 Job의 Persona"이고 Job 단위가 아니다. 같은 워커가 같은 Persona의 Job을 둘
--   들고 있으면 한쪽 경로에 쓰고 다른 쪽에 등록할 수 있다 — 그래도 Asset 등록(register_asset)은 선점한 Job에만,
--   그 Job의 Persona 경로에만 가능하다. 호스팅 Storage API가 pa_worker 역할로 이 정책을 실제로 적용하는지는
--   로컬에서 검증할 수 없다 (supabase/README.md의 "외부 검증" 참고).
-- -----------------------------------------------------------------------------
create or replace function private.worker_may_read_object(p_bucket text, p_name text)
returns boolean
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_cred public.worker_credentials := private.worker_identity();
begin
  if v_cred.jti is null or p_bucket is null or p_name is null then
    return false;
  end if;
  return exists (
    select 1
      from public.automation_jobs j
      join public.content_jobs c on c.id = j.content_job_id
     where j.status = 'processing' and j.job_type = 'generation' and j.claimed_by = v_cred.worker_id
       and (
         (p_bucket = 'persona-private' and exists (
            select 1 from public.persona_assets pa
             where pa.persona_id = j.persona_id and pa.is_active and pa.storage_path = p_name
               and pa.id in (select private.job_input_refs(c.input_images, 'persona_asset_id'))))
         or
         (p_bucket = 'media' and exists (
            select 1 from public.assets a
             where a.persona_id = j.persona_id and a.storage_bucket = 'media' and a.storage_path = p_name
               and a.id in (select private.job_input_refs(c.input_images, 'asset_id'))))
       ));
end;
$$;

create or replace function private.worker_may_write_object(p_bucket text, p_name text)
returns boolean
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_cred public.worker_credentials := private.worker_identity();
  v_m    text[];
begin
  if v_cred.jti is null or p_bucket is distinct from 'media' or p_name is null then
    return false;
  end if;
  -- 정확히 persona/{uuid}/assets/{uuid}[_thumb].{png|jpg|webp}. 경로 조작·중첩·대문자를 허용하지 않는다.
  v_m := regexp_match(p_name,
    '^persona/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/assets/'
    '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(_thumb)?\.(png|jpg|webp)$');
  if v_m is null then
    return false;
  end if;
  return exists (
           select 1 from public.automation_jobs j
            where j.status = 'processing' and j.job_type = 'generation' and j.claimed_by = v_cred.worker_id
              and j.persona_id = v_m[1]::uuid)
     and not exists (select 1 from public.assets a where a.storage_path = p_name);   -- 등록된 Asset은 쓸 수 없다
end;
$$;

revoke all on function private.worker_may_read_object(text, text) from public, anon, authenticated;
revoke all on function private.worker_may_write_object(text, text) from public, anon, authenticated;
grant execute on function private.worker_may_read_object(text, text) to pa_worker;
grant execute on function private.worker_may_write_object(text, text) to pa_worker;

-- storage 스키마는 Supabase에 이미 있다 (로컬 테스트에서는 스텁이 만든다).
-- SELECT·INSERT만 준다. UPDATE·DELETE는 주지 않는다 (덮어쓰기·삭제 불가). 행은 아래 정책이 거른다.
do $$
begin
  if to_regclass('storage.objects') is not null then
    execute 'grant usage on schema storage to pa_worker';
    execute 'grant select, insert on storage.objects to pa_worker';
    execute 'drop policy if exists pa_worker_read_inputs on storage.objects';
    execute 'drop policy if exists pa_worker_write_results on storage.objects';
    execute 'create policy pa_worker_read_inputs on storage.objects for select to pa_worker '
            'using (private.worker_may_read_object(bucket_id, name))';
    execute 'create policy pa_worker_write_results on storage.objects for insert to pa_worker '
            'with check (private.worker_may_write_object(bucket_id, name))';
  end if;
end
$$;

-- -----------------------------------------------------------------------------
-- 9. 실행 권한
--    pa_worker는 아래 RPC만 실행할 수 있다. public·private 스키마의 테이블 직접 접근은 없다 (12조).
--    (storage.objects의 SELECT·INSERT는 8절 정책이 행 단위로 거른다.)
--    기본 권한 설정에 기대지 않도록 새 함수의 공개 범위를 명시적으로 닫는다.
-- -----------------------------------------------------------------------------
revoke all on function
  public.worker_get_job_context(uuid),
  public.worker_get_input_asset(uuid, uuid)
from public, anon, authenticated;

revoke all on function
  public.issue_worker_credential(uuid, text, text, timestamptz, text),
  public.list_worker_credentials(),
  public.revoke_worker_credential(uuid),
  public.move_pending_jobs_to_target(text),
  public.retry_automation_job(uuid, text),
  public.create_content_job(uuid, text, text, text, text, text, jsonb, jsonb, integer, integer, text, boolean, text)
from public, anon;

grant execute on function
  public.claim_next_automation_job(text, text),
  public.claim_automation_job(uuid, text),
  public.heartbeat_automation_job(uuid, timestamptz),
  public.complete_automation_job(uuid, timestamptz, jsonb),
  public.fail_automation_job(uuid, timestamptz, text, text, text, boolean, integer, text),
  public.register_asset(uuid, timestamptz, jsonb),
  public.log_execution(uuid, text, text, text, jsonb, jsonb, bigint, text, text),
  public.report_worker_status(text, text, jsonb),
  public.log_security_event(text, text, text, jsonb, uuid),
  public.worker_get_job_context(uuid),
  public.worker_get_input_asset(uuid, uuid),
  public.revoke_worker_credential(uuid)
to pa_worker;

-- 로컬 Phase 0 경로(service_role)와 운영자 화면도 계속 동작해야 한다.
grant execute on function
  public.worker_get_job_context(uuid),
  public.worker_get_input_asset(uuid, uuid)
to service_role;

grant execute on function
  public.issue_worker_credential(uuid, text, text, timestamptz, text),
  public.revoke_worker_credential(uuid),
  public.list_worker_credentials()
to authenticated;
