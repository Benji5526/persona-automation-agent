-- =============================================================================
-- 0005 Security (TECH_DESIGN 15.3 ~ 15.5, 12.3)
--   원칙: 기본은 모두 닫고(Fail Closed), 필요한 것만 연다.
--   * anon: 아무 권한 없음
--   * authenticated: 12.3 표의 권한만. status·role 칸은 직접 수정 불가 (칸 단위 권한)
--   * service_role: Worker RPC 실행 (RLS 우회)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. 기본 권한 회수 (Supabase는 public 스키마의 새 객체를 모든 역할에 공개한다)
-- -----------------------------------------------------------------------------
revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke execute on all functions in schema public from public, anon, authenticated;

alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke execute on functions from anon, authenticated;
-- PUBLIC EXECUTE는 전역 기본값이라 스키마 단위(in schema)로는 회수되지 않는다. 전역으로 회수한다.
-- (이후 V1·V2 마이그레이션에서 만드는 함수도 기본으로 닫힘)
alter default privileges for role postgres revoke execute on functions from public;

revoke all on schema private from public, anon, authenticated;
revoke execute on all functions in schema private from public, anon, authenticated;

grant all on all tables    in schema public to service_role;
grant all on all sequences in schema public to service_role;

-- -----------------------------------------------------------------------------
-- 2. RLS: public 스키마의 모든 테이블
-- -----------------------------------------------------------------------------
alter table public.app_settings      enable row level security;
alter table public.users             enable row level security;
alter table public.personas          enable row level security;
alter table public.persona_assets    enable row level security;
alter table public.content_jobs      enable row level security;
alter table public.social_accounts   enable row level security;
alter table public.automation_jobs   enable row level security;
alter table public.assets            enable row level security;
alter table public.posts             enable row level security;
alter table public.execution_logs    enable row level security;
alter table public.system_errors     enable row level security;
alter table public.state_transitions enable row level security;
alter table public.comfy_workflows   enable row level security;
alter table public.security_events   enable row level security;
-- app_settings: Operator 정책 없음 (가입 허용 목록을 볼 수 없다). 필요한 값은 RPC로만 노출.

-- -----------------------------------------------------------------------------
-- 3. 테이블별 권한과 정책 (12.3)
--    auth.uid()는 (select auth.uid())로 감싸 행마다 다시 계산하지 않게 한다 (15.4)
-- -----------------------------------------------------------------------------

-- users: 자기 행 읽기, 표시 이름·아바타만 수정 (role 수정 불가)
grant select on public.users to authenticated;
grant update (display_name, avatar_url) on public.users to authenticated;
create policy users_select_own on public.users
  for select to authenticated using (id = (select auth.uid()));
create policy users_update_own on public.users
  for update to authenticated using (id = (select auth.uid())) with check (id = (select auth.uid()));

-- personas: 자기 것 전체 (user_id는 바꿀 수 없음)
grant select, delete on public.personas to authenticated;
grant insert (name, slug, profile_image_path, description, personality, speaking_style, interests,
              background, content_rules, interaction_rules, safety_rules, visual_settings, status)
  on public.personas to authenticated;
grant update (name, slug, profile_image_path, description, personality, speaking_style, interests,
              background, content_rules, interaction_rules, safety_rules, visual_settings, status)
  on public.personas to authenticated;
create policy personas_select_own on public.personas
  for select to authenticated using (user_id = (select auth.uid()));
create policy personas_insert_own on public.personas
  for insert to authenticated with check (user_id = (select auth.uid()));
create policy personas_update_own on public.personas
  for update to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
create policy personas_delete_own on public.personas
  for delete to authenticated using (user_id = (select auth.uid()));

