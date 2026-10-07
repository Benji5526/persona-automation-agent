# MVP Scope Lock — persona-automation-agent v1.0

> 이 문서는 **어디까지 만들면 MVP v1.0인가**를 고정한다. 기능을 더하려면 이 문서를 먼저 고친다 (CLAUDE.md 18번).
> 설계의 정본은 [TECH_DESIGN.md](TECH_DESIGN.md)이고, 이 문서는 그 위에 **범위의 선**을 긋는다. 설계와 충돌하는 곳은 ⚙️로 표시했고 아래 "열린 결정"에 모았다.
> 체크리스트: [MVP_CHECKLIST.md](MVP_CHECKLIST.md) · Claude Code 규칙: [../CLAUDE.md](../CLAUDE.md) · Skill: `.claude/skills/pa-*`

## 0. 용어: 이 문서의 "MVP v1.0"

기술 설계(40.4)는 단계를 **MVP(M0~M5) → V1(M6~M8) → V2a(M9~)** 로 나눈다. 이 문서의 **MVP v1.0은 그 셋을 이은 하나의 목표**다.

| 이 문서의 MVP v1.0 구성 | 기술 설계의 단계 | Sprint (44장) |
|---|---|---|
| 로그인 · Persona · Content Job → 생성 → Asset · 캡션 초안 | MVP (M0~M5) | 1 |
| 계정 연결 · 승인 · 게시 (Instagram, 또는 X) | V1 (M6~M8 중 게시) | 2 |
| 성과 수집 · Analytics | V1 (M8) | 3 |
| AI 성과 분석 · AI Decision (`create_content`) · 승인 · 새 Content Job | V2a (M9), **권한 수준 1~2** | 4 |
| 팬 상호작용 | V2a (M10) — **MVP v1.0 밖** | 5 |

기술 설계에서 "MVP"라고만 쓰면 Sprint 1(생성 파이프라인)을 뜻한다. 이 문서에서 "MVP v1.0"이라고 쓰면 위 표의 Sprint 1~4 전체를 뜻한다.

## 1. 목적

AI Virtual Influencer가 아래 사이클을 **실제 환경에서 한 번 이상 안정적으로** 돈다. 기능 수가 아니라 **하나의 완전한 반자동 Content Loop**가 작동하는 것이 성공 기준이다.

```text
Persona → Content Job → n8n → Python → ComfyUI → Asset → Approval → Post → SNS
  → Performance → Analytics → AI Decision → 새 Content Job
```

완전 무인 운영보다 **사람 승인을 포함한 반자동**을 먼저 한다 (AI는 게시하지 않는다, 28.13).

## 2. MVP v1.0 성공 조건

모두 만족하면 MVP v1.0 완료다. (오른쪽은 기술 설계의 근거)

| # | 조건 | 근거 |
|---|---|---|
| 1 | Google 로그인, **허용 목록 밖 계정 가입 거부** | 24.3, 16.14 |
| 2 | 사용자별 데이터 분리 (RLS, 교차 계정 접근 차단) | 10.21, 45.6, 50.5 1번 |
| 3 | Persona 생성·수정·활성화 | 22장, 45장 |
| 4 | Content Job 생성 (`queued`) | 46장 |
| 5 | n8n이 Content Job을 선점 (Atomic Claim, 중복 없음) | 20.5, 49장 |
| 6 | Python 브릿지가 Job을 처리 | 19장, 47장 |
| 7 | ComfyUI에서 RTX 5080으로 이미지 생성 | 25.6, 48장 |
| 8 | 결과가 Supabase Storage에 저장, Asset이 DB에 등록 | 19.15, 50장 |
| 9 | Asset 승인·반려 (반려 사유 저장) | 50.5 3번 |
| 10 | Post 생성, **Instagram 또는 X 중 최소 1개**에 게시 | 28·41~43장, 51장 |
| 11 | 게시 결과가 `posts`에 기록 (`external_post_id`, `permalink`) | 11.8 |
| 12 | 성과 수집, Analytics에서 확인 | 29장, 52장 |
| 13 | LLM이 Performance Context를 받아 Structured Decision 생성 | 30장, 53·54장 |
| 14 | Decision Validator 작동 (n8n 조기 거절 + **DB 최종 강제**) | 30.8, 54.5 1번 |
| 15 | AI Decision으로 새 Content Job 생성 (승인 후) | 30.11 |
| 16 | Audit Log (`state_transitions`, `execution_logs`, `ai_decisions`) | 11.14, 30.14 |
| 17 | Emergency Stop 작동 | 32.6, 33.10 |
| 18 | 실패한 Job을 화면에서 확인·재시도 | 17.11, 17.12 |
| 19 | 핵심 실행 모두 Idempotency 적용 | 14.17, 43.8, 54.5 |

## 3. 포함 기능

### 3.1 Authentication
Supabase Auth, Google OAuth, Protected Routes, 세션 유지, 로그아웃. 경로 `/login`, `/dashboard`.

