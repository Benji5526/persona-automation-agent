-- =============================================================================
-- 0015 Post 승인 흐름: approvals + Post 초안·제출·승인·반려·수정·취소 RPC
--   TECH_DESIGN 11.8~11.10, 12.4(V1 RPC 표), 28.7, 33.7, 51.2. MVP 완료 목록 11번 (MVP_SCOPE_LOCK 3.7).
--
--   이 파일은 "게시 전" 단계까지만 다룬다: Asset → Post 초안 → 제출 → 승인·반려.
--   예약·즉시 게시·게시 전 검사(check_publish_ready)·mark_post_publishing·complete_publish는 다음 마이그레이션이다.
--
--   * Post 초안은 원래 WF-005(LLM 캡션)가 만든다(create_post_draft, 0004). 이 PC의 로컬 흐름에는 LLM 캡션 경로가 없어서
--     운영자가 캡션을 직접 쓰는 `create_post_from_asset`을 더한다 (설계 추가: TECH_DESIGN 28.7에 기록).
--   * 승인 없는 게시는 구조적으로 불가능하다: pending_approval → approved 전이는 "그 Post의 approvals 행이 approved"일 때만
--     통과한다(아래 Guard). service_role로 상태만 바꿔도 마찬가지다.
--   * 쓰기는 RPC로만 한다 (CLAUDE.md 7). authenticated는 approvals를 읽기만 한다. LLM·워커는 이 RPC를 부르지 못한다.
--   * 만료(expire_approvals, 11.9 R7)와 예약 시각 지남 처리는 예약 게시와 함께 한다. 그때까지 pending은 그대로 남는다
--     (시간이 지나도 자동 실행하지 않는다, 33.7).
--   * cancel_post는 `publishing` 중에는 거부한다. 외부 SNS 호출이 이미 나갔을 수 있어서, 취소하면 게시물만 남고 기록이
--     끊긴다. 결과(published·failed)가 나온 뒤에 취소한다. 전이표(publishing → cancelled)는 그대로 둔다.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. approvals (10.18, 33.7): 지금은 게시 승인(publish)만. 다른 유형은 그 대상이 생길 때 더한다
-- -----------------------------------------------------------------------------
create table public.approvals (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.users (id) on delete restrict,        -- 승인자 = Persona 소유 Operator
  persona_id    uuid not null references public.personas (id) on delete restrict,
  approval_type text not null default 'publish' check (approval_type in ('publish')),
  post_id       uuid not null references public.posts (id) on delete restrict,
  status        text not null default 'pending'
                check (status in ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
  comment       text check (comment is null or char_length(comment) <= 500),
  created_at    timestamptz not null default now(),
  resolved_at   timestamptz,
  constraint approvals_resolved_has_time check ((status = 'pending') = (resolved_at is null))
);

create index approvals_persona_id_idx on public.approvals (persona_id, created_at desc);
create index approvals_post_id_idx on public.approvals (post_id);
-- Post마다 대기 중인 승인은 하나 (다시 승인받으려면 새 행, 11.10)
create unique index approvals_one_pending_per_post on public.approvals (post_id) where status = 'pending';

alter table public.approvals enable row level security;
grant select on public.approvals to authenticated;
grant all on public.approvals to service_role;
create policy approvals_select_own on public.approvals
  for select to authenticated
  using (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));

-- Persona 격리 (36.12): 승인은 그 Post와 같은 Persona이고, 승인자는 그 Persona의 소유자다
create or replace function private.enforce_approval_match()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not exists (select 1 from public.posts p where p.id = new.post_id and p.persona_id = new.persona_id) then
    perform private.raise_api_error('VALIDATION_FAILED', 'post belongs to a different persona');
  end if;
  if not exists (select 1 from public.personas p where p.id = new.persona_id and p.user_id = new.user_id) then
    perform private.raise_api_error('VALIDATION_FAILED', 'approver must own the persona');
  end if;
  return new;
end;
$$;

create trigger approvals_match
  before insert or update of persona_id, post_id, user_id on public.approvals
  for each row execute function private.enforce_approval_match();

-- 승인 화면이 실시간으로 갱신되도록 (28.12). 읽기는 RLS가 그대로 적용된다
alter publication supabase_realtime add table public.approvals;

-- -----------------------------------------------------------------------------
-- 2. 승인 상태 전이 (11.10)와 기록. 전이표·Guard·기록은 다른 객체와 같은 장치를 쓴다
-- -----------------------------------------------------------------------------
insert into private.allowed_transitions (entity_type, from_status, to_status) values
  ('approval', null,      'pending'),
  ('approval', 'pending', 'approved'),
  ('approval', 'pending', 'rejected'),
  ('approval', 'pending', 'expired'),
  ('approval', 'pending', 'cancelled');

