# MVP Checklist

> 두 가지를 한 곳에 둔다: ① **기능 하나를 끝낼 때마다 채우는 12칸** (UI만 만들고 "완료"라고 하는 것을 막는다), ② **MVP v1.0 완료 목록 24개** ([MVP_SCOPE_LOCK.md](MVP_SCOPE_LOCK.md) 8장).
> 규칙: 칸을 비워 둔 채 "완료"라고 하지 않는다. 해당 없으면 `N/A`와 이유를 적는다. 통과하지 못한 것은 결과와 함께 그대로 적는다.

## 1. 기능 완료 체크 (기능마다 복사해서 쓴다)

```text
기능: ____________________   Sprint/Milestone: ______   관련 TECH_DESIGN 절: ______

[ ] DB            마이그레이션(새 파일), 제약, 상태 값은 DB 값
[ ] RLS           소유 Persona 경유 읽기, 쓰기는 RPC, 교차 계정 테스트
[ ] Repository    (이 프로젝트는 Hook이 Supabase를 부른다) 쿼리·RPC 호출 함수, 생성한 타입
[ ] Hook          로딩·빈·오류·권한 상태, Realtime 구독 정리
[ ] UI            실제 상태만 표시(가짜 지표·가짜 진행률 없음), 위험한 동작은 확인, 한국어 문구
[ ] API           RPC / 브릿지 / Webhook 계약과 오류 코드
[ ] Error handling  error_code, retryable, 재시도 한도, 사람이 볼 수 있는 실패 표시
[ ] Idempotency   키, 중복 호출·응답 유실 시 결과, 불확실한 외부 호출은 확인 후 진행
[ ] Logging       execution_logs / state_transitions / system_errors, 비밀값·본문 없음
[ ] Tests         단위 + 통합 + 실패·복구, 동시 선점·멱등·교차 계정, 전체 pytest 통과 출력
[ ] Security      비밀값·RLS·입력 검증·인젝션·한도·긴급 정지 (.claude/skills/pa-security)
[ ] Documentation TECH_DESIGN / MVP_SCOPE_LOCK 갱신(아키텍처가 바뀌었다면 먼저), README 진행 상황
```

해당 없는 칸의 예: 순수 DB 함수 → `Hook`·`UI` N/A. 화면만 있는 읽기 전용 기능 → `Idempotency` N/A.

## 2. MVP v1.0 완료 목록 (24개)

범례: ☐ 미완 · 근거는 기술 설계의 확인 방법이다. Sprint는 [MVP_SCOPE_LOCK.md](MVP_SCOPE_LOCK.md) 0장.

| # | 항목 | Sprint | 확인 방법 | 상태 |
|---|---|---|---|---|
| 1 | Google 로그인 (허용 목록 밖은 거부) | 1 | 24.3, 27.7 S3 | ☐ |
| 2 | Persona 생성 | 1 | 45장, 22장 | ☐ |
| 3 | Persona 활성화 (`active`) | 1 | 45장 | ☐ |
| 4 | Content Job 생성 (`queued`) | 1 | 27.4 | ☐ |
| 5 | n8n Claim (Atomic, 중복 없음) | 1 | 27.6 D3, 49.5 3번 | ☐ |
| 6 | Python 실행 (브릿지 선점 → 생성) | 1 | 25.6 | ☐ |
| 7 | ComfyUI 생성 (활성 GPU: Local 또는 Cloud) | 1 | 25.6, 48.8 | ☐ |
| 8 | Asset 업로드 (Storage) | 1 | 27.4 | ☐ |
| 9 | Asset DB 등록 (lineage 칸 포함) | 1 | 27.3 확인 SQL, 50.3 | ☐ |
| 10 | Asset 승인·반려 (사유) | 2 | 50.5 3번 | ☐ |
| 11 | Post 생성 (캡션 초안 → 제출) | 2 | 28.7, 51장 | ☐ |
| 12 | SNS 게시 (Instagram 또는 X) | 2 | 28.15 E2E, 관문 G2 | ☐ |
| 13 | Post ID 저장 (`external_post_id`, `permalink`) | 2 | 11.8 CHECK | ☐ |
| 14 | 성과 수집 (24h·168h Snapshot) | 3 | 29.20 테스트, 52장 | ☐ |
| 15 | Analytics 표시 (실제 값만) | 3 | 29.18 | ☐ |
| 16 | AI Decision 생성 (Structured) | 4 | 30.18, 54장 | ☐ |
| 17 | Decision 검증 (n8n 조기 + DB 최종) | 4 | 54.5 1번 테스트 | ☐ |
| 18 | Decision → 새 Content Job (승인 후, `source = 'agent'`) | 4 | 30.18 E2E | ☐ |
| 19 | Audit Log | 1~4 | `state_transitions`, `execution_logs`, `ai_decisions` | ☐ |
| 20 | Emergency Stop (전역·플랫폼·Persona) | 2~4 | 33.16, 30.18 (`agent_enabled = false`) | ☐ |
| 21 | Retry (일시 오류 재시도, 최종 실패 표시) | 1 | 27.5 F2·F6 | ☐ |
| 22 | Idempotency (생성·게시·수집·결정) | 1~4 | 27.6 D1~D3, 43.8, 49.5 1번 | ☐ |
| 23 | RLS (교차 계정·교차 Persona) | 1~4 | 27.7 S2~S4, 50.5 1번, 51.5 3번 | ☐ |
| 24 | Error Monitoring (실패 Job·`system_errors` 확인) | 1 | 17.11 Error Center | ☐ |

**최종 인수:** 사람 손 없이 `Persona → Content Job → 생성 → Asset → 승인 → 게시 → 성과 → AI Decision → 새 Content Job`이 한 바퀴 돈다 (MVP_SCOPE_LOCK 1장, TECH_DESIGN 27.8의 확장). 기록은 27장 형식으로 남긴다.

## 3. 기능별로 채울 칸 (예)

| 기능 | DB | RLS | Hook | UI | API | Error | Idem | Log | Test | Sec | Doc |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Persona CRUD | ✔ | ✔ | ✔ | ✔ | RPC | ✔ | N/A | ✔ | ✔ | ✔ | ✔ |
| Content Job 생성 | ✔ | ✔ | ✔ | ✔ | RPC | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| 브릿지 생성 | ✔ | ✔ | N/A | N/A | `/v1/jobs` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Asset Library | N/A (읽기) | ✔ | ✔ | ✔ | RPC(`archive_asset`) | ✔ | N/A | ✔ | ✔ | ✔ | ✔ |
| Post 게시 | ✔ | ✔ | ✔ | ✔ | n8n/Adapter | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| 성과 수집 | ✔ | ✔ | ✔ | ✔ | n8n/`record_metrics` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| AI Decision | ✔ | ✔ | ✔ | ✔ | RPC + n8n | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |

`✔`는 "필요하고 해야 한다"는 뜻이다 (완료 표시가 아니다). 실제 진행은 기능마다 1장의 양식을 복사해 채운다.

## 4. 진행 기록

| 날짜 | 기능 | 12칸 | 비고 (미통과·N/A 사유) |
|---|---|---|---|
| | | | |
