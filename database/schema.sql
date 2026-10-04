-- =============================================================================
-- persona-automation-agent : Supabase schema
-- 실행: Supabase Dashboard → SQL Editor 에 전체 붙여넣고 Run (여러 번 실행해도 안전)
--
-- 접근 모델
--   * 모든 테이블 RLS ON + 정책 없음 → anon/authenticated 키로는 접근 불가
--   * n8n 과 comfy_bridge.py 는 service_role 키로 접근 (RLS 우회)
--   * 나중에 대시보드 등 클라이언트 접근이 필요하면 그때 정책을 추가한다
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 공통: updated_at 자동 갱신 트리거
-- -----------------------------------------------------------------------------
create or replace function public.set_updated_at()
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
-- 1) messages : DM/댓글 대화 내역
-- -----------------------------------------------------------------------------
create table if not exists public.messages (
  id               uuid primary key default gen_random_uuid(),
  persona_id       text        not null,                 -- 어떤 버추얼 인플루언서의 대화인지
  platform         text        not null,                 -- instagram, x, tiktok, ...
  conversation_id  text        not null,                 -- 플랫폼의 스레드/DM 방 ID
  external_user_id text,                                 -- 상대방의 플랫폼 사용자 ID
  role             text        not null
                   check (role in ('user', 'assistant', 'system')),
  content          text        not null,
  intent           text,                                 -- 분류된 의도 (선택)
  metadata         jsonb       not null default '{}'::jsonb,
  created_at       timestamptz not null default now()
);

create index if not exists messages_conversation_created_idx
  on public.messages (conversation_id, created_at);

create index if not exists messages_persona_created_idx
  on public.messages (persona_id, created_at desc);

alter table public.messages enable row level security;

-- -----------------------------------------------------------------------------
-- 2) media_queue : ComfyUI 생성 작업 대기열
--    상태 흐름: pending → processing → done | failed
--    일시적 오류(타임아웃, 연결 끊김, GPU OOM 등)는 attempts < max_attempts 이면
--    브릿지가 status 를 'pending' 으로 되돌리고 run_after 를 지수 백오프로 미룬다.
--    (failed 를 수동 재시도하려면 status='pending', attempts=0, run_after=now() 로 되돌린다)
-- -----------------------------------------------------------------------------
create table if not exists public.media_queue (
  id                uuid primary key default gen_random_uuid(),
  persona_id        text        not null,
  job_type          text        not null
                    check (job_type in ('image', 'video', 'faceswap')),
  workflow          text        not null,                -- workflows/<workflow>.json 템플릿 이름
  params            jsonb       not null default '{}'::jsonb,
                                                          -- 템플릿 {{placeholder}} 치환 값
                                                          -- 예: {"prompt": "...", "seed": 42}
  input_images      jsonb       not null default '{}'::jsonb,
                                                          -- {"placeholder": "https://... 또는 storage 경로"}
                                                          -- 브릿지가 ComfyUI 에 업로드 후 파일명으로 치환
  status            text        not null default 'pending'
                    check (status in ('pending', 'processing', 'done', 'failed')),
  priority          smallint    not null default 0,      -- 클수록 먼저 처리
  attempts          smallint    not null default 0,     -- 선점(실행 시도) 횟수
  max_attempts      smallint    not null default 3
                    check (max_attempts >= 1),
  run_after         timestamptz not null default now(), -- 이 시각 이후에만 선점 가능 (재시도 백오프)
  comfy_prompt_id   text,
  output_paths      text[]      not null default '{}',   -- Storage 내부 경로
  output_urls       text[]      not null default '{}',   -- 공개 URL (SNS 업로드용)
  error             text,                               -- 마지막 오류 (재시도 중에도 남겨둠)
  source_message_id uuid        references public.messages (id) on delete set null,
  created_at        timestamptz not null default now(),
  started_at        timestamptz,
  completed_at      timestamptz,
  updated_at        timestamptz not null default now()
);