create trigger approvals_enforce_transition
  before insert or update of status on public.approvals
  for each row execute function private.enforce_status_transition('approval');
create trigger approvals_log_transition
  after insert or update of status on public.approvals
  for each row execute function private.log_state_transition('approval');

-- -----------------------------------------------------------------------------
-- 3. Guard: 승인·반려는 그 Post의 approvals 행이 있어야 한다 (11.8 "연결된 approvals.status")
--    0002의 enforce_status_transition에 post 가지 두 줄만 더했다. 나머지는 그대로다.
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
    -- 승인 없이 approved가 될 수 없다. pending_approval에서만 올 수 있고, 그 Post의 approvals 행이 approved여야 한다
    if v_from = 'pending_approval' and new.status = 'approved' and not exists (
      select 1 from public.approvals a where a.post_id = new.id and a.status = 'approved') then
      perform private.raise_api_error('INVALID_TRANSITION', 'post has no approved approval');
    end if;
    if v_from = 'pending_approval' and new.status = 'rejected' and not exists (
      select 1 from public.approvals a where a.post_id = new.id and a.status = 'rejected') then
      perform private.raise_api_error('INVALID_TRANSITION', 'post has no rejected approval');
    end if;
    if new.status = 'published' then
      new.published_at := coalesce(new.published_at, now());
    end if;
  end if;

  return new;
end;
$$;

-- -----------------------------------------------------------------------------
-- 4. Rollup
--    R6/R7: 승인 처리 → Post 반영 (approved → approved, rejected → rejected, expired → draft)
--    R5: Post 취소 → 진행 중인 Job 취소(0002에 있음) + 대기 중인 승인 취소(여기서 더함)
-- -----------------------------------------------------------------------------
create or replace function private.rollup_approval()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_prev jsonb;
begin
  v_prev := private.begin_system_change('rollup:approval_' || new.status);
  if new.status = 'approved' then
    update public.posts set status = 'approved' where id = new.post_id and status = 'pending_approval';
  elsif new.status = 'rejected' then
    update public.posts set status = 'rejected' where id = new.post_id and status = 'pending_approval';
  elsif new.status = 'expired' then
    update public.posts set status = 'draft' where id = new.post_id and status = 'pending_approval';
  end if;
  perform private.end_system_change(v_prev);
  return null;
end;
$$;

create trigger approvals_rollup
  after update of status on public.approvals
  for each row when (old.status is distinct from new.status)
  execute function private.rollup_approval();

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
    update public.approvals set status = 'cancelled', resolved_at = now()
     where post_id = new.id and status = 'pending';
    perform private.end_system_change(v_prev);
  end if;
  return null;
end;
$$;

-- -----------------------------------------------------------------------------
-- 5. 도우미
-- -----------------------------------------------------------------------------
-- 내 Persona의 Post만 잠그고 가져온다 (남의 것은 존재하지 않는 것처럼 NOT_FOUND)
create or replace function private.lock_owned_post(p_post_id uuid)
returns public.posts
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.posts;
begin
  select po.* into v
    from public.posts po
    join public.personas p on p.id = po.persona_id
   where po.id = p_post_id and p.user_id = private.require_operator()
     for update of po;
  if v.id is null then
    perform private.raise_api_error('NOT_FOUND', 'post not found');
  end if;
  return v;
end;
$$;

-- 해시태그: '#'를 떼고, 공백·'#'가 든 것은 거부하고, 빈 것과 중복은 버린다 (저장은 '#' 없이, 게시할 때 붙인다, 51.3)
create or replace function private.normalize_hashtags(p_tags text[])
returns text[]
language plpgsql
set search_path = ''
as $$
declare
  v_out text[] := '{}';
  v_tag text;
begin
  foreach v_tag in array coalesce(p_tags, '{}') loop
    v_tag := regexp_replace(btrim(v_tag), '^#+', '');
    if v_tag = '' then
      continue;
    end if;
    if v_tag !~ '^[^\s#]+$' or char_length(v_tag) > 100 then
      perform private.raise_api_error('VALIDATION_FAILED', format('invalid hashtag: %s', left(v_tag, 40)));
    end if;
    if not v_tag = any (v_out) then
      v_out := v_out || v_tag;
    end if;
  end loop;
  return v_out;
end;
$$;

