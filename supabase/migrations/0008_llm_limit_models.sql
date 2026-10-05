-- =============================================================================
-- 0008 LLM 하루 호출 한도, 설치된 모델 목록 (TECH_DESIGN 21.18, 22.9, 15.18)
--   1. reserve_llm_call: n8n이 실제 LLM을 부르기 전에 하루 호출 수를 하나 예약한다.
--   2. worker_status.models: 브릿지가 ComfyUI에 설치된 체크포인트·LoRA 이름을 보고한다.
--      Lovable Visual Identity가 이 목록으로 드롭다운을 보여준다.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. 하루 사용량 카운터 (private: API로 노출되지 않는다)
-- -----------------------------------------------------------------------------
create table private.usage_counters (
  day   date    not null,
  key   text    not null check (char_length(key) between 1 and 64),
  count integer not null default 0 check (count >= 0),
  primary key (day, key)
);

-- reserve_llm_call: 잠금이 맞는 prompt·caption Job만 호출을 예약한다.
--   {"allowed": true,  "count": n, "limit": m}
--   {"allowed": false, "reason": "rate_limited", "limit": m}   하루 한도 도달 (UTC 기준)
--   {"allowed": false, "reason": "lock_lost"}                  회수·취소된 Job
-- 한도에 걸려도 예외를 내지 않는다: 예외를 내면 같은 트랜잭션의 security_events 기록도 롤백된다.
create or replace function public.reserve_llm_call(p_job_id uuid, p_locked_at timestamptz)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v       public.automation_jobs := private.lock_processing_job(p_job_id, p_locked_at);
  v_day   date := (now() at time zone 'utc')::date;
  v_limit integer := private.setting_int('limits', 'daily_llm_calls_limit', 1000);
  v_count integer;
begin
  if v.id is null then
    return jsonb_build_object('allowed', false, 'reason', 'lock_lost');
  end if;
  if v.job_type not in ('prompt', 'caption') then
    perform private.raise_api_error('VALIDATION_FAILED', format('%s jobs do not call the LLM', v.job_type));
  end if;

  if v_limit > 0 then
    -- 동시에 호출돼도 한도를 넘지 않게 한 문장으로 증가시킨다 (한도에 닿으면 갱신되지 않음)
    insert into private.usage_counters as u (day, key, count)
    values (v_day, 'llm_calls', 1)
    on conflict (day, key) do update set count = u.count + 1
     where u.count < v_limit
    returning u.count into v_count;
  end if;

  if v_count is null then
    insert into public.security_events (event_type, actor_type, persona_id, detail)
    values ('RATE_LIMITED', 'n8n', v.persona_id,
            jsonb_build_object('limit', 'daily_llm_calls_limit', 'value', v_limit,
                               'automation_job_id', v.id, 'job_type', v.job_type));
    return jsonb_build_object('allowed', false, 'reason', 'rate_limited', 'limit', v_limit);
  end if;
  return jsonb_build_object('allowed', true, 'count', v_count, 'limit', v_limit);
end;
$$;

-- -----------------------------------------------------------------------------
-- 2. 설치된 모델 목록
--    {"checkpoints": ["model_a.safetensors", ...], "loras": ["gina_v2.safetensors", ...]}
-- -----------------------------------------------------------------------------
alter table public.worker_status
  add column models jsonb not null default '{}'::jsonb;

-- 기존 정의를 교체한다 (인자가 같아 0007의 권한이 유지된다).
-- p_info에 models가 없으면 이전 값을 지우지 않는다 (ComfyUI가 잠시 꺼져 목록을 못 읽은 경우).
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
    (id, kind, comfyui_ok, gpu, current_job_id, queue_size, version, models, last_seen_at)
  values (p_worker_id, p_kind,
          (p_info ->> 'comfyui_ok')::boolean,
          coalesce(p_info -> 'gpu', '{}'::jsonb),
          nullif(p_info ->> 'current_job_id', '')::uuid,
          coalesce((p_info ->> 'queue_size')::integer, 0),
          p_info ->> 'version',
          coalesce(v_models, '{}'::jsonb),
          now())
  on conflict (id) do update
     set kind = excluded.kind, comfyui_ok = excluded.comfyui_ok, gpu = excluded.gpu,
         current_job_id = excluded.current_job_id, queue_size = excluded.queue_size,
         version = excluded.version,
         models = coalesce(v_models, w.models),
         last_seen_at = excluded.last_seen_at;
exception
  when check_violation or invalid_text_representation then
    perform private.raise_api_error('VALIDATION_FAILED', sqlerrm);
end;
$$;

-- -----------------------------------------------------------------------------
-- 권한
-- -----------------------------------------------------------------------------
revoke execute on function public.reserve_llm_call(uuid, timestamptz) from public, anon, authenticated;
grant execute on function public.reserve_llm_call(uuid, timestamptz) to service_role;