-- persona_assets: 소유한 Persona의 것
grant select, delete on public.persona_assets to authenticated;
grant insert (persona_id, asset_type, name, storage_path, metadata, is_active) on public.persona_assets to authenticated;
grant update (asset_type, name, storage_path, metadata, is_active) on public.persona_assets to authenticated;
create policy persona_assets_owner on public.persona_assets
  for all to authenticated
  using (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())))
  with check (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));

-- content_jobs: 읽기, draft일 때만 직접 생성·수정. status는 RPC로만.
grant select on public.content_jobs to authenticated;
grant insert (persona_id, content_type, topic, prompt, negative_prompt, workflow, params, input_images,
              variants, platform, priority, scheduled_at, metadata)
  on public.content_jobs to authenticated;
grant update (topic, prompt, negative_prompt, workflow, params, input_images,
              variants, platform, priority, scheduled_at, metadata)
  on public.content_jobs to authenticated;
create policy content_jobs_select_own on public.content_jobs
  for select to authenticated
  using (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));
create policy content_jobs_insert_draft on public.content_jobs
  for insert to authenticated
  with check (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid()))
              and status = 'draft' and source = 'operator' and created_by = (select auth.uid()));
create policy content_jobs_update_draft on public.content_jobs
  for update to authenticated
  using (status = 'draft'
         and persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())))
  with check (status = 'draft'
              and persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));

-- 읽기 전용 테이블: 소유한 Persona의 것만
grant select on public.assets, public.automation_jobs, public.system_errors,
                public.state_transitions, public.social_accounts, public.posts
  to authenticated;

create policy assets_select_own on public.assets
  for select to authenticated
  using (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));
create policy automation_jobs_select_own on public.automation_jobs
  for select to authenticated
  using (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));
create policy system_errors_select_own on public.system_errors
  for select to authenticated
  using (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));
create policy state_transitions_select_own on public.state_transitions
  for select to authenticated
  using (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));
-- social_accounts: Vault secret id 칸은 uuid일 뿐이고 Vault는 service_role만 읽을 수 있어 노출돼도 무해하다
create policy social_accounts_select_own on public.social_accounts
  for select to authenticated
  using (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));

-- execution_logs: 소유한 Persona의 Job 기록
grant select on public.execution_logs to authenticated;
create policy execution_logs_select_own on public.execution_logs
  for select to authenticated
  using (automation_job_id in (
    select j.id from public.automation_jobs j
      join public.personas p on p.id = j.persona_id
     where p.user_id = (select auth.uid())));

-- posts: 읽기 + draft·rejected일 때 캡션·해시태그만 수정
grant update (caption, hashtags, is_sponsored) on public.posts to authenticated;
create policy posts_select_own on public.posts
  for select to authenticated
  using (persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));
create policy posts_update_draft on public.posts
  for update to authenticated
  using (status in ('draft', 'rejected')
         and persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())))
  with check (status in ('draft', 'rejected')
              and persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));

-- comfy_workflows: 로그인한 Operator는 모두 읽기 (Workflow 선택 목록)
grant select on public.comfy_workflows to authenticated;
create policy comfy_workflows_select on public.comfy_workflows
  for select to authenticated using (true);

-- security_events: 자기 이벤트와 자기 Persona 관련 이벤트 읽기
grant select on public.security_events to authenticated;
create policy security_events_select_own on public.security_events
  for select to authenticated
  using (actor_id = (select auth.uid())
         or persona_id in (select p.id from public.personas p where p.user_id = (select auth.uid())));

-- -----------------------------------------------------------------------------
-- 4. 함수 실행 권한
-- -----------------------------------------------------------------------------
grant execute on function
  public.create_content_job(uuid, text, text, text, text, text, jsonb, jsonb, integer, integer, text, boolean),
  public.submit_content_job(uuid),
  public.cancel_content_job(uuid, text),
  public.retry_content_job(uuid),
  public.regenerate_content_job(uuid, integer),
  public.retry_automation_job(uuid),
  public.archive_asset(uuid),
  public.get_dashboard_summary(uuid)
to authenticated;