-- 게시 가능한 Asset인가 (28.8 5번: archived·rejected가 아니면 된다, 50.4 Operator 결정)
create or replace function private.require_postable_asset(p_asset_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not exists (select 1 from public.assets a where a.id = p_asset_id and a.status in ('generated', 'approved')) then
    perform private.raise_api_error('VALIDATION_FAILED', 'asset is archived or rejected');
  end if;
end;
$$;

-- 제출·예약할 수 있는 계정인가: 같은 Persona, 같은 플랫폼, active
create or replace function private.require_usable_account(p_account_id uuid, p_persona_id uuid, p_platform text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not exists (select 1 from public.social_accounts s
                  where s.id = p_account_id and s.persona_id = p_persona_id
                    and s.platform = p_platform and s.status = 'active') then
    perform private.raise_api_error('VALIDATION_FAILED', 'social account is not an active account of this persona and platform');
  end if;
end;
$$;

-- -----------------------------------------------------------------------------
-- 6. create_post_from_asset: Asset에서 캡션 초안(draft) 만들기
--    계정은 제출할 때 정해도 된다. 플랫폼은 MVP에서 instagram, x (tiktok은 CHECK 값만 있고 지원하지 않는다, 41.5).
-- -----------------------------------------------------------------------------
create or replace function public.create_post_from_asset(
  p_asset_id          uuid,
  p_platform          text,
  p_caption           text,
  p_hashtags          text[]  default '{}',
  p_social_account_id uuid    default null,
  p_is_sponsored      boolean default false
)
returns public.posts
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid     uuid := private.require_operator();
  v_asset   public.assets;
  v_caption text := nullif(btrim(p_caption), '');
  v         public.posts;
begin
  if p_platform is null or p_platform not in ('instagram', 'x') then
    perform private.raise_api_error('VALIDATION_FAILED', 'platform must be instagram or x');
  end if;
  if v_caption is null then
    perform private.raise_api_error('VALIDATION_FAILED', 'caption is required');
  end if;

  select a.* into v_asset
    from public.assets a
    join public.personas p on p.id = a.persona_id
   where a.id = p_asset_id and p.user_id = v_uid
     for share of a;
  if v_asset.id is null then
    perform private.raise_api_error('NOT_FOUND', 'asset not found');
  end if;
  perform private.require_active_persona(v_asset.persona_id);
  perform private.require_postable_asset(v_asset.id);
  if p_social_account_id is not null then
    perform private.require_usable_account(p_social_account_id, v_asset.persona_id, p_platform);
  end if;

  perform private.set_reason('operator:create_post');
  insert into public.posts (persona_id, asset_id, social_account_id, platform, caption, hashtags, is_sponsored)
  values (v_asset.persona_id, v_asset.id, p_social_account_id, p_platform, v_caption,
          private.normalize_hashtags(p_hashtags), coalesce(p_is_sponsored, false))
  returning * into v;
  perform private.set_reason(null);
  return v;
exception
  when check_violation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- 7. submit_post_for_approval: draft → pending_approval, 승인 대기 행 만들기
-- -----------------------------------------------------------------------------
create or replace function public.submit_post_for_approval(
  p_post_id           uuid,
  p_social_account_id uuid default null
)
returns public.posts
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid     uuid := private.require_operator();
  v         public.posts := private.lock_owned_post(p_post_id);
  v_account uuid;
begin
  perform private.expect_status(v.status, array['draft'], 'post');
  perform private.require_active_persona(v.persona_id);
  perform private.require_postable_asset(v.asset_id);
  if coalesce(btrim(v.caption), '') = '' then
    perform private.raise_api_error('VALIDATION_FAILED', 'caption is required');
  end if;
  v_account := coalesce(p_social_account_id, v.social_account_id);
  if v_account is null then
    perform private.raise_api_error('VALIDATION_FAILED', 'social account is required');
  end if;
  perform private.require_usable_account(v_account, v.persona_id, v.platform);

  perform private.set_reason('operator:submit_post');
  update public.posts set social_account_id = v_account, status = 'pending_approval'
   where id = v.id returning * into v;
  insert into public.approvals (user_id, persona_id, post_id) values (v_uid, v.persona_id, v.id);
  perform private.set_reason(null);
  return v;
end;
$$;

-- -----------------------------------------------------------------------------
-- 8. resolve_approval: pending → approved·rejected (Post 반영은 approvals_rollup, R6)
-- -----------------------------------------------------------------------------
create or replace function public.resolve_approval(
  p_approval_id uuid,
  p_decision    text,
  p_comment     text default null
)
returns public.approvals
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid     uuid := private.require_operator();
  v         public.approvals;
  v_post    public.posts;
  v_comment text := nullif(btrim(p_comment), '');
begin
  if p_decision is null or p_decision not in ('approved', 'rejected') then
    perform private.raise_api_error('VALIDATION_FAILED', 'decision must be approved or rejected');
  end if;
  if v_comment is not null and char_length(v_comment) > 500 then
    perform private.raise_api_error('VALIDATION_FAILED', 'comment must be 500 characters or fewer');
  end if;

  select ap.* into v
    from public.approvals ap
    join public.personas p on p.id = ap.persona_id
   where ap.id = p_approval_id and p.user_id = v_uid
     for update of ap;
  if v.id is null then
    perform private.raise_api_error('NOT_FOUND', 'approval not found');
  end if;
  perform private.expect_status(v.status, array['pending'], 'approval');

  select po.* into v_post from public.posts po where po.id = v.post_id for update;
  perform private.expect_status(v_post.status, array['pending_approval'], 'post');
  if p_decision = 'approved' then
    perform private.require_active_persona(v.persona_id);
    perform private.require_postable_asset(v_post.asset_id);
  end if;

  perform private.set_reason(case p_decision when 'approved' then 'operator:approve_post' else 'operator:reject_post' end);
  update public.approvals
     set status = p_decision, comment = v_comment, resolved_at = now()
   where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
end;
$$;

-- -----------------------------------------------------------------------------
-- 9. revise_post: rejected → draft (선택: 캡션·해시태그를 함께 고친다). 다시 제출하면 새 승인 행이 생긴다
-- -----------------------------------------------------------------------------
create or replace function public.revise_post(
  p_post_id  uuid,
  p_caption  text   default null,
  p_hashtags text[] default null
)
returns public.posts
language plpgsql
security definer
set search_path = ''
as $$
declare
  v         public.posts := private.lock_owned_post(p_post_id);
  v_caption text := nullif(btrim(p_caption), '');
begin
  perform private.expect_status(v.status, array['rejected'], 'post');
  perform private.require_active_persona(v.persona_id);
  if p_caption is not null and v_caption is null then
    perform private.raise_api_error('VALIDATION_FAILED', 'caption must not be blank');
  end if;

  perform private.set_reason('operator:revise_post');
  update public.posts
     set status = 'draft',
         caption = coalesce(v_caption, caption),
         hashtags = case when p_hashtags is null then hashtags else private.normalize_hashtags(p_hashtags) end
   where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
exception
  when check_violation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- 10. cancel_post: published·cancelled·publishing을 뺀 모든 상태 → cancelled
--     진행 중인 Job과 대기 중인 승인은 post 롤업(R5)이 함께 취소한다
-- -----------------------------------------------------------------------------
create or replace function public.cancel_post(p_post_id uuid, p_reason text default null)
returns public.posts
language plpgsql
security definer
set search_path = ''
as $$
declare
  v public.posts := private.lock_owned_post(p_post_id);
begin
  if v.status = 'publishing' then
    perform private.raise_api_error('INVALID_TRANSITION',
      'post is being published; cancel after the result is known');
  end if;
  perform private.expect_status(
    v.status, array['draft', 'pending_approval', 'approved', 'scheduled', 'failed', 'rejected'], 'post');
  perform private.set_reason('operator:cancel_post' || coalesce(': ' || left(p_reason, 200), ''));
  update public.posts set status = 'cancelled' where id = v.id returning * into v;
  perform private.set_reason(null);
  return v;
end;
$$;

-- -----------------------------------------------------------------------------
-- 11. 권한: Lovable(authenticated)만. private 함수는 아무에게도 열지 않는다
-- -----------------------------------------------------------------------------
revoke all on function public.create_post_from_asset(uuid, text, text, text[], uuid, boolean) from public, anon;
revoke all on function public.submit_post_for_approval(uuid, uuid) from public, anon;
revoke all on function public.resolve_approval(uuid, text, text) from public, anon;
revoke all on function public.revise_post(uuid, text, text[]) from public, anon;
revoke all on function public.cancel_post(uuid, text) from public, anon;
grant execute on function public.create_post_from_asset(uuid, text, text, text[], uuid, boolean) to authenticated;
grant execute on function public.submit_post_for_approval(uuid, uuid) to authenticated;
grant execute on function public.resolve_approval(uuid, text, text) to authenticated;
grant execute on function public.revise_post(uuid, text, text[]) to authenticated;
grant execute on function public.cancel_post(uuid, text) to authenticated;

revoke all on function private.enforce_approval_match() from public, anon, authenticated;
revoke all on function private.rollup_approval() from public, anon, authenticated;
revoke all on function private.lock_owned_post(uuid) from public, anon, authenticated;
revoke all on function private.normalize_hashtags(text[]) from public, anon, authenticated;
revoke all on function private.require_postable_asset(uuid) from public, anon, authenticated;
revoke all on function private.require_usable_account(uuid, uuid, text) from public, anon, authenticated;
