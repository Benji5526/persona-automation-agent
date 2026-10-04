-- =============================================================================
-- 0002 State Machine (TECH_DESIGN 11)
--   * 허용된 전환만 통과시키는 BEFORE 트리거 (11.12)
--   * 다른 행을 봐야 하는 Guard (11.3 ~ 11.8)
--   * 상위·하위 상태 연동 Rollup (11.9)
--   * 모든 상태 변경 기록 state_transitions (11.14)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 허용 목록
-- -----------------------------------------------------------------------------
create table private.allowed_transitions (
  entity_type text not null,
  from_status text,          -- null = INSERT 시 초기 상태
  to_status   text not null,
  unique nulls not distinct (entity_type, from_status, to_status)
);

insert into private.allowed_transitions (entity_type, from_status, to_status) values
  -- content_job (11.3)
  ('content_job', null,         'draft'),
  ('content_job', null,         'queued'),
  ('content_job', 'draft',      'queued'),
  ('content_job', 'draft',      'cancelled'),
  ('content_job', 'queued',     'generating'),
  ('content_job', 'queued',     'cancelled'),
  ('content_job', 'generating', 'ready'),
  ('content_job', 'generating', 'failed'),
  ('content_job', 'generating', 'cancelled'),
  ('content_job', 'ready',      'queued'),
  ('content_job', 'ready',      'published'),
  ('content_job', 'ready',      'cancelled'),
  ('content_job', 'failed',     'queued'),
  ('content_job', 'failed',     'generating'),  -- 실패한 단계만 다시 실행 (retry_automation_job)
  ('content_job', 'failed',     'cancelled'),
  -- automation_job (11.4)
  ('automation_job', null,         'pending'),
  ('automation_job', 'pending',    'processing'),
  ('automation_job', 'pending',    'cancelled'),
  ('automation_job', 'processing', 'done'),
  ('automation_job', 'processing', 'pending'),
  ('automation_job', 'processing', 'failed'),
  ('automation_job', 'processing', 'cancelled'),
  ('automation_job', 'failed',     'pending'),
  -- asset (11.7)
  ('asset', null,        'generated'),
  ('asset', 'generated', 'approved'),
  ('asset', 'generated', 'rejected'),
  ('asset', 'generated', 'archived'),
  ('asset', 'approved',  'rejected'),
  ('asset', 'approved',  'archived'),
  ('asset', 'rejected',  'approved'),
  ('asset', 'rejected',  'archived'),
  -- post (11.8)
  ('post', null,               'draft'),
  ('post', 'draft',            'pending_approval'),
  ('post', 'pending_approval', 'approved'),
  ('post', 'pending_approval', 'rejected'),
  ('post', 'pending_approval', 'draft'),
  ('post', 'rejected',         'draft'),
  ('post', 'approved',         'scheduled'),
  ('post', 'approved',         'publishing'),
  ('post', 'approved',         'pending_approval'),
  ('post', 'scheduled',        'publishing'),
  ('post', 'scheduled',        'pending_approval'),
  ('post', 'publishing',       'published'),
  ('post', 'publishing',       'failed'),
  ('post', 'failed',           'scheduled'),
  ('post', 'failed',           'publishing'),
  ('post', 'draft',            'cancelled'),
  ('post', 'pending_approval', 'cancelled'),
  ('post', 'approved',         'cancelled'),
  ('post', 'scheduled',        'cancelled'),
  ('post', 'publishing',       'cancelled'),
  ('post', 'failed',           'cancelled'),
  ('post', 'rejected',         'cancelled');

-- -----------------------------------------------------------------------------
-- 행위자 판별 (11.14)
--   app.actor_override (Rollup 등 내부 처리) → Operator(auth.uid) → x-actor 헤더 → system
-- -----------------------------------------------------------------------------
create or replace function private.current_actor_type()
returns text
language plpgsql
stable
set search_path = ''
as $$
declare
  v_override text := nullif(current_setting('app.actor_override', true), '');
  v_header   text;
begin
  if v_override is not null then
    return v_override;
  end if;
  if auth.uid() is not null then
    return 'operator';
  end if;
  begin
    v_header := nullif(current_setting('request.headers', true), '')::json ->> 'x-actor';
  exception when others then
    v_header := null;
  end;
  if v_header in ('n8n', 'python') then
    return v_header;
  end if;
  return 'system';
end;
$$;

create or replace function private.current_actor_id()
returns uuid
language sql
stable
set search_path = ''
as $$
  select case when nullif(current_setting('app.actor_override', true), '') is null then auth.uid() end
$$;

-- -----------------------------------------------------------------------------
-- 오류 발생 도우미: PostgREST가 SQLSTATE PTxxx를 HTTP xxx로 바꿔 준다 (12.2)
-- -----------------------------------------------------------------------------
create or replace function private.raise_api_error(p_code text, p_detail text)
returns void
language plpgsql
set search_path = ''
as $$
declare
  v_state text := case p_code
    when 'NOT_FOUND' then 'PT404'
    when 'FORBIDDEN' then 'PT403'
    when 'INVALID_TRANSITION' then 'PT409'
    when 'VALIDATION_FAILED' then 'PT422'
    when 'RATE_LIMITED' then 'PT429'
    else 'PT500'
  end;