grant execute on function
  public.claim_content_job(uuid),
  public.create_automation_job(text, uuid, uuid, uuid, text, jsonb, integer, integer, text),
  public.claim_automation_job(uuid, text),
  public.claim_next_automation_job(text, text),
  public.heartbeat_automation_job(uuid, timestamptz),
  public.complete_automation_job(uuid, timestamptz, jsonb),
  public.fail_automation_job(uuid, timestamptz, text, text, text, boolean, integer, text),
  public.log_execution(uuid, text, text, text, jsonb, jsonb, bigint, text, text),
  public.save_prompt_parts(uuid, timestamptz, jsonb, text[]),
  public.register_asset(uuid, timestamptz, jsonb),
  public.create_post_draft(uuid, timestamptz, uuid, text, text, text[]),
  public.sync_workflow_registry(jsonb),
  public.log_security_event(text, text, text, jsonb, uuid),
  public.recover_stale_jobs()
to service_role;

-- -----------------------------------------------------------------------------
-- 5. 가입 허용 목록 (15.3)
--   허용 목록에 없는 이메일이면 가입 자체를 거부한다 (auth.users INSERT가 롤백됨).
--   거부 기록은 Supabase Auth 로그에 남는다.
-- -----------------------------------------------------------------------------
create or replace function private.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_email text := lower(btrim(new.email));
begin
  if v_email is null or not exists (
       select 1 from public.app_settings s
        where s.key = 'allowed_emails' and s.value ? v_email) then
    raise exception using errcode = 'P0001', message = 'SIGNUP_NOT_ALLOWED',
      detail = 'this email is not on the operator allow list';
  end if;

  insert into public.users (id, email, display_name, avatar_url)
  values (new.id, v_email,
          coalesce(new.raw_user_meta_data ->> 'full_name', new.raw_user_meta_data ->> 'name'),
          new.raw_user_meta_data ->> 'avatar_url');
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function private.handle_new_user();

-- -----------------------------------------------------------------------------
-- 6. Storage (15.5)
--   media: 공개 (생성 결과물). 목록 조회 정책 없음, 쓰기는 service_role만.
--   persona-private: 비공개 (참조 이미지). 소유 Operator만 읽기·쓰기.
-- -----------------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types) values
  ('media', 'media', true, 52428800,
   array['image/png', 'image/jpeg', 'image/webp', 'video/mp4']),
  ('persona-private', 'persona-private', false, 52428800,
   array['image/png', 'image/jpeg', 'image/webp'])
on conflict (id) do update
  set public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

create policy persona_private_select on storage.objects
  for select to authenticated
  using (bucket_id = 'persona-private'
         and (storage.foldername(name))[1] = 'persona'
         and (storage.foldername(name))[3] = 'refs'
         and (storage.foldername(name))[2] in (
           select p.id::text from public.personas p where p.user_id = (select auth.uid())));
create policy persona_private_insert on storage.objects
  for insert to authenticated
  with check (bucket_id = 'persona-private'
              and (storage.foldername(name))[1] = 'persona'
              and (storage.foldername(name))[3] = 'refs'
              and (storage.foldername(name))[2] in (
                select p.id::text from public.personas p where p.user_id = (select auth.uid())));
create policy persona_private_update on storage.objects
  for update to authenticated
  using (bucket_id = 'persona-private'
         and (storage.foldername(name))[1] = 'persona'
         and (storage.foldername(name))[3] = 'refs'
         and (storage.foldername(name))[2] in (
           select p.id::text from public.personas p where p.user_id = (select auth.uid())));
create policy persona_private_delete on storage.objects
  for delete to authenticated
  using (bucket_id = 'persona-private'
         and (storage.foldername(name))[1] = 'persona'
         and (storage.foldername(name))[3] = 'refs'
         and (storage.foldername(name))[2] in (
           select p.id::text from public.personas p where p.user_id = (select auth.uid())));
