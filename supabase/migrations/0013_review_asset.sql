-- =============================================================================
-- 0013 Asset 검토: review_asset (승인·반려 + 반려 사유)
--   TECH_DESIGN 12.4 (V1 RPC 표), 50.5 3번. MVP 완료 목록 10번 (MVP_SCOPE_LOCK 3.6).
--
--   Asset 검토와 Post 게시 승인(approvals)은 별개다. 이 함수는 Asset의 상태만 바꾼다:
--     generated·rejected → approved
--     generated·approved → rejected
--   허용 전이표(private.allowed_transitions)와 가드(게시·예약·게시 중인 Post가 있으면 approved → rejected 불가)는
--   0002에 이미 있다. 여기서는 새로 만들지 않는다.
--
--   반려 사유: assets.review_note (500자 이하). 반려할 때 저장하고, 다시 승인하면 비운다.
--   누가·언제는 state_transitions(actor, created_at)에 남는다 — 그래서 별도 칸을 만들지 않는다.
--
--   쓰기는 RPC로만 한다 (CLAUDE.md 7). authenticated에게는 assets UPDATE 권한이 없다.
--   LLM·워커·n8n은 이 함수를 부르지 못한다 (authenticated 전용, 워커 RPC 허용 목록에 넣지 않는다).
-- =============================================================================

alter table public.assets
  add column if not exists review_note text
  check (review_note is null or char_length(review_note) <= 500);

comment on column public.assets.review_note is
  'Reject reason written by review_asset(rejected). Cleared when the asset is approved again.';

create or replace function public.review_asset(
  p_asset_id uuid,
  p_decision text,
  p_note     text default null
)
returns public.assets
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid  uuid := private.require_operator();
  v      public.assets;
  v_note text := nullif(btrim(p_note), '');
begin
  if p_decision is null or p_decision not in ('approved', 'rejected') then
    perform private.raise_api_error('VALIDATION_FAILED', 'decision must be approved or rejected');
  end if;
  if v_note is not null and char_length(v_note) > 500 then
    perform private.raise_api_error('VALIDATION_FAILED', 'note must be 500 characters or fewer');
  end if;
  if p_decision = 'approved' and v_note is not null then
    perform private.raise_api_error('VALIDATION_FAILED', 'a note is only for rejection');
  end if;

  -- 내 Persona의 Asset만 (남의 것은 존재하지 않는 것처럼 NOT_FOUND)
  select a.* into v
    from public.assets a
    join public.personas p on p.id = a.persona_id
   where a.id = p_asset_id and p.user_id = v_uid
     for update of a;
  if v.id is null then
    perform private.raise_api_error('NOT_FOUND', 'asset not found');
  end if;

  perform private.expect_status(
    v.status,
    case p_decision when 'approved' then array['generated', 'rejected'] else array['generated', 'approved'] end,
    'asset');

  perform private.set_reason(case p_decision when 'approved' then 'operator:approve' else 'operator:reject' end);
  update public.assets
     set status = p_decision,
         review_note = case when p_decision = 'rejected' then v_note else null end
   where id = v.id
  returning * into v;
  perform private.set_reason(null);
  return v;
end;
$$;

-- Lovable(authenticated)만. 기본 권한에 기대지 않고 명시적으로 닫는다 (0010·0011과 같은 방식).
revoke all on function public.review_asset(uuid, text, text) from public, anon;
grant execute on function public.review_asset(uuid, text, text) to authenticated;