### 3.2 Persona
CRUD (`/personas`, `/personas/:id`). 이 문서의 필수 데이터는 `name`, `slug`, `description`, `personality`, `speaking_style`, `interests`, `background`, `content_rules`, `interaction_rules`, `safety_rules`, `status`다. ⚙️ **생성에는 `visual_settings`(기본 Workflow·Negative Prompt·생성 Parameter)와 참조 이미지·LoRA 등록도 필요**하다 (10.5, 10.6). 상태 값은 `active` / `inactive`.

### 3.3 Content Job
- 종류: `image`만. 플랫폼: **`instagram` 우선**, 공식 API 연동이 먼저 가능하면 `x`.
- 상태는 DB 값이다 (11.3). 이 문서·CLAUDE.md의 이름과 대응:

| 이 문서의 표기 | DB 값 | 비고 |
|---|---|---|
| DRAFT | `draft` | |
| PENDING | `queued` | n8n이 선점하는 대상 |
| GENERATING | `generating` | |
| GENERATED | `ready` | Asset이 `generated`로 등록됨 |
| REVIEW | (상태 아님) | Asset `generated` = 검토 대기, Post `pending_approval` = 게시 승인 대기 |
| APPROVED | Asset `approved` / Post `approved` | Content Job의 상태가 아니다 |
| SCHEDULED, PUBLISHING | Post `scheduled`, `publishing` | |
| PUBLISHED | Content Job `published` (첫 Post 게시 때), Post `published` | |
| FAILED | `failed` | 재시도 대기는 상태가 아니라 Automation Job의 `pending` + 미래 `run_after` |

### 3.4 Image Generation
- ComfyUI 이미지 생성만. **LLM이 ComfyUI Workflow JSON을 만들거나 실행하지 않는다.**
- ⚙️ Workflow는 **Registry(`workflows/registry.json`)의 것만** 쓴다: `image_generation_v1`, `image_generation_lora_v1`, `image_to_image_v1`. 방향별 `portrait_v1`·`square_v1`·`landscape_v1` Workflow는 **만들지 않고 해상도 프리셋**으로 대신한다 (48.3: 세로 4:5 1024×1280, 정사각 1024×1024, 가로 3:2 1536×1024).
- 동적으로 바뀌는 값은 `prompt`, `negative_prompt`, `seed`, `width`, `height`, `steps`, `cfg`, `model`(체크포인트), `lora`, `lora_strength`뿐이다 (Registry의 `params`·`models`).
- 운영 상한: **해상도 한 변 ≤ 1536, steps ≤ 50, 후보 수 1, timeout 900초.** ⚙️ 지금 Registry의 값은 2048·80·4라서, **첫 실제 생성 때 Registry를 이 값으로 맞춘다** (열린 결정 2).

### 3.5 Asset
생성 결과는 **반드시 Asset**으로 등록한다. 보존해야 하는 것: 파일, prompt, negative_prompt, model, LoRA, workflow, workflow_version, seed, generation metadata, `content_job_id`, `persona_id` (+ `automation_job_id`). 위치는 `assets` 칸과 `generation_metadata` (50.3). 재생성은 새 Asset을 만든다 (덮어쓰지 않는다).

### 3.6 Approval
최소 UI: [승인] [반려]. 반려는 사유를 남긴다. Asset 검토(`review_asset`)와 **Post 게시 승인**(`approvals`)은 별개이며, 게시를 허용하는 것은 Post 승인 하나다 (50.4).

### 3.7 SNS Publishing
공식 API 플랫폼 하나를 먼저 끝낸다 (우선 Instagram, 다음 X). 브라우저 자동화는 쓰지 않는다. 흐름:

```text
Approved Asset → Post 초안 → 승인 → 예약 또는 즉시 → n8n → SNS Adapter → External Post ID → 검증 → 기록
```

### 3.8 Analytics
- 필수 지표: `views`, `likes`, `comments`, `shares`, `saves`, `reach`, `followers_delta`, `engagement_rate`. `impressions`는 `raw_metrics`에만 (플랫폼이 `views`로 대체), `engagement_count`는 저장하지 않고 조회에서 계산한다 (52.2).
- ⚙️ 최소 Snapshot은 **24h·168h**이고(PRD 3.6), `LATEST`는 행이 아니라 "가장 큰 `snapshot_hours`"를 읽는 것이다. 기본 설정(1·6·24·48·168)을 그대로 둬도 되고, 줄이려면 `app_settings.analytics.snapshot_hours`만 바꾼다 (52.2).
- 화면: 게시 수, 조회수, 좋아요, 댓글, 참여율, **게시물로 얻은 팔로워**, 최고 성과 게시물.
- 수집 실패는 0이 아니라 "행 없음"이다. 가짜 지표는 만들지 않는다.

