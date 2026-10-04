-- =============================================================================
-- 0006 Scheduled jobs (TECH_DESIGN 11.6)
--   pg_cron이 필요하다. Supabase에서는 Dashboard → Database → Extensions에서도 켤 수 있다.
--   로컬 테스트 DB(pgserver)에는 pg_cron이 없으므로 이 파일은 테스트에서 건너뛴다.
-- =============================================================================

create extension if not exists pg_cron with schema pg_catalog;

do $$
begin
  perform cron.unschedule(jobid) from cron.job where jobname = 'recover-stale-jobs';
end
$$;

-- Heartbeat가 끊긴 processing Job 회수, 1분마다
select cron.schedule('recover-stale-jobs', '* * * * *', $$select public.recover_stale_jobs()$$);