-- 대기 작업 꺼내기용 부분 인덱스
create index if not exists media_queue_pending_idx
  on public.media_queue (priority desc, created_at, run_after)
  where status = 'pending';

create index if not exists media_queue_status_idx
  on public.media_queue (status);

create index if not exists media_queue_source_message_idx
  on public.media_queue (source_message_id);

drop trigger if exists media_queue_set_updated_at on public.media_queue;
create trigger media_queue_set_updated_at
  before update on public.media_queue
  for each row execute function public.set_updated_at();

alter table public.media_queue enable row level security;

-- -----------------------------------------------------------------------------
-- 3) posts : SNS 포스팅 스케줄
--    상태 흐름: draft → scheduled → publishing → published | failed
-- -----------------------------------------------------------------------------
create table if not exists public.posts (
  id               uuid primary key default gen_random_uuid(),
  persona_id       text        not null,
  media_job_id     uuid        references public.media_queue (id) on delete set null,
  platform         text        not null,
  caption          text,
  hashtags         text[]      not null default '{}',
  media_urls       text[]      not null default '{}',    -- 보통 media_queue.output_urls 복사
  status           text        not null default 'draft'
                   check (status in ('draft', 'scheduled', 'publishing', 'published', 'failed')),
  scheduled_at     timestamptz,
  published_at     timestamptz,
  external_post_id text,                                 -- 플랫폼이 돌려준 게시물 ID
  error            text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  constraint posts_scheduled_needs_time
    check (status <> 'scheduled' or scheduled_at is not null)
);

-- 발행 대상 조회: status = 'scheduled' and scheduled_at <= now()
create index if not exists posts_due_idx
  on public.posts (scheduled_at)
  where status = 'scheduled';

create index if not exists posts_media_job_idx
  on public.posts (media_job_id);

drop trigger if exists posts_set_updated_at on public.posts;
create trigger posts_set_updated_at
  before update on public.posts
  for each row execute function public.set_updated_at();

alter table public.posts enable row level security;

-- -----------------------------------------------------------------------------
-- 작업 선점(claim) 함수
--   n8n 이 같은 작업을 두 번 보내도(재시도 등) 한 번만 processing 으로 바뀐다.
-- -----------------------------------------------------------------------------

-- 특정 작업 선점: pending 이고 run_after 가 지났을 때만 processing 으로 전환하고 그 행을 반환 (아니면 0행)
create or replace function public.claim_media_job(p_job_id uuid)
returns setof public.media_queue
language sql
set search_path = ''
as $$
  update public.media_queue
     set status     = 'processing',
         started_at = now(),
         attempts   = attempts + 1
   where id = p_job_id
     and status = 'pending'
     and run_after <= now()
  returning *;
$$;

-- 가장 우선순위 높은 대기 작업 1건 선점 (폴링 방식용)
create or replace function public.claim_next_media_job()
returns setof public.media_queue
language sql
set search_path = ''
as $$
  update public.media_queue q
     set status     = 'processing',
         started_at = now(),
         attempts   = q.attempts + 1
   where q.id = (
           select id
             from public.media_queue
            where status = 'pending'
              and run_after <= now()
            order by priority desc, created_at
            limit 1
              for update skip locked
         )
  returning q.*;
$$;

revoke execute on function public.claim_media_job(uuid)  from public, anon, authenticated;
revoke execute on function public.claim_next_media_job() from public, anon, authenticated;
grant  execute on function public.claim_media_job(uuid)  to service_role;
grant  execute on function public.claim_next_media_job() to service_role;

-- -----------------------------------------------------------------------------
-- Storage 버킷
--   public = true : Instagram Graph API 등은 공개 URL 로 미디어를 가져가므로 공개 버킷 사용.
--   공개 전 결과물을 숨기고 싶다면 false 로 만들고 브릿지에서 signed URL 을 쓰도록 바꾼다.
-- -----------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('media', 'media', true)
on conflict (id) do nothing;
