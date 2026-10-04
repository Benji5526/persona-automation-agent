-- =============================================================================
-- 0001 Core tables (TECH_DESIGN 10. Database / ERD, MVP 범위)
--   상태 값은 11. State Machine, 오류 분류는 6.9 / 13.12를 따른다.
--   V1·V2 테이블(performance_metrics, approvals, conversations, messages,
--   fan_memories, ai_decisions)은 이후 마이그레이션에서 추가한다.
-- =============================================================================

create schema if not exists private;

-- -----------------------------------------------------------------------------
-- 공통: updated_at 자동 갱신
-- -----------------------------------------------------------------------------
create or replace function private.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- -----------------------------------------------------------------------------
-- app_settings: 시스템 설정 (15.3 가입 허용 목록, 15.11 긴급 정지, 15.18 실행 한도)
-- -----------------------------------------------------------------------------
create table public.app_settings (
  key         text primary key,
  value       jsonb not null,
  description text,
  updated_at  timestamptz not null default now()
);

insert into public.app_settings (key, value, description) values
  ('allowed_emails', '[]'::jsonb,
   '가입을 허용할 Operator 이메일 목록 (소문자). 비어 있으면 아무도 가입할 수 없다'),
  ('publishing_enabled', 'false'::jsonb,
   '긴급 게시 정지 스위치 (V1). false면 어떤 게시도 실행하지 않는다'),
  ('limits', '{
     "max_content_jobs_per_hour": 30,
     "daily_generation_limit": 300,
     "daily_llm_calls_limit": 1000,
     "daily_publish_limit": 10
   }'::jsonb,
   '실행 한도 (15.18)'),
  ('retry_backoff_seconds', '[30, 120, 300, 900]'::jsonb,
   '재시도 대기 시간. n번째 실패 후 n번째 값, 끝을 넘으면 마지막 값 (14.11)'),
  ('heartbeat_timeout_seconds', '{
     "prompt": 120, "caption": 120, "generation": 180, "publish": 300, "analytics": 300
   }'::jsonb,
   'job_type별 Heartbeat 제한 시간 (11.6)');

create trigger app_settings_set_updated_at
  before update on public.app_settings
  for each row execute function private.set_updated_at();