begin
  raise exception using errcode = v_state, message = p_code, detail = p_detail;
end;
$$;

-- -----------------------------------------------------------------------------
-- 전환 강제 + Guard (BEFORE INSERT OR UPDATE OF status)
--   TG_ARGV[0] = entity_type
-- -----------------------------------------------------------------------------
create or replace function private.enforce_status_transition()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_entity text := tg_argv[0];
  v_from   text := case when tg_op = 'UPDATE' then old.status end;
begin
  if tg_op = 'UPDATE' and new.status is not distinct from old.status then
    return new;
  end if;

  if not exists (
    select 1 from private.allowed_transitions t
    where t.entity_type = v_entity
      and t.from_status is not distinct from v_from
      and t.to_status = new.status
  ) then
    perform private.raise_api_error('INVALID_TRANSITION',
      format('%s %s: %s → %s is not allowed', v_entity, new.id, coalesce(v_from, '(new)'), new.status));
  end if;

  -- 다른 행을 봐야 하는 Guard -------------------------------------------------
  if v_entity = 'content_job' then
    if new.status = 'queued' and coalesce(btrim(new.topic), '') = '' and coalesce(btrim(new.prompt), '') = '' then
      perform private.raise_api_error('VALIDATION_FAILED', 'topic or prompt is required');
    end if;
    if new.status = 'ready' then
      if not exists (select 1 from public.assets a
                     where a.content_job_id = new.id and a.status in ('generated', 'approved')) then
        perform private.raise_api_error('VALIDATION_FAILED', 'ready requires at least one valid asset');
      end if;
      new.completed_at := now();
    end if;
    if new.status = 'published' and not exists (
      select 1 from public.posts p join public.assets a on a.id = p.asset_id
      where a.content_job_id = new.id and p.status = 'published') then
      perform private.raise_api_error('VALIDATION_FAILED', 'published requires a published post');
    end if;
    if v_from = 'ready' and new.status = 'cancelled' and exists (
      select 1 from public.posts p join public.assets a on a.id = p.asset_id
      where a.content_job_id = new.id and p.status = 'published') then
      perform private.raise_api_error('INVALID_TRANSITION', 'content job has a published post');
    end if;

  elsif v_entity = 'automation_job' then
    if new.status in ('done', 'failed', 'cancelled') then
      new.completed_at := coalesce(new.completed_at, now());
    end if;

  elsif v_entity = 'asset' then
    if new.status = 'archived' and exists (
      select 1 from public.posts p where p.asset_id = new.id and p.status in ('scheduled', 'publishing')) then
      perform private.raise_api_error('INVALID_TRANSITION', 'asset has a scheduled or publishing post');
    end if;
    if v_from = 'approved' and new.status = 'rejected' and exists (
      select 1 from public.posts p where p.asset_id = new.id and p.status in ('scheduled', 'publishing', 'published')) then
      perform private.raise_api_error('INVALID_TRANSITION', 'asset is scheduled or already published');
    end if;

  elsif v_entity = 'post' then
    if new.status = 'pending_approval' and (new.caption is null or new.social_account_id is null) then
      perform private.raise_api_error('VALIDATION_FAILED', 'caption and social_account_id are required');
    end if;
    if new.status = 'published' then
      new.published_at := coalesce(new.published_at, now());
    end if;
  end if;

  return new;
end;
$$;

create trigger content_jobs_enforce_transition
  before insert or update of status on public.content_jobs
  for each row execute function private.enforce_status_transition('content_job');
create trigger automation_jobs_enforce_transition
  before insert or update of status on public.automation_jobs
  for each row execute function private.enforce_status_transition('automation_job');
create trigger assets_enforce_transition
  before insert or update of status on public.assets
  for each row execute function private.enforce_status_transition('asset');
create trigger posts_enforce_transition
  before insert or update of status on public.posts
  for each row execute function private.enforce_status_transition('post');

-- -----------------------------------------------------------------------------
-- 상태 변경 기록 (AFTER INSERT OR UPDATE OF status)
-- -----------------------------------------------------------------------------
create or replace function private.log_state_transition()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_metadata jsonb := '{}'::jsonb;
begin
  if tg_op = 'UPDATE' and new.status is not distinct from old.status then
    return null;
  end if;

  -- 칸 참조는 해당 테이블일 때만 실행되도록 IF 안에 둔다 (SQL CASE 안에 두면 모든 테이블에서 칸을 찾는다)
  if tg_argv[0] = 'automation_job' then
    v_metadata := jsonb_strip_nulls(jsonb_build_object(
      'job_type', new.job_type, 'attempts', new.attempts,
      'error_type', new.error_type, 'error_code', new.error_code));
  end if;

  insert into public.state_transitions
    (entity_type, entity_id, persona_id, from_status, to_status, actor_type, actor_id, reason, metadata)
  values (
    tg_argv[0],
    new.id,
    new.persona_id,
    case when tg_op = 'UPDATE' then old.status end,
    new.status,
    private.current_actor_type(),
    private.current_actor_id(),
    nullif(current_setting('app.transition_reason', true), ''),
    v_metadata
  );
  return null;