### 3.9 AI Decision
- 파이프라인: `Performance → Context Builder → LLM → Structured Decision → Validator → Permission → Approval → Action`.
- **필수 Action: `create_content`, `no_action`.** (`vary_content`는 같은 경로라 허용.)
- ⚙️ `CONTENT_STRATEGY`·`POST_SCHEDULE`(`propose_strategy`·`schedule_post`)는 이 문서의 원안 목록에 있지만, 기술 설계에서 **V2b**(Strategy 저장 35.2, Schedule Engine 30.11)의 부품이 필요하다. **MVP v1.0의 완료 조건에서 제외**하고, 출력 스키마의 허용 목록에도 넣지 않는다 (열린 결정 1).
- 권한 수준: **1 (모든 결정이 승인 대기)로 시작, 상한 2** (Content Job 자동 생성, 게시는 항상 사람 승인). 기본값 0 = AI Run 없음 (30.9, 44.8).

### 3.10 Autonomous Loop
```text
Performance → LLM 분석 → create_content → Content Job → 생성 → Asset → 승인 → Post → Performance
```
반자동(사람 승인 포함)이 우선이다.

### 3.11 Fan Interaction
**MVP v1.0 밖이다.** `conversations`·`messages`·`fan_memories` 테이블은 Sprint 5의 `fan` 마이그레이션(0015)에서 만들고, SNS DM → LLM → 자동 Reply는 **어떤 단계에서도 MVP v1.0 완료 조건이 아니다.** 설계는 31장·55장에 있다.

## 4. MVP v1.0 제외 기능

TikTok, YouTube, Likey, Fantrie, 브라우저 게시, 영상·음성 생성, 라이브, Multi-Persona 자율 최적화, 고급 A/B·실험(34장), 자동 Memory 학습, 팬 관계 자동 관리, 자동 LoRA·모델 학습, Multi-GPU·Cloud GPU, 캠페인 관리, 수익 최적화, 유료 광고 자동화, Level 3 이상 권한, 자동 게시(Level 4).

## 5. 아키텍처

```text
Lovable → Supabase (Auth / DB / Storage / Realtime) → n8n → Python (127.0.0.1) → ComfyUI (RTX 5080)
LLM = 의사결정 계층에서만 (Context → Decision → Validator)
```

## 6. 절대 원칙 (CLAUDE.md와 같다)

1. Frontend는 실행 시스템을 직접 호출하지 않는다 (Lovable → Supabase → n8n → Python → ComfyUI).
2. LLM이 Tool을 직접 실행하지 않는다.
3. 모든 실행은 Job 기반이다.
4. 외부 API 호출은 멱등이다.
5. 모든 AI Action은 Validator를 통과한다.
6. Emergency Stop이 모든 자율 Action보다 우선한다.
7. 비밀값은 Frontend에 없다.
8. RLS를 우회하지 않는다.
9. 실패를 숨기지 않는다.
10. 가짜 Metric을 만들지 않는다.

## 7. 개발 우선순위

```text
1 Authentication → 2 Persona → 3 Content Job → 4 Generation → 5 Asset → 6 Approval → 7 Publishing
 → 8 Analytics → 9 AI Decision → 10 Autonomous Loop → 11 Fan Interaction (MVP v1.0 밖)
```

시간이 모자라면 10에서 멈춘다. Sprint와의 대응은 0장의 표다 (44.5~44.8).

## 8. MVP v1.0 Definition of Done

[MVP_CHECKLIST.md](MVP_CHECKLIST.md)의 2번 표가 이 문서의 17번 목록(24개)이다. 하나라도 비어 있으면 MVP v1.0이 아니다.

## 9. 열린 결정 (사람이 정한다)

| # | 결정 | 이 문서가 지금 쓴 값 | 이유 |
|---|---|---|---|
| 1 | `CONTENT_STRATEGY`·`POST_SCHEDULE`를 MVP v1.0에 넣을까 | **넣지 않는다 (V2b)** | 각각 Strategy 버전 저장(35.2)·Schedule Engine이 필요하고, 사이클(1장)은 `create_content`만으로 닫힌다. 넣으려면 Sprint 4에 V2b의 DB·화면이 들어온다 |
| 2 | 운영 상한(1536·50·1)을 Registry에 반영할 시점 | 첫 실제 생성 때 | 48.3이 "Registry 값은 첫 실제 생성의 VRAM·시간을 보고 정한다"고 했다. `tests/bridge`의 OOM·시간 초과 시험(2048)이 이 값에 기대므로 테스트와 함께 바꾼다 |
| 3 | 권한 수준 이름 | 설계의 **0~5**(15.19)를 쓰고 MVP 기본은 **1**(전부 승인 대기), 상한 2 | 이 문서 초안의 `L2 Draft + Approval`은 설계의 수준 1에 해당한다 |
| 4 | Instagram과 X 중 먼저 할 것 | Instagram | 설계(28장, Sprint 2). X는 API 등급·요금 확인이 먼저다 (41.5) |