-- -----------------------------------------------------------------------------
-- users (10.4): auth.users와 1:1. 가입 트리거는 0005에서 만든다.
-- -----------------------------------------------------------------------------
create table public.users (
  id           uuid primary key references auth.users (id) on delete cascade,
  email        text not null,
  display_name text,
  avatar_url   text,
  role         text not null default 'operator' check (role in ('operator', 'admin')),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create trigger users_set_updated_at
  before update on public.users
  for each row execute function private.set_updated_at();

-- -----------------------------------------------------------------------------
-- personas (10.5)
-- -----------------------------------------------------------------------------
create table public.personas (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid not null default auth.uid() references public.users (id) on delete cascade,
  name               text not null check (char_length(name) between 1 and 100),
  slug               text not null check (slug ~ '^[a-z0-9-]{1,60}$'),
  profile_image_path text,
  description        text check (char_length(description) <= 2000),
  personality        jsonb not null default '{}'::jsonb,
  speaking_style     jsonb not null default '{}'::jsonb,
  interests          jsonb not null default '[]'::jsonb,
  background         jsonb not null default '{}'::jsonb,
  content_rules      jsonb not null default '{}'::jsonb,
  interaction_rules  jsonb not null default '{}'::jsonb,
  safety_rules       jsonb not null default '{}'::jsonb,
  visual_settings    jsonb not null default '{}'::jsonb,
  status             text not null default 'active' check (status in ('active', 'inactive')),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (user_id, slug)
);

create index personas_user_id_idx on public.personas (user_id);

create trigger personas_set_updated_at
  before update on public.personas
  for each row execute function private.set_updated_at();

-- -----------------------------------------------------------------------------
-- persona_assets (10.6): Visual Identity 파일
-- -----------------------------------------------------------------------------
create table public.persona_assets (
  id           uuid primary key default gen_random_uuid(),
  persona_id   uuid not null references public.personas (id) on delete cascade,
  asset_type   text not null
               check (asset_type in ('base_model', 'lora', 'face_ref', 'style_ref', 'character_ref')),
  name         text not null check (char_length(name) between 1 and 255),
  storage_path text,
  metadata     jsonb not null default '{}'::jsonb,
  is_active    boolean not null default true,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create index persona_assets_persona_id_idx on public.persona_assets (persona_id);

create trigger persona_assets_set_updated_at
  before update on public.persona_assets
  for each row execute function private.set_updated_at();

-- -----------------------------------------------------------------------------
-- content_jobs (10.7): 핵심 Entity. 무엇을 만들 것인가.
-- -----------------------------------------------------------------------------
create table public.content_jobs (
  id              uuid primary key default gen_random_uuid(),
  persona_id      uuid not null references public.personas (id) on delete restrict,
  source          text not null default 'operator' check (source in ('operator', 'schedule', 'agent')),
  created_by      uuid default auth.uid() references public.users (id) on delete set null,
  ai_decision_id  uuid,  -- V2: ai_decisions(id) FK는 V2 마이그레이션에서 추가
  content_type    text not null check (content_type in ('image', 'video', 'carousel', 'story', 'text')),
  topic           text check (char_length(topic) <= 500),
  prompt          text check (char_length(prompt) <= 4000),
  prompt_parts    jsonb,
  negative_prompt text check (char_length(negative_prompt) <= 2000),
  workflow        text check (workflow ~ '^[a-z0-9_]+$'),
  params          jsonb not null default '{}'::jsonb,
  input_images    jsonb not null default '{}'::jsonb,
  variants        smallint not null default 1 check (variants between 1 and 4),
  platform        text check (platform in ('instagram', 'tiktok', 'x')),
  priority        smallint not null default 5 check (priority between 1 and 10),
  status          text not null default 'draft'
                  check (status in ('draft', 'queued', 'generating', 'ready', 'published', 'failed', 'cancelled')),
  run_number      smallint not null default 1 check (run_number >= 1),  -- 재시도·재생성 회차 (14.17 idempotency_key)
  scheduled_at    timestamptz,
  metadata        jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  completed_at    timestamptz
);

create index content_jobs_persona_id_idx on public.content_jobs (persona_id);
create index content_jobs_queued_idx on public.content_jobs (priority desc, created_at) where status = 'queued';
create index content_jobs_created_by_idx on public.content_jobs (created_by);

create trigger content_jobs_set_updated_at
  before update on public.content_jobs
  for each row execute function private.set_updated_at();

-- -----------------------------------------------------------------------------
-- social_accounts (10.9): MVP는 구조만, 연동은 V1
-- -----------------------------------------------------------------------------
create table public.social_accounts (
  id                      uuid primary key default gen_random_uuid(),
  persona_id              uuid not null references public.personas (id) on delete restrict,
  platform                text not null check (platform in ('instagram', 'tiktok', 'x')),
  account_id              text not null,
  username                text,
  access_token_secret_id  uuid,  -- Supabase Vault secret id (V1)
  refresh_token_secret_id uuid,
  token_expires_at        timestamptz,
  status                  text not null default 'inactive' check (status in ('active', 'inactive')),
  metadata                jsonb not null default '{}'::jsonb,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now(),
  unique (platform, account_id)
);

create index social_accounts_persona_id_idx on public.social_accounts (persona_id);

create trigger social_accounts_set_updated_at
  before update on public.social_accounts
  for each row execute function private.set_updated_at();

-- -----------------------------------------------------------------------------
-- automation_jobs (10.15): 실행 단위. 기존 media_queue를 대체한다.
-- -----------------------------------------------------------------------------
create table public.automation_jobs (
  id              uuid primary key default gen_random_uuid(),
  persona_id      uuid not null references public.personas (id) on delete restrict,
  content_job_id  uuid references public.content_jobs (id) on delete restrict,
  post_id         uuid,  -- FK는 posts 생성 후 추가
  job_type        text not null check (job_type in ('prompt', 'generation', 'caption', 'publish', 'analytics')),
  status          text not null default 'pending'
                  check (status in ('pending', 'processing', 'done', 'failed', 'cancelled')),
  worker          text not null check (worker in ('n8n', 'python')),
  claimed_by      text,  -- 선점한 인스턴스 (예: python:rtx5080-1)
  priority        smallint not null default 5 check (priority between 1 and 10),
  idempotency_key text unique,
  attempts        smallint not null default 0 check (attempts >= 0),
  max_attempts    smallint not null default 3 check (max_attempts between 1 and 10),
  run_after       timestamptz not null default now(),
  locked_at       timestamptz,
  heartbeat_at    timestamptz,
  started_at      timestamptz,
  completed_at    timestamptz,
  payload         jsonb not null default '{}'::jsonb,
  result          jsonb not null default '{}'::jsonb,
  error_type      text check (error_type in
                  ('transient', 'api', 'timeout', 'generation', 'validation', 'authentication', 'policy', 'unknown')),
  error_code      text,
  error_message   text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint automation_jobs_has_parent check (content_job_id is not null or post_id is not null)
);

create index automation_jobs_persona_id_idx on public.automation_jobs (persona_id);
create index automation_jobs_content_job_id_idx on public.automation_jobs (content_job_id);
create index automation_jobs_post_id_idx on public.automation_jobs (post_id);
create index automation_jobs_claim_idx
  on public.automation_jobs (job_type, priority desc, created_at)
  where status = 'pending';
create index automation_jobs_heartbeat_idx
  on public.automation_jobs (heartbeat_at)
  where status = 'processing';

-- 같은 Content Job의 prompt·generation 단계가 동시에 둘 이상 진행되지 않게 한다 (10.21 Rule 3).
-- caption은 Asset마다 하나씩 동시에 만들 수 있으므로 대상에서 뺀다.
create unique index automation_jobs_one_active_step
  on public.automation_jobs (content_job_id, job_type)
  where status in ('pending', 'processing') and job_type in ('prompt', 'generation');

create trigger automation_jobs_set_updated_at
  before update on public.automation_jobs
  for each row execute function private.set_updated_at();

-- -----------------------------------------------------------------------------
-- assets (10.8): 실행 후 검증을 통과한 파일만 행이 생긴다 (11.7)
-- -----------------------------------------------------------------------------
create table public.assets (
  id                  uuid primary key default gen_random_uuid(),
  persona_id          uuid not null references public.personas (id) on delete restrict,
  content_job_id      uuid not null references public.content_jobs (id) on delete restrict,
  automation_job_id   uuid references public.automation_jobs (id) on delete set null,
  asset_type          text not null check (asset_type in ('image', 'video')),
  file_name           text not null,
  storage_bucket      text not null default 'media',
  storage_path        text not null unique,
  public_url          text,
  thumbnail_url       text,
  mime_type           text not null,
  width               integer check (width > 0),
  height              integer check (height > 0),
  duration            numeric check (duration > 0),
  prompt              text,
  workflow            jsonb,
  generation_metadata jsonb not null default '{}'::jsonb,
  status              text not null default 'generated'
                      check (status in ('generated', 'approved', 'rejected', 'archived')),
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create index assets_persona_id_idx on public.assets (persona_id);
create index assets_content_job_id_idx on public.assets (content_job_id);
create index assets_automation_job_id_idx on public.assets (automation_job_id);

create trigger assets_set_updated_at
  before update on public.assets
  for each row execute function private.set_updated_at();

-- -----------------------------------------------------------------------------
-- posts (10.10): MVP는 Caption 초안(draft)까지, 게시는 V1
-- -----------------------------------------------------------------------------
create table public.posts (
  id                uuid primary key default gen_random_uuid(),
  persona_id        uuid not null references public.personas (id) on delete restrict,
  asset_id          uuid not null references public.assets (id) on delete restrict,
  social_account_id uuid references public.social_accounts (id) on delete restrict,
  platform          text not null check (platform in ('instagram', 'tiktok', 'x')),
  external_post_id  text,
  permalink         text,
  caption           text check (char_length(caption) <= 2200),
  hashtags          text[] not null default '{}' check (cardinality(hashtags) <= 30),
  is_sponsored      boolean not null default false,
  status            text not null default 'draft'
                    check (status in ('draft', 'pending_approval', 'approved', 'scheduled',
                                      'publishing', 'published', 'failed', 'rejected', 'cancelled')),
  scheduled_at      timestamptz,
  published_at      timestamptz,
  error             text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  -- 11.13 Invariant 3: 게시된 Post는 반드시 외부 게시물 ID가 있다
  constraint posts_published_has_external_id check (status <> 'published' or external_post_id is not null),
  constraint posts_scheduled_has_time check (status <> 'scheduled' or scheduled_at is not null)
);

create index posts_persona_id_idx on public.posts (persona_id);
create index posts_asset_id_idx on public.posts (asset_id);
create index posts_social_account_id_idx on public.posts (social_account_id);
create index posts_due_idx on public.posts (scheduled_at) where status = 'scheduled';

create trigger posts_set_updated_at
  before update on public.posts
  for each row execute function private.set_updated_at();

alter table public.automation_jobs
  add constraint automation_jobs_post_id_fkey
  foreign key (post_id) references public.posts (id) on delete restrict;

-- -----------------------------------------------------------------------------
-- execution_logs (10.16)
-- -----------------------------------------------------------------------------
create table public.execution_logs (
  id                bigint generated always as identity primary key,
  automation_job_id uuid not null references public.automation_jobs (id) on delete restrict,
  step              text not null check (char_length(step) <= 100),
  service           text not null check (service in ('n8n', 'python', 'comfyui', 'llm', 'supabase', 'sns')),
  status            text not null check (status in ('started', 'succeeded', 'failed')),
  input_data        jsonb,
  output_data       jsonb,
  duration_ms       bigint check (duration_ms >= 0),
  execution_ref     text,
  error             text,
  created_at        timestamptz not null default now()
);

create index execution_logs_automation_job_id_idx on public.execution_logs (automation_job_id, created_at);

-- -----------------------------------------------------------------------------
-- system_errors (10.19)
-- -----------------------------------------------------------------------------
create table public.system_errors (
  id                bigint generated always as identity primary key,
  automation_job_id uuid references public.automation_jobs (id) on delete restrict,
  persona_id        uuid references public.personas (id) on delete restrict,
  service           text not null,
  step              text,
  error_type        text not null check (error_type in
                    ('transient', 'api', 'timeout', 'generation', 'validation', 'authentication', 'policy', 'unknown')),
  error_code        text,
  message           text,
  stack_trace       text,
  retryable         boolean not null default false,
  resolved          boolean not null default false,
  created_at        timestamptz not null default now()
);

create index system_errors_automation_job_id_idx on public.system_errors (automation_job_id);
create index system_errors_persona_id_idx on public.system_errors (persona_id, created_at desc);

-- -----------------------------------------------------------------------------
-- state_transitions (11.14)
-- -----------------------------------------------------------------------------
create table public.state_transitions (
  id          bigint generated always as identity primary key,
  entity_type text not null check (entity_type in ('content_job', 'automation_job', 'asset', 'post', 'approval')),
  entity_id   uuid not null,
  persona_id  uuid,
  from_status text,
  to_status   text not null,
  actor_type  text not null check (actor_type in ('operator', 'n8n', 'python', 'agent', 'system')),
  actor_id    uuid,
  reason      text,
  metadata    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);

create index state_transitions_entity_idx on public.state_transitions (entity_type, entity_id, created_at);
create index state_transitions_persona_id_idx on public.state_transitions (persona_id, created_at desc);

-- -----------------------------------------------------------------------------
-- comfy_workflows (12.5): 로컬 Registry의 사본
-- -----------------------------------------------------------------------------
create table public.comfy_workflows (
  id        text primary key check (id ~ '^[a-z0-9_]+$'),
  version   text not null,
  type      text not null check (type in ('image', 'video')),
  stage     text not null check (stage in ('mvp', 'v1', 'v2', 'long_term')),
  enabled   boolean not null default true,
  params    jsonb not null default '{}'::jsonb,
  inputs    jsonb not null default '{}'::jsonb,
  synced_at timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- security_events (15.22)
-- -----------------------------------------------------------------------------
create table public.security_events (
  id         bigint generated always as identity primary key,
  event_type text not null,
  actor_type text not null check (actor_type in ('operator', 'n8n', 'python', 'agent', 'system', 'anonymous')),
  actor_id   uuid,
  persona_id uuid references public.personas (id) on delete set null,
  source_ip  inet,
  detail     jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index security_events_created_at_idx on public.security_events (created_at desc);
create index security_events_actor_id_idx on public.security_events (actor_id);

-- -----------------------------------------------------------------------------
-- Realtime (12.3): Dashboard가 구독하는 테이블
-- -----------------------------------------------------------------------------
alter publication supabase_realtime
  add table public.content_jobs, public.automation_jobs, public.assets, public.posts;