end;
$$;

create trigger content_jobs_log_transition
  after insert or update of status on public.content_jobs
  for each row execute function private.log_state_transition('content_job');
create trigger automation_jobs_log_transition
  after insert or update of status on public.automation_jobs
  for each row execute function private.log_state_transition('automation_job');
create trigger assets_log_transition
  after insert or update of status on public.assets
  for each row execute function private.log_state_transition('asset');
create trigger posts_log_transition
  after insert or update of status on public.posts
  for each row execute function private.log_state_transition('post');

-- -----------------------------------------------------------------------------
-- Rollup (11.9). 내부 처리이므로 행위자는 system으로 기록한다.
-- -----------------------------------------------------------------------------
-- 이전 값을 jsonb로 돌려주고, end에서 그대로 복원한다 (중첩 호출에서도 안전)
create or replace function private.begin_system_change(p_reason text)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  v_prev jsonb := jsonb_build_object(
    'reason', coalesce(current_setting('app.transition_reason', true), ''),
    'actor',  coalesce(current_setting('app.actor_override', true), ''));
begin
  perform set_config('app.actor_override', 'system', true);
  perform set_config('app.transition_reason', p_reason, true);
  return v_prev;
end;
$$;

create or replace function private.end_system_change(p_prev jsonb)
returns void
language plpgsql
set search_path = ''
as $$
begin
  perform set_config('app.actor_override', coalesce(p_prev ->> 'actor', ''), true);
  perform set_config('app.transition_reason', coalesce(p_prev ->> 'reason', ''), true);
end;
$$;

-- R1, R2, R8
create or replace function private.rollup_automation_job()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_prev jsonb;
begin
  if new.content_job_id is not null and new.job_type in ('prompt', 'generation') then
    -- R1: 생성 Job 완료 → 다른 생성 Job이 남아 있지 않고 유효 Asset이 있으면 Content Job ready
    if new.job_type = 'generation' and new.status = 'done'
       and not exists (select 1 from public.automation_jobs j
                       where j.content_job_id = new.content_job_id and j.job_type = 'generation'
                         and j.status in ('pending', 'processing') and j.id <> new.id)
       and exists (select 1 from public.assets a
                   where a.content_job_id = new.content_job_id and a.status in ('generated', 'approved')) then
      v_prev := private.begin_system_change('rollup:generation_done');
      update public.content_jobs set status = 'ready'
       where id = new.content_job_id and status = 'generating';
      perform private.end_system_change(v_prev);
    end if;

    -- R2: prompt·generation 최종 실패 → Content Job failed
    if new.status = 'failed' then
      v_prev := private.begin_system_change('rollup:' || new.job_type || '_failed');
      update public.content_jobs set status = 'failed'
       where id = new.content_job_id and status = 'generating';
      perform private.end_system_change(v_prev);
    end if;
  end if;

  -- R8: 게시 Job 최종 실패 → Post failed
  if new.post_id is not null and new.job_type = 'publish' and new.status = 'failed' then
    v_prev := private.begin_system_change('rollup:publish_failed');
    update public.posts set status = 'failed', error = new.error_message
     where id = new.post_id and status = 'publishing';
    perform private.end_system_change(v_prev);
  end if;

  return null;
end;
$$;

create trigger automation_jobs_rollup
  after update of status on public.automation_jobs
  for each row when (old.status is distinct from new.status)
  execute function private.rollup_automation_job();

-- R5 (Content Job 취소 → 진행 중인 Automation Job 취소)
create or replace function private.rollup_content_job()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_prev jsonb;
begin
  if new.status = 'cancelled' then
    v_prev := private.begin_system_change('rollup:content_job_cancelled');
    update public.automation_jobs set status = 'cancelled'
     where content_job_id = new.id and status in ('pending', 'processing');
    perform private.end_system_change(v_prev);
  end if;
  return null;
end;
$$;

create trigger content_jobs_rollup
  after update of status on public.content_jobs
  for each row when (old.status is distinct from new.status)
  execute function private.rollup_content_job();

-- R4 (Post 게시 → Content Job published), R5 (Post 취소 → Post Job 취소)
create or replace function private.rollup_post()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_prev jsonb;
begin
  if new.status = 'published' then
    v_prev := private.begin_system_change('rollup:post_published');
    update public.content_jobs c set status = 'published'
      from public.assets a
     where a.id = new.asset_id and c.id = a.content_job_id and c.status = 'ready';
    perform private.end_system_change(v_prev);
  elsif new.status = 'cancelled' then
    v_prev := private.begin_system_change('rollup:post_cancelled');
    update public.automation_jobs set status = 'cancelled'
     where post_id = new.id and status in ('pending', 'processing');
    perform private.end_system_change(v_prev);
  end if;
  return null;
end;
$$;

create trigger posts_rollup
  after update of status on public.posts
  for each row when (old.status is distinct from new.status)
  execute function private.rollup_post();
