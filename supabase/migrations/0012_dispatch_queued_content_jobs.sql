-- =============================================================================
-- 0012 Local Dispatcher: queued Content Job → generation Job (DB 함수 + pg_cron)
--   TECH_DESIGN 56.10. n8n 없이도 Control Center의 '생성'이 Worker Pull까지 이어지게 한다.
--
--   n8n WF-001·002가 하던 일 중 "프롬프트가 이미 있는 주문"만 DB가 맡는다:
--     queued → claim(generating) → generation Job (generation:{content_job_id}:{run_number})
--   프롬프트가 없는 주문(LLM이 써야 하는 것)은 건드리지 않는다. 그것은 계속 n8n + LLM의 일이다.
--
--   LLM은 아무것도 고르지 않는다. 입력은 이미 저장된 Content Job뿐이고, 실행 위치(Target)는
--   Content Job에서 create_automation_job이 그대로 상속한다 (이 함수는 Target을 만지지 않는다).
-- =============================================================================

create or replace function public.dispatch_queued_content_jobs(p_limit integer default 10)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_prev   jsonb;
  v_count  integer := 0;
  v_code   text;
  v_msg    text;
  cj       public.content_jobs;
  v_job    public.automation_jobs;
begin
  if p_limit is null or p_limit < 1 or p_limit > 100 then
    perform private.raise_api_error('VALIDATION_FAILED', 'p_limit must be between 1 and 100');
  end if;

  -- 전역 정지·생성 스위치 (TECH_DESIGN 32.6, 33.10). 행이 있고 값이 기대와 다르면 닫힌 것으로 본다(fail closed).
  -- 두 키는 아직 기본 설정에 없을 수 있다(M7). 없으면 막지 않는다.
  if exists (select 1 from public.app_settings s where s.key = 'emergency_stop_all' and s.value <> 'false'::jsonb)
     or exists (select 1 from public.app_settings s where s.key = 'generation_enabled' and s.value <> 'true'::jsonb) then
    return 0;
  end if;

  v_prev := private.begin_system_change('dispatch');
  for cj in
    select c.*
      from public.content_jobs c
      join public.personas p on p.id = c.persona_id
     where c.status = 'queued'
       and p.status = 'active'
       and (c.scheduled_at is null or c.scheduled_at <= now())
       -- 프롬프트가 이미 있는 주문만. 없으면 WF-002(LLM)의 몫이다.
       and (nullif(btrim(c.prompt), '') is not null
            or (c.prompt_parts is not null and c.prompt_parts not in ('{}'::jsonb, 'null'::jsonb)))
     order by c.priority desc, c.created_at
     limit p_limit
       for update of c skip locked
  loop
    begin
      -- 선점과 Job 생성은 한 덩어리다. 둘째가 실패하면 첫째도 되돌아가 queued로 남는다.
      perform 1 from public.claim_content_job(cj.id);
      if not found then
        continue;                                   -- 다른 곳이 먼저 가져갔다
      end if;

      v_job := public.create_automation_job(
        p_job_type        => 'generation',
        p_persona_id      => cj.persona_id,
        p_content_job_id  => cj.id,
        p_worker          => 'python',
        p_max_attempts    => 3,
        p_idempotency_key => format('generation:%s:%s', cj.id, cj.run_number));

      insert into public.execution_logs (automation_job_id, step, service, status, input_data)
      values (v_job.id, 'DISPATCH', 'supabase', 'succeeded',
              jsonb_build_object('dispatcher', 'pg_cron', 'content_job_id', cj.id));
      v_count := v_count + 1;
    exception when others then
      get stacked diagnostics v_msg = message_text;
      v_code := case when sqlstate = 'PT429' then 'RATE_LIMITED' else sqlstate end;

      -- 삼키지 않는다 (CLAUDE.md 10). 같은 Persona의 같은 원인은 1시간에 한 줄만 남긴다.
      if not exists (select 1 from public.system_errors e
                      where e.persona_id = cj.persona_id and e.service = 'dispatcher' and e.error_code = v_code
                        and not e.resolved and e.created_at > now() - interval '1 hour') then
        insert into public.system_errors (persona_id, service, step, error_type, error_code, message, retryable)
        values (cj.persona_id, 'dispatcher', 'DISPATCH',
                case when sqlstate = 'PT429' then 'policy' else 'unknown' end,
                v_code, left(format('content job %s: %s', cj.id, v_msg), 500), true);
      end if;

      -- 하루 한도는 전체에 걸린다. 이번 회차에서 더 시도해도 같은 결과다. 다음 주기에 다시 본다.
      exit when sqlstate = 'PT429';
    end;
  end loop;
  perform private.end_system_change(v_prev);
  return v_count;
end;
$$;

-- 워커·화면·익명은 실행할 수 없다. pg_cron(postgres)과 service_role만.
revoke all on function public.dispatch_queued_content_jobs(integer) from public, anon, authenticated;
grant execute on function public.dispatch_queued_content_jobs(integer) to service_role;

-- -----------------------------------------------------------------------------
-- 스케줄: 10초마다 (pg_cron이 초 단위를 모르면 1분마다). 로컬 테스트 DB에는 pg_cron이 없어 건너뛴다.
-- -----------------------------------------------------------------------------
do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.unschedule(jobid) from cron.job where jobname = 'dispatch-queued-content-jobs';
    begin
      perform cron.schedule('dispatch-queued-content-jobs', '10 seconds',
                            'select public.dispatch_queued_content_jobs()');
    exception when others then
      perform cron.schedule('dispatch-queued-content-jobs', '* * * * *',
                            'select public.dispatch_queued_content_jobs()');
    end;
  end if;
end
$$;
