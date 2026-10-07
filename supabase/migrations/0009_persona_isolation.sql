-- =============================================================================
-- 0009 persona_isolation (TECH_DESIGN 36.12 MVP 수정, 47.3 4번, 50.5 2번)
--
-- 첫 운영 적용(db push) 전에 넣는 보강이다. 적용한 파일(0001~0008)은 고치지 않는다.
--   1. automation_jobs: content_job·post의 persona가 Job의 persona와 같아야 한다 (트리거)
--   2. posts: asset·social_account의 persona가 Post의 persona와 같아야 한다 (트리거)
--      → service_role로 테이블에 직접 넣어도 다른 Persona의 데이터가 섞이지 않는다
--   3. 보관(inactive)한 Persona에서는 다시 시도·재생성·단계 재시도를 거부한다 (VALIDATION_FAILED)
--   4. Asset Library 조회용 index (persona_id, created_at desc)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. automation_jobs: Persona 일치
-- -----------------------------------------------------------------------------
create or replace function private.enforce_job_persona_match()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.content_job_id is not null and not exists (
       select 1 from public.content_jobs c where c.id = new.content_job_id and c.persona_id = new.persona_id) then
    perform private.raise_api_error('VALIDATION_FAILED', 'content job belongs to a different persona');
  end if;
  if new.post_id is not null and not exists (
       select 1 from public.posts p where p.id = new.post_id and p.persona_id = new.persona_id) then
    perform private.raise_api_error('VALIDATION_FAILED', 'post belongs to a different persona');
  end if;
  return new;
end;
$$;

create trigger automation_jobs_persona_match
  before insert or update of persona_id, content_job_id, post_id on public.automation_jobs
  for each row execute function private.enforce_job_persona_match();

-- -----------------------------------------------------------------------------
-- 2. posts: Persona 일치
-- -----------------------------------------------------------------------------
create or replace function private.enforce_post_persona_match()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not exists (select 1 from public.assets a where a.id = new.asset_id and a.persona_id = new.persona_id) then
    perform private.raise_api_error('VALIDATION_FAILED', 'asset belongs to a different persona');
  end if;
  if new.social_account_id is not null and not exists (
       select 1 from public.social_accounts s where s.id = new.social_account_id and s.persona_id = new.persona_id) then
    perform private.raise_api_error('VALIDATION_FAILED', 'social account belongs to a different persona');
  end if;
  return new;
end;
$$;

create trigger posts_persona_match
  before insert or update of persona_id, asset_id, social_account_id on public.posts
  for each row execute function private.enforce_post_persona_match();

-- -----------------------------------------------------------------------------
-- 3. 보관한 Persona의 다시 시도·재생성·단계 재시도 거부 (47.3 4번)
--    Worker RPC(claim_content_job, create_automation_job)는 바꾸지 않는다: 이미 대기 중인 일은 끝내고,
--    멈추려면 취소한다. Worker RPC를 막으면 Content Job이 generating에 걸린 채 복구가 되풀이된다.
-- -----------------------------------------------------------------------------
create or replace function private.require_active_persona(p_persona_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not exists (select 1 from public.personas p where p.id = p_persona_id and p.status = 'active') then
    perform private.raise_api_error('VALIDATION_FAILED', 'persona is not active');
  end if;
end;
$$;

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
  perform private.require_active_persona(v.persona_id);
  perform private.set_reason('operator:retry');
  update public.content_jobs
     set status = 'queued', run_number = run_number + 1, completed_at = null
   where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
end;
$$;

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
  perform private.require_active_persona(v.persona_id);
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

-- 함수 권한: create or replace는 기존 EXECUTE 권한을 유지한다. 새 private 함수는 노출하지 않는다.
revoke all on function private.enforce_job_persona_match() from public, anon, authenticated;
revoke all on function private.enforce_post_persona_match() from public, anon, authenticated;
revoke all on function private.require_active_persona(uuid) from public, anon, authenticated;

-- -----------------------------------------------------------------------------
-- 4. Asset Library 조회용 index (50.5 2번)
-- -----------------------------------------------------------------------------
create index if not exists assets_persona_created_idx on public.assets (persona_id, created_at desc);
