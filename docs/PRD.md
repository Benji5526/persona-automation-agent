# PRD: persona-automation-agent

| 항목 | 내용 |
|---|---|
| 상태 | v1.0 1차 완성 (기술 설계 단계로 이동) |
| 최종 수정 | 2026-10-05 |
| 진행 | 1. Product Overview ✅ · 2. Problem Definition ✅ · 3. Goal & Success Metrics ✅ · 4. Target User ✅ · 5. Core Features ✅ · 6. Automation Scenarios ✅ · 7. Scope ✅ · 8. Future Roadmap ✅ |

---

## 1. Product Overview ✅

### 제품 유형

**Autonomous Virtual Influencer Operating Platform** (자율형 버추얼 인플루언서 운영 플랫폼)

이 제품은 단순한 "AI 이미지 생성 자동화"가 아닙니다. 버추얼 인플루언서 한 명을 **하나의 AI Agent이자 운영 단위**로 보고, 그 인플루언서의 콘텐츠·팬·SNS·기억·성과를 계속 관리하는 시스템입니다.

### Product Vision

> AI 버추얼 인플루언서가 스스로 콘텐츠를 기획하고, 생성하고, 게시하며, 팬과 상호작용하고, 데이터를 분석하여 다음 행동을 결정할 수 있는 자율형 운영 플랫폼을 구축한다.

### 핵심 구조

```text
                 ┌──────────────────────┐
                 │   Virtual Persona    │
                 │      AI Agent        │
                 └──────────┬───────────┘
          ┌─────────────────┼─────────────────┐
          ↓                 ↓                 ↓
       CONTENT           SOCIAL           AUDIENCE
      콘텐츠 생성        SNS 운영          팬 / CRM
          ↓                 ↓                 ↓
       ComfyUI         n8n / APIs         Messages
          └─────────────────┼─────────────────┘
                            ↓
                       Analytics
                            ↓
                   AI Decision Engine ──→ 다음 행동
```

### 핵심 Loop

```text
Observe → Think → Create → Publish → Interact → Measure → Learn → Decide → (Create …)
```

데이터를 보고, AI가 판단하고, 콘텐츠를 만들고, 게시하고, 팬과 상호작용하고, 결과를 분석한 뒤, 다시 다음 행동을 결정하는 순환 구조입니다.

### 구성요소별 역할

n8n이 AI 자체는 아닙니다. 각 구성요소의 책임을 아래처럼 나눕니다.

| 구성요소 | 비유 | 역할 |
|---|---|---|
| Lovable | Control Center | 사람이 AI Agent를 관리하는 대시보드 |
| Supabase | Memory / Database | 모든 상태, 데이터, Asset의 Source of Truth |
| n8n | Nervous System | Agent의 행동을 실행하는 Orchestrator |
| Python | Local Execution Layer | 로컬 GPU와 외부 시스템을 잇는 백엔드 |
| ComfyUI | Visual Engine | 이미지·영상 생성 엔진 |
| LLM | Brain | 판단, 기획, 텍스트 생성, 대화 |
| SNS API | – | 콘텐츠 게시와 데이터 수집 |
| RTX 5080 | – | 로컬 AI 렌더링 |

### 목표

- **최종 목표:** 버추얼 인플루언서의 콘텐츠 제작, SNS 운영, 팬 상호작용, CRM, 기억, 성과 분석, 다음 행동 결정을 하나의 자동화 시스템으로 통합한다.
- **단계별 목표:** 기능을 한꺼번에 만들지 않고 `MVP → V1 → V2 → Long-term` 순서로 넓힌다 (7번 Scope).
  - **MVP:** 콘텐츠 생성 자동화 파이프라인. `Google Login → Persona → Content Job → n8n → Python → ComfyUI → Storage → Asset Library`
  - **V1:** 실제 SNS 운영. 승인 → Instagram 게시 → 성과 수집
  - **V2:** AI가 무엇을 만들지 결정. 성과 분석, AI 기획, 기본 팬 상호작용
- **장기 목표:** 팬 상호작용과 성과 데이터를 바탕으로 AI Agent가 다음 콘텐츠와 행동을 스스로 결정하는 Closed-loop 시스템을 만든다.

---

## 2. Problem Definition ✅

현재 버추얼 인플루언서 운영에서는 이미지·영상 제작, SNS 게시, 팬과의 대화, 콘텐츠 일정 관리, 데이터 분석이 모두 따로 이루어집니다. 그래서 사람이 이 작업들을 반복해서 연결하고 관리해야 합니다. 콘텐츠를 계속 운영하려고 하면 아래 문제가 생깁니다.

### Problem 1: 콘텐츠 제작의 반복성

버추얼 인플루언서의 활동을 유지하려면 새 이미지와 영상을 계속 만들어야 합니다. 지금은 사람이 아래 과정을 반복합니다.

```text
아이디어 → 프롬프트 작성 → 이미지 생성 → 수정 → 이미지 선택 → 영상 제작 → 파일 정리
```

그래서 콘텐츠 생산량이 늘어날수록 운영자의 작업량도 같이 늘어납니다.

### Problem 2: 콘텐츠와 Persona의 일관성

버추얼 인플루언서는 이미지를 만드는 데서 끝나지 않고, 하나의 일관된 캐릭터로 인식되어야 합니다. 그런데 여러 AI 도구와 workflow를 사람이 하나씩 관리하면 콘텐츠마다 아래 요소가 달라질 수 있습니다.

- 외형
- 말투
- 성격
- 선호도
- 세계관
- 콘텐츠 스타일

따라서 Persona를 한곳에서 정의하고, 모든 AI 작업이 그 정의를 재사용하는 구조가 필요합니다.

### Problem 3: SNS 운영의 분절

콘텐츠를 만든 뒤에도 사람이 직접 아래 과정을 반복합니다.

```text
파일 선택 → Caption 작성 → Hashtag 작성 → 게시 시간 결정 → SNS 업로드 → 게시 결과 확인
```

SNS가 여러 개로 늘어나면 같은 콘텐츠를 플랫폼마다 다시 처리해야 하므로 운영이 더 복잡해집니다.

### Problem 4: 팬과의 상호작용이 따로 관리됨

댓글이나 DM이 오면 각 플랫폼에 들어가서 확인하고 답해야 합니다. 이 방식으로는 AI가 과거 대화나 팬의 특성을 계속 활용하기 어렵습니다. 예를 들어 팬 한 명에 대한 아래 정보를 하나의 Customer/Conversation Context로 관리할 필요가 있습니다.

```text
Fan A
 ├─ 처음 대화
 ├─ 관심사
 ├─ 과거 질문
 ├─ 이전 대화
 └─ 최근 interaction
```

### Problem 5: 데이터가 다음 행동으로 연결되지 않음

일반적인 콘텐츠 운영은 대개 여기서 끝납니다.

```text
게시 → 조회수 / 좋아요 / 댓글 → 사람이 확인
```

우리 시스템에서는 이 데이터가 다음 콘텐츠로 이어져야 합니다.

```text
게시 → Performance Data → AI Analysis → Pattern Detection → Next Content Decision → New Content
```

즉, 데이터는 단순한 기록에 그치지 않고 다음 행동의 입력값이 되어야 합니다.

### Problem 6: 여러 자동화 시스템을 연결하기 어려움

이미지 생성, DB, SNS, AI 모델, 일정 관리를 각각 별도의 도구로 만들면 시스템 사이의 연결이 복잡해집니다.

```text
ComfyUI · Supabase · n8n · Python · LLM · SNS · Storage
```

각 시스템의 상태를 사람이 관리하면 자동화가 복잡해집니다. 장애가 나도 어느 단계에서 문제가 생겼는지 찾기 어렵습니다. 따라서 **하나의 Job ID를 중심으로 전체 작업 상태를 추적하는 구조**가 필요합니다.

### 핵심 Problem Statement

> 버추얼 인플루언서를 계속 운영하려면 콘텐츠 제작, Persona 관리, SNS 게시, 팬과의 상호작용, 데이터 분석 같은 반복 작업이 필요하다. 지금은 이 작업들이 여러 도구와 플랫폼에 흩어져 있고, 사람이 직접 연결하고 있다.
>
> persona-automation-agent는 이렇게 분리된 작업과 데이터를 하나의 시스템으로 통합한다. 그래서 버추얼 인플루언서가 콘텐츠를 생성하고, 게시하고, 팬과 상호작용하고, 성과를 분석한 뒤 다음 행동을 결정하는 **Closed-loop Automation**을 만드는 것을 목표로 한다.

### MVP 문제 범위

최종 제품은 자율형 Agent입니다. 다만 문제를 처음부터 너무 크게 잡지 않습니다. 먼저 아래 Loop를 안정적으로 만듭니다. `Persona → … → Asset`까지가 MVP, `SNS Post → Performance`가 V1입니다 (7번 Scope).

```text
Persona → Content Job → ComfyUI → Asset → SNS Post → Performance
```

| Problem | 다루는 단계 |
|---|---|
| P1 콘텐츠 제작 | **MVP** Content Job 큐 기반 자동 생성 |
| P2 Persona 일관성 | **MVP** Persona를 한곳에서 정의하고 생성 작업에 재사용 (외형·스타일·기본 Context) |
| P3 SNS 운영 | **MVP** Post 구조와 Caption 생성 → **V1** Instagram 예약·게시 (게시 전 사람 승인) |
| P4 팬 상호작용 | **V1** 기본 → **V2** 자동 응답·Fan Memory |
| P5 데이터 → 행동 | **V1** Performance 수집 → **V2** 분석과 다음 행동 결정 |
| P6 시스템 연결 | **MVP** Job ID 하나로 상태 추적 (V1부터 Performance까지 확장) |

### 확정된 결정 (2026-10-03)

| 항목 | 결정 | 영향 |
|---|---|---|
| 대상 사용자 | **1인 운영자**, 페르소나 1~3명 | MVP에서 팀 계정·권한·역할 기능 제외. 대시보드 로그인은 운영자 1명 기준 |
| 첫 플랫폼 | **Instagram** | Graph API 사용. 비즈니스·크리에이터 계정과 Meta 앱 필요. 게시 미디어는 공개 URL이어야 함 (현재 공개 `media` 버킷과 맞음) |
| 사람 개입 | **게시 전 사람 승인** | `posts`에 승인 단계 필요 (예: `draft → pending_approval → approved → scheduled`). 승인되지 않은 게시물은 자동 게시하지 않음. 실제 게시가 시작되는 **V1부터 적용** |

---

## 3. Goal & Success Metrics ✅

### Goal

> persona-automation-agent는 버추얼 인플루언서의 콘텐츠 생성, SNS 운영, 팬 Interaction, 데이터 분석을 자동화한다. 장기적으로는 수집된 데이터를 바탕으로 다음 행동을 스스로 결정하는 Closed-loop AI Agent 시스템을 구축한다.

### 3.1 Goal 1: 콘텐츠 제작 자동화

운영자가 ComfyUI를 직접 실행하거나 파일을 관리하지 않아도, 시스템이 콘텐츠 생성 작업을 자동으로 처리해야 한다.

```text
Content Request → Supabase Queue → n8n → Python Worker → ComfyUI / RTX 5080 → Generated Asset → Supabase Storage
```

- 이미지 생성 자동화
- 영상 생성 구조 지원
- FaceSwap workflow 지원
- 생성 결과 자동 저장
- 작업 상태 자동 업데이트

### 3.2 Goal 2: 중앙화된 Persona 관리

버추얼 인플루언서의 핵심 정보를 하나의 Persona 데이터로 관리한다. Persona에는 앞으로 다음 정보가 들어간다.

```text
Identity
├── Name
├── Description
├── Appearance
├── Personality
├── Speaking Style
├── Interests
├── Background
└── Rules

Visual Identity
├── Base Model
├── LoRA
├── Face Reference
├── Style
└── Workflow

Behavior
├── Content Preferences
├── Interaction Rules
├── Response Style
└── Safety Rules
```

같은 Persona 정보를 콘텐츠 생성, Caption 생성, 팬과의 대화, 의사결정에 재사용할 수 있어야 한다.

### 3.3 Goal 3: SNS 운영 자동화

사람이 파일을 내려받아 다시 올리지 않아도, 생성된 콘텐츠가 게시 workflow로 넘어가야 한다.

```text
Asset Ready → Caption Generation → Post Scheduling → SNS API → Published
```

- 콘텐츠 예약
- Caption 생성
- 플랫폼별 콘텐츠 변환
- 게시 결과 기록
- 게시 실패 감지
- Retry

### 3.4 Goal 4: 팬 Interaction 관리

팬과의 대화를 하나의 데이터 구조로 관리한다.

```text
Fan → Conversation → Messages → Persona Context → AI Response
```

AI가 필요한 범위에서 이전 conversation context를 참고해, 일관된 Persona로 응답하는 구조를 만든다.

### 3.5 Goal 5: Closed-loop Automation

장기적으로 가장 중요한 목표다.

```text
Observe → Analyze → Decide → Create → Publish → Interact → Measure → Learn → (다시 Observe)
```

시스템은 "사람이 시키면 생성하는 자동화 도구"에서 끝나지 않고, **데이터를 바탕으로 다음 작업을 결정하는 Agent 시스템**으로 발전하는 것을 목표로 한다.

### 3.6 MVP Success Criteria

MVP에서는 숫자를 많이 잡지 않는다. 대신 **End-to-End 자동화가 실제로 동작하는지**를 기준으로 판단한다. 단계 정의는 7번 Scope를 따른다.

| 단계 | 범위 | 완료 지점 |
|---|---|---|
| **MVP** | 콘텐츠 생성 자동화 | `Queue → Generation → Storage → Ready` |
| **V1** | 게시와 성과 수집 | `Ready → 승인 → Instagram 게시 → Performance` |

#### MVP: 생성 → Storage → Ready

1. Supabase Queue에 작업을 생성할 수 있다.
2. n8n이 작업을 자동으로 감지하고 Python Worker를 호출한다.
3. Python Worker가 ComfyUI를 통해 로컬 RTX 5080에서 콘텐츠를 생성한다.
4. 생성된 결과가 Supabase Storage에 자동으로 업로드된다.
5. 작업 상태가 자동으로 업데이트된다.
6. 실패한 작업을 기록하고 Retry할 수 있다.
7. 생성된 Asset을 SNS Publishing workflow로 전달할 수 있다.
8. 전체 Job을 하나의 ID로 추적할 수 있다.

| 영역 | 성공 기준 |
|---|---|
| Queue | DB에 생성 작업을 등록할 수 있음 |
| Orchestration | n8n이 Queue 작업을 자동으로 처리 |
| GPU | Python이 ComfyUI에 Workflow 전달 |
| Generation | ComfyUI가 정상적으로 Asset 생성 |
| Storage | 생성 결과가 Supabase Storage에 저장 |
| State | DB 상태가 `pending → processing → done`으로 자동 변경 |
| Error | 실패한 Job을 `failed`로 기록 |
| Retry | 실패한 Job을 다시 처리할 수 있음 |
| Post | 생성된 Asset을 Post workflow로 전달 |
| Tracking | 하나의 Job ID로 전체 과정 추적 가능 |

> **MVP 목표:** 정상적인 콘텐츠 생성 Job은 운영자의 수동 개입 없이 `Queue → Generation → Storage → Ready`까지 완료된다.

#### V1: 승인 → Instagram 게시 → 성과 수집

1. Ready 상태의 Asset으로 LLM이 Persona 말투의 Caption·Hashtag 초안을 만든다.
2. 운영자가 대시보드에서 게시물을 승인·수정·반려할 수 있다. 승인되지 않은 게시물은 게시되지 않는다.
3. 승인된 게시물이 예약 시각에 Instagram에 자동 게시되고, 게시물 ID가 기록된다.
4. 게시 실패를 감지하고 Retry한다.
5. 게시 후 정해진 시점(24시간, 7일)에 Instagram 성과 지표를 수집해 저장한다.
6. 하나의 Job ID로 생성 → 승인 → 게시 → 성과까지 추적할 수 있다.

> **V1 목표:** 페르소나 1명이 **주 7개(하루 1개) 게시를 4주 연속** 유지하고, 운영자는 승인과 수정만 한다.

### 3.7 핵심 KPI

MVP가 안정화되면 다음 값을 측정한다.

| # | KPI | 정의 | 목표 |
|---|---|---|---|
| ① | Automation Completion Rate | 완료된 자동화 Job / 전체 Job × 100 | **≥ 95%** |
| ② | Human Intervention Rate | 콘텐츠 하나를 만드는 동안 사람이 직접 개입한 횟수 | 정상 Job은 **개입 없이** 생성 → Storage 저장까지 완료 |
| ③ | Average Job Processing Time | Job 생성부터 `done`까지 걸린 시간 | 처음에는 workflow별 기준값을 측정하고, 이후 최적화 |
| ④ | Failure Recovery Rate | **재시도 대상 오류**(시간 초과, 연결 끊김, GPU OOM 등)로 실패한 Job 중 자동 Retry로 정상 완료된 비율. 설정 오류(템플릿·모델 파일 없음 등)는 재시도해도 성공할 수 없으므로 분모에서 제외 | **≥ 90%** (무한 Retry 금지, `max_attempts`로 제한) |
| ⑤ | End-to-End Success Rate | `Content Request → Generation → Storage → Post` 전체가 자동으로 완료된 비율 (가장 중요한 지표) | V1 이후 **≥ 90%** |

### 3.8 장기 Success Metrics

최종적으로는 기술적인 자동화 성공뿐 아니라 Agent 자체의 운영 효율도 측정한다.

| 분류 | 지표 |
|---|---|
| Operational | 하루 자동 생성 콘텐츠 수, 주간 자동 게시 수, manual intervention 횟수, 평균 콘텐츠 생성 시간, 실패율, Retry율 |
| Audience | Engagement Rate, 댓글 수, DM 수, 재방문 사용자, Conversation 수 |
| Content | 콘텐츠별 조회수, 좋아요, 댓글, 공유, 저장, 콘텐츠 유형별 성과 |
| Agent | AI가 독립적으로 수행한 작업 비율, AI Decision → Execution 성공률, 잘못된 작업 발생률, Human Approval 필요 비율 |

### 3.9 최종 Product Success

```text
운영자
  │  Persona / Rules / Goals 설정
  ↓
AI Agent
  ├── 콘텐츠 계획
  ├── 이미지/영상 생성
  ├── Caption 생성
  ├── 게시 예약
  ├── 팬 Interaction
  ├── 성과 분석
  └── 다음 행동 결정
          ↓
      Automation → Data ──→ AI Agent
```

운영자는 모든 작업을 직접 하는 사람에서 **Persona와 목표를 설정하고 AI Agent의 행동을 감독하는 Operator**가 된다.

> **장기 목표:** `Observe → Analyze → Decide → Create → Publish → Interact → Measure → Learn`의 Closed-loop를 구축해, AI Agent가 지속적인 운영 작업을 수행하도록 한다.

---

## 4. Target User ✅

> persona-automation-agent의 **직접적인 고객은 Operator**이고, **AI Agent는 실행 주체**이며, **Fan은 시스템이 최종적으로 서비스를 제공하는 대상**이다.

### 4.1 Primary User: Operator / Creator

버추얼 인플루언서를 운영하는 실제 사용자다. MVP에서는 1인 운영자를 기준으로 한다 (2번 확정 결정).

**주요 역할**

- Persona 생성과 관리
- 콘텐츠 제작 목표 설정
- 콘텐츠 생성 요청
- SNS 게시 일정 관리
- 생성된 콘텐츠 검수
- 성과 데이터 확인
- AI Agent의 자동화 결과 확인
- 게시 전 승인, 예외 상황 개입

**주요 Pain Point**

- 콘텐츠 제작에 반복적으로 시간이 든다
- 이미지·영상·SNS·AI 도구가 여러 시스템에 흩어져 있다
- Persona의 스타일과 성격을 계속 유지하기 어렵다
- SNS 게시와 팬 응대가 반복적인 수작업이다
- 콘텐츠 성과가 다음 콘텐츠 제작으로 이어지지 않는다
- 자동화 과정에서 현재 작업 상태와 오류 원인을 확인하기 어렵다

**User Goal**

> "내가 매번 직접 작업하지 않아도, AI가 정해진 Persona와 운영 목표에 따라 버추얼 인플루언서를 운영하게 한다."

### 4.2 Secondary User: AI Agent

사람이 직접 쓰는 사용자는 아니지만 시스템의 핵심 Actor다. AI Agent는 Operator가 설정한 Persona, 목표, 규칙, 과거 데이터를 바탕으로 다음 행동을 수행한다.

**주요 역할**

- 콘텐츠 제작 계획 수립
- 콘텐츠 생성 Job 생성
- 생성된 Asset 평가
- SNS 게시 계획 수립
- Caption과 콘텐츠 설명 생성
- 게시 작업 실행
- 성과 데이터 분석
- 팬 메시지에 대한 응답 생성
- 다음 콘텐츠와 행동 결정
- 실패한 작업의 재시도 또는 대체 작업 결정

**핵심 원칙**

AI Agent는 단순한 Content Generator가 아니다. `Observe → Analyze → Decide → Create → Publish → Interact → Measure → Learn`의 전체 Loop를 수행하는 시스템 Actor로 정의한다.

단, 시스템 안정성과 안전을 위해 **Operator가 설정한 권한과 Rule을 넘어서는 행동은 하지 않는다.** 게시가 시작되는 V1부터 이 권한은 "게시 전 사람 승인"이다. AI Agent는 게시물을 준비하고 예약까지 할 수 있지만, 승인되지 않은 게시물은 게시하지 않는다.

### 4.3 External User: Fan / Audience

SNS에서 버추얼 인플루언서와 상호작용하는 외부 사용자다.

**주요 행동**

- 콘텐츠 조회
- 좋아요, 댓글, 공유
- DM 전송
- Persona와 대화
- 콘텐츠에 대한 반응
- 반복 방문과 상호작용

**시스템에서의 역할**

Fan의 행동과 메시지는 단순한 SNS 활동 기록이 아니라, AI Agent가 앞으로의 행동을 결정하는 Input으로 쓰인다.

```text
Fan Comment → Message / Interaction → Persona Context → AI Response → Fan Reaction → Performance Data → Future Decision
```

### 4.4 Future User: Multi-Persona Operator

앞으로 한 명의 Operator가 여러 버추얼 인플루언서를 운영할 수 있도록 확장한다.

```text
Operator
   ├── Persona A ── Content · SNS · Fans
   ├── Persona B ── Content · SNS · Fans
   └── Persona C ── Content · SNS · Fans
```

이를 위해 데이터베이스와 시스템 구조는 처음부터 `user → persona → content → social account` 구조를 고려한다.

> MVP 데이터 구조는 처음부터 Persona 여러 개(1~3명)를 담을 수 있게 만든다. 다만 V1 성공 판정과 화면 설계는 Persona 1명 운영을 기준으로 한다. 많은 Persona를 한 화면에서 관리하는 기능이 P2다.

### 4.5 MVP User Priority

| Priority | Actor | 역할 |
|---|---|---|
| P0 | Operator | Persona와 자동화 설정, 게시 승인 |
| P0 | AI Agent | 콘텐츠 생성과 Workflow 실행 |
| P1 | Fan / Audience | SNS 상호작용 (팬 Interaction은 V1 기본, V2 고도화) |
| P2 | Multi-Persona Operator | 여러 Persona 관리 |

### 4.6 핵심 사용 시나리오와 단계

| 단계 | 흐름 | 범위 |
|---|---|---|
| 1 | Operator가 Persona 설정 | MVP |
| 2 | 운영 목표 설정 | MVP |
| 3 | Content Job 생성 | MVP |
| 4 | AI Agent → n8n → Python → ComfyUI | MVP |
| 5 | Asset 생성 → Storage → Ready | MVP |
| 6 | Caption 생성 → Operator 승인 → SNS Publish | V1 (Caption 생성은 MVP) |
| 7 | Performance 수집 | V1 |
| 8 | AI Agent 분석 → 다음 Content Job 결정 | V2 (Closed-loop) |

---

## 5. Core Features ✅

> **핵심 결정:** `Content Job`을 시스템의 중심 객체로 둔다. `media_queue`(생성 실행 큐)만 중심에 두면 기획 → 생성 → SNS → 성과 → AI 의사결정을 연결하기 어렵다. 그래서 `media_queue`는 Content Job 아래의 **실행 단위(Generation Job)**가 된다.

### 5.0 기능별 단계 요약

| # | Feature | MVP | V1 | V2 이후 |
|---|---|---|---|---|
| 5.1 | Persona Management | Name, Profile Image, Description, Personality, Speaking Style, Interests, Content Rules, Visual Settings (Base Model, LoRA, Face Reference, Workflow, Negative Prompt, 파라미터) | – | 세계관·관계, Interaction Rules, Safety Rules 고도화 |
| 5.2 | Content Planning | Operator가 Content Job 직접 생성 (Image 우선, Video는 Job 구조만) | 일정(Schedule) 기반 자동 생성 | AI Agent가 기획 (V2) |
| 5.3 | AI Content Generation | **AI Prompt Generation**, Text→Image, Image→Image, LoRA, 기본 Character Reference, **FaceSwap 기본 workflow**, 자동 저장 (✅ 브릿지 구현됨) | Video Generation, FaceSwap 고도화, Upscaling | 고급 Character Consistency, 자동 Video Editing, Background Processing |
| 5.4 | Asset Management | Asset 저장·메타데이터, Asset Library (썸네일·미리보기·필터·다운로드) | Asset 검수(승인·반려) | – |
| 5.5 | SNS Management | SNS Account·Post 데이터 구조, Asset → Post 연결, Caption 생성, Platform Adapter 인터페이스 | Instagram 실제 게시, Hashtag 생성, 예약·즉시 게시, 실패 처리·Retry | 다른 플랫폼, Platform Formatter |
| 5.6 | Fan Interaction | – | 기본 (수집) | 자동 응답, Relationship Management |
| 5.7 | Memory System | Operational Memory (Job·생성 이력) | Operational Memory (게시·성과) | Fan Memory (V2), Persona Memory 고도화 |
| 5.8 | Performance Analytics | 기본 (Job·생성 통계) | 지표 수집 (24시간·7일), 기본 Analytics | AI 분석·패턴 탐지 (V2), 예측·A/B 테스트 |
| 5.9 | AI Decision Engine | – | – | ✅ V2부터, 장기 핵심 |
| 5.10 | Automation & Job Management | Atomic Claim, Retry, 실패 기록 (✅ 구현됨) | 게시 Job 포함 | Execution Log, 여러 Worker |
| 5.11 | Notification & Approval | 생성 실패 기록·Dashboard 표시 | **모든 게시물 사전 승인** (2번 확정 결정), 실패 알림 | Risk Check 기반 선택적 승인 |
| 5.12 | Dashboard | Google Login, Personas, Content Jobs, Asset Library, Active Jobs, Failed Jobs, System Status | Approvals, SNS Accounts, Published Posts, 기본 Analytics | Conversations, 고급 Analytics |

### 5.1 Persona Management

버추얼 인플루언서의 정체성과 행동 규칙을 한곳에서 관리한다.

**관리 항목:** 이름, 프로필 이미지, 성별·연령대, 외형 특징, 성격, 말투, 관심사, 취미, 배경 설정, 세계관, 선호 콘텐츠, 금지 콘텐츠, 행동 규칙, 응답 규칙, 안전 규칙

**Visual Identity:** Base Model, LoRA, Face Reference, Style Reference, Character Reference, ComfyUI Workflow, Negative Prompt, Generation Parameters

**핵심 원칙:** 모든 콘텐츠 생성과 팬 응답은 해당 Persona의 Context를 참조해야 한다.

```text
Persona
 ├── Identity
 ├── Personality
 ├── Speaking Style
 ├── World
 ├── Visual Identity
 ├── Content Rules
 └── Interaction Rules
```

### 5.2 Content Planning

AI Agent 또는 Operator가 콘텐츠 제작 작업을 만든다. 모든 콘텐츠 제작 작업은 하나의 `Content Job`으로 관리한다.

```text
Content Job
├── Persona
├── Content Type
├── Goal
├── Topic
├── Prompt
├── Platform
├── Schedule
├── Priority
└── Status
```

**지원 콘텐츠:** Image, Video, Short-form Video, Story, Carousel, Text Post, AI-generated Caption

**상태:** 5.15에서 정의한다.

### 5.3 AI Content Generation

Content Job을 실제 미디어 Asset으로 바꾼다.

```text
Content Job → AI Prompt Generation → ComfyUI Workflow Selection → Python Execution → RTX 5080 / ComfyUI → Image / Video → Asset
```

**지원 기능:** Text→Image, Image→Image, Character Reference, LoRA 적용, Face Reference, Style Reference, Image→Video, Video Processing, FaceSwap, Upscaling, Background Processing, 자동 파일 저장

ComfyUI Workflow를 바꾸더라도 상위 시스템은 같은 `Content Job → Asset` 구조를 유지한다. (현재 브릿지는 `workflows/<이름>.json` 템플릿을 교체하는 방식으로 이를 지원한다.)

### 5.4 Asset Management

생성된 이미지와 영상을 한곳에서 관리한다. 기본 저장소는 Supabase Storage다.

**Asset 정보:** Asset ID, Persona ID, Content Job ID, File Type, File Path, Storage URL, Thumbnail, Generation Prompt, Workflow, Model, LoRA, Seed, Generation Parameters, Created At, Status

**상태:** 5.15에서 정의한다.

### 5.5 SNS Management

생성된 콘텐츠를 SNS에 자동으로 게시한다.

**지원 기능:** SNS 계정 연결, 플랫폼별 계정 관리, Caption 생성, Hashtag 생성, 게시 시간 설정, 예약 게시, 즉시 게시, 게시 결과 확인, 게시 실패 처리, Retry

```text
Asset → Caption Generator → Platform Formatter → Scheduler → SNS API → Published Post
```

플랫폼마다 특성이 다르므로 Caption 하나를 그대로 쓰지 않고, AI Agent가 플랫폼에 맞게 변환한다.

### 5.6 Fan Interaction

SNS에서 생기는 팬과의 상호작용을 관리한다.

**수집 데이터:** Comment, DM, Reply, Mention, Reaction, User Profile Context, Conversation History

```text
Fan Message → Message Collector → Conversation → Persona Context → Memory → LLM → Response → SNS
```

AI Agent는 현재 메시지만 보고 답하지 않는다. 가능한 경우 아래 정보를 함께 쓴다.

```text
Current Message + Conversation History + Fan Memory + Persona + Interaction Rules + Recent Content
```

### 5.7 Memory System

AI Agent가 과거 정보를 활용할 수 있도록 Memory를 관리한다. Memory의 Source of Truth는 Supabase다.

| Memory | 내용 |
|---|---|
| Persona Memory | 버추얼 인플루언서 자체의 장기 정보: Personality, Preferences, World, Relationships, Rules |
| Fan Memory | 특정 Fan과의 관계와 과거 상호작용: Interests, Previous Conversations, Preferences, Important Events, Relationship Context |
| Operational Memory | 시스템 운영 정보: Previous Jobs, Generation History, Published Posts, Performance, Failures, Decisions |

### 5.8 Performance Analytics

SNS에서 생긴 데이터를 수집하고 분석한다.

**수집 데이터:** Views, Likes, Comments, Shares, Saves, Reach, Engagement, Followers, Profile Visits, DM, Link Clicks

**분석 기준:** `Persona → Platform → Content Type → Topic → Posting Time → Performance`

```text
Reel · Topic: Travel · Duration: 12 sec · Posting Time: 20:00
        ↓
Views · Likes · Comments · Shares · Saves · Engagement Rate
```

### 5.9 AI Decision Engine

장기적으로 시스템의 핵심 기능이다. AI Agent가 수집된 데이터를 바탕으로 다음 행동을 결정한다.

- **Input:** Persona + Content History + Performance + Fan Interaction + Current Goals + Rules
- **질문:** "What should happen next?"
- **가능한 Decision:** 새 콘텐츠 생성, 기존 콘텐츠 변형, 특정 주제 반복, 새 주제 테스트, 게시 시간 변경, Caption 변경, 특정 Fan에게 응답, 콘텐츠 생성 중단, 실패 Job Retry, Operator 승인 요청
- **구조:** `Observe → Analyze → Decide → Execute`

### 5.10 Automation & Job Management

모든 자동화 작업을 하나의 Job 시스템으로 관리한다.

**Job 정보:** Job ID, Job Type, Persona ID, Content Job ID, Current Status, Priority, Retry Count(`attempts` / `max_attempts`), Error, Started At, Completed At, Worker, Execution Log

**상태:** 5.15에서 정의한다.

**중요한 원칙:** 같은 Job을 여러 Worker가 동시에 실행하지 않도록 **Atomic Job Claim**을 쓴다. Worker A가 선점한 Job은 Worker B가 가져갈 수 없다. (✅ `claim_media_job()` / `claim_next_media_job()`으로 구현됨)

### 5.11 Notification & Approval

완전 자동화가 아닌 경우 Operator에게 개입을 요청한다.

**Approval이 필요한 상황:** 민감한 콘텐츠, Persona Rule 위반 가능성, SNS API 오류, 반복적인 Generation 실패, 새로운 콘텐츠 전략, 중요한 Fan Interaction, 시스템이 판단하기 어려운 상황

```text
AI Decision → Risk Check ─┬─ Safe ──────────────→ Execute
                          └─ Requires Approval ─→ Operator → Approve → Execute
```

> **V1 규칙:** 게시가 시작되는 V1에서는 Risk Check 결과와 상관없이 **모든 게시물이 Requires Approval 경로**로 간다 (2번 확정 결정). Risk Check로 일부를 자동 게시하는 것은 V2 이후다.

### 5.12 Dashboard

Lovable로 전체 시스템을 관리한다.

```text
┌─────────────────────────────────────┐
│ Overview                            │
├──────────┬──────────┬───────────────┤
│ Persona  │ Jobs     │ Published     │
│ 1        │ 24       │ 18            │
├──────────┴──────────┴───────────────┤
│ Content Performance (Graph)         │
├─────────────────────────────────────┤
│ Active Jobs                         │
│ Job #1024   Generating    72%       │
│ Job #1025   Publishing    ✓         │
│ Job #1026   Failed        Retry     │
├─────────────────────────────────────┤
│ Approval Required                   │
│ Content #204                        │
│ [Review] [Approve] [Reject]         │
└─────────────────────────────────────┘
```

**주요 화면:** Dashboard, Persona, Content Jobs, Asset Library, SNS Accounts, Published Posts, Conversations, Analytics, Automation Jobs, Approvals, Settings

> Lovable은 브라우저에서 동작하므로 `service_role` 키를 쓸 수 없다. Dashboard를 붙이기 전에 Supabase Auth 로그인과 Operator용 RLS 정책이 필요하다. (현재 스키마는 `service_role` 전용으로 잠겨 있다.)

### 5.13 System Architecture Mapping

| Feature | Lovable | Supabase | n8n | Python | ComfyUI | LLM |
|---|---|---|---|---|---|---|
| Persona | UI | DB | – | – | – | ✓ |
| Content Planning | UI | DB | ✓ | – | – | ✓ |
| Generation | UI | Job | ✓ | ✓ | ✓ | ✓ |
| Asset | UI | Storage | ✓ | ✓ | – | – |
| SNS | UI | DB | ✓ | ✓ | – | ✓ |
| Fan Interaction | UI | DB | ✓ | ✓ | – | ✓ |
| Memory | UI | DB | – | – | – | ✓ |
| Analytics | UI | DB | ✓ | ✓ | – | ✓ |
| AI Decision | UI | DB | ✓ | ✓ | – | ✓ |
| Job Management | UI | DB | ✓ | ✓ | – | – |
| Approval | UI | DB | ✓ | – | – | ✓ |

### 5.14 Core Automation Loop

모든 Core Feature는 하나의 Loop로 연결된다. 이 Loop가 persona-automation-agent의 핵심 제품 구조다.

```text
Observe (SNS / Fans / Performance)
   ↓
Analyze (LLM)
   ↓
Decide (AI Agent)
   ↓
Create (ComfyUI)
   ↓
Publish (SNS API)
   ↓
Interact (Fans)
   ↓
Measure (Analytics)
   └──────────────→ Observe
```

### 5.15 상태 모델 (Status Model) ✅ 2026-10-04 확정

원안에는 상태 흐름이 세 군데(Content Job, Asset, Job Management)에 따로 있었다. 이 흐름들은 서로 겹치고(`scheduled`, `published`가 Content Job과 Asset 양쪽에 있음), 3번에서 확정한 용어(`done`)와도 다르다. 그래서 **객체마다 자기 상태만 갖도록** 나눈다.

| 객체 | 테이블 | 상태 흐름 | 비고 |
|---|---|---|---|
| Content Job (기획 단위) | `content_jobs` (신규) | `draft → queued → generating → ready → published` + `failed`, `cancelled` | 아래 객체들의 진행 상황을 요약한 상태 |
| Generation Job (실행 단위) | `media_queue` (기존) | `pending → processing → done` + `failed` | 재시도는 `pending` + `run_after`로 표현 (별도 `retry` 상태 없음). ✅ 구현됨 |
| Asset | `assets` (신규) | `generated → approved` 또는 `rejected` | 게시 상태는 Asset이 아니라 Post가 갖는다 |
| Post (게시 단위) | `posts` (기존, 수정) | `draft → pending_approval → approved → scheduled → publishing → published` + `failed`, `rejected` | 2번 확정 결정(게시 전 승인) 반영 |

```text
Content Job ──1:N──▶ Generation Job (media_queue) ──1:N──▶ Asset ──1:N──▶ Post ──1:N──▶ Performance
```

- 상태 값은 모두 소문자로 쓴다 (원안 5.10의 `PENDING`, `CLAIMED` 같은 대문자 대신).
- 원안 5.10의 `CLAIMED`와 `RUNNING`은 `processing` 하나로 합친다. 선점(claim)과 실행 시작이 같은 순간이라 나눌 필요가 없다.
- Content Job 하나에서 Asset을 여러 개 만들고 그중 일부만 게시할 수 있다 (예: 4장 생성 → 1장 승인).
- 상태별 전환 조건, 추가 상태(`cancelled`, `archived`, Approval의 `expired`), 멈춘 Job 회수 규칙은 [TECH_DESIGN.md 11. State Machine](TECH_DESIGN.md)에서 정의한다.

---

## 6. Automation Scenarios ✅

5번이 "무슨 기능이 있는가"라면, 6번은 그 기능들이 `n8n → Python → ComfyUI → Supabase → SNS → AI Agent`로 **어떤 순서로 연결되어 움직이는가**를 정의한다.

> **핵심 원칙:** AI가 **결정**하는 것과 n8n/Python이 **실행**하는 것을 분리한다. 예를 들어 AI가 "새 여행 콘텐츠를 만들어라"라고 결정해도 AI가 ComfyUI나 SNS를 직접 건드리지 않는다. `Decision → Job 생성 → n8n 실행 → Python/ComfyUI 실행 → 결과 저장` 순서로 간다.

### 6.0 시나리오별 단계 요약

| # | Scenario | 단계 | 현재 상태 |
|---|---|---|---|
| A | Content Generation | MVP | ✅ 생성·저장·재시도 구현됨. Content Job·Asset 테이블과 LLM 프롬프트 생성은 미구현 |
| B | Asset Review & Approval | V1 | – |
| C | SNS Publishing | MVP 구조 → V1 Instagram | – |
| D | Performance Collection | V1 | – |
| E | AI Performance Analysis | V2 | – |
| F | Autonomous Content Decision | V2 | – |
| G | Fan Message Automation | V2 (V1은 수집만) | – |
| H | Fan Memory Update | V2 | – |
| I | Failure & Retry | MVP부터 단계적으로 | ✅ 일시적 오류 자동 재시도 구현됨 |
| J | Human-in-the-Loop | V1 | – |
| K | Daily Autonomous Operation | Long-term | – |
| L | End-to-End Lifecycle | 전체 기준 | – |

### 6.1 Scenario A: Content Generation (MVP)

**목적:** Operator나 AI Agent가 만든 Content Job으로 이미지나 영상을 자동 생성한다.

**Trigger**

| Trigger | 단계 |
|---|---|
| Operator가 Content Job 생성 | MVP |
| 예약된 콘텐츠 생성 시간 도달 | V1 |
| AI Agent가 새 콘텐츠 생성을 결정 | V2 |

**Workflow**

```text
Content Job 생성 → Supabase → n8n Trigger → Job Claim → LLM Prompt 생성 → Python Bridge
→ ComfyUI Workflow 선택 → RTX 5080 Generation → Output Asset → Python → Supabase Storage
→ Asset 등록 → Content Job 상태 갱신 (ready)
```

> MVP부터 "LLM Prompt 생성" 단계를 포함한다. LLM이 Persona Context와 Content Job의 Topic으로 ComfyUI 프롬프트를 만든다. Operator가 Prompt를 직접 적으면 그 값을 그대로 쓴다.

**실패 처리**

```text
Generation 실패 → 재시도 횟수 확인 ─┬─ 재시도 가능 → 대기 후 Retry
                                    └─ 최대 횟수 초과 → failed → Operator Notification
```

(✅ 현재 `media_queue`의 `attempts` / `max_attempts` / `run_after`로 구현됨)

### 6.2 Scenario B: Asset Review & Approval (V1)

**목적:** 생성된 Asset을 SNS 게시 전에 검토한다.

```text
Asset Generated → Review Required? ─┬─ No ──→ Scheduled
                                    └─ Yes ─→ Operator ─┬─ Approve → Schedule
                                                        └─ Reject  → Regenerate
```

**Approval 기준:** Persona 일치 여부, 이미지 품질, 콘텐츠 규칙, 플랫폼 정책, Caption 적합성, 민감 콘텐츠 여부

> **V1 규칙:** Approval은 설정으로 켜고 끌 수 있게 만들되, **V1에서는 항상 켜 둔다** (2번 확정 결정: 모든 게시물 사전 승인). 그래서 V1에서는 `Review Required? = Yes` 경로만 쓴다. 끄는 것은 신뢰가 쌓인 V2 이후다.

### 6.3 Scenario C: SNS Publishing (MVP 구조 → V1)

**목적:** 승인된 Asset을 지정된 SNS 계정에 자동으로 게시한다.

```text
Approved Asset → Platform 확인 → Caption Generation → Platform Formatting → Schedule 확인
→ n8n → SNS API → Published Post → Post ID 저장 → DB 업데이트
```

**Platform Adapter:** SNS마다 API와 콘텐츠 형식이 다르므로 공통 Publishing Interface를 쓴다. 상위 Workflow는 플랫폼별 구현 차이를 직접 처리하지 않는다.

```text
Publish Service
   ├── Instagram   ← V1
   ├── TikTok
   ├── X
   └── Future Platform
```

### 6.4 Scenario D: Performance Collection (V1)

**목적:** 게시된 콘텐츠의 성과 데이터를 자동으로 수집한다.

**Trigger:** 게시 후 일정 시간 경과(V1: 24시간, 7일), Scheduled Analytics Job, Operator 요청

```text
Published Post → n8n Scheduler → SNS API → Metrics Collection → Supabase → Performance Record
```

**수집 예시:** Views, Likes, Comments, Shares, Saves, Reach, Engagement Rate

### 6.5 Scenario E: AI Performance Analysis (V2)

**목적:** 데이터를 저장하는 데서 끝나지 않고 AI Agent가 성과를 분석한다.

```text
Performance Data → Aggregation → LLM → Performance Analysis → Insight → Supabase
```

**분석 예시:** 최근 20개 콘텐츠 → Topic별 성과 → Format별 성과 → 게시 시간별 성과 → Audience Reaction → AI Analysis

AI는 콘텐츠 하나의 수치만 보지 않고, 가능하면 과거 콘텐츠와 비교해서 분석한다.

### 6.6 Scenario F: Autonomous Content Decision (V2)

**목적:** AI Agent가 다음 콘텐츠를 스스로 결정한다.

```text
Performance + Fan Interaction + Content History + Persona + Current Goal + Rules
   → AI Decision Engine → Next Action
```

**Decision 예시:** "다음 콘텐츠를 생성한다", "기존 여행 콘텐츠의 변형을 생성한다", "새 주제를 테스트한다", "Operator 승인이 필요하다"

**Decision Record:** AI가 내린 결정은 반드시 기록한다. 그래야 AI Agent가 무엇을, 왜 결정하고 실행했는지 추적할 수 있다.

```text
Decision
├── Decision ID
├── Persona ID
├── Input Context
├── Decision
├── Reason
├── Confidence
├── Action
├── Result
└── Created At
```

### 6.7 Scenario G: Fan Message Automation (V2)

**목적:** SNS로 들어오는 댓글이나 DM에 Persona 기반으로 자동 응답한다.

```text
SNS → New Message → n8n → Fan Identification → Conversation Retrieval → Fan Memory Retrieval
→ Persona Context → LLM → Safety / Rule Check → Response → SNS API → Response Sent
```

**LLM에 전달하는 Context (가능한 경우):** Persona + Conversation History + Fan Memory + Recent Content + Interaction Rules + Current Message

### 6.8 Scenario H: Fan Memory Update (V2)

**목적:** 자주 대화하는 Fan에 대한 중요한 정보를 장기 Memory에 반영한다.

```text
Conversation → LLM Memory Extraction → Important Information? ─┬─ No  → End
                                                                └─ Yes → Fan Memory Update
```

예: Fan이 "I love travelling to Japan."이라고 말하면 Fan Memory의 Interest에 `Japan Travel`을 저장한다.

모든 대화를 무조건 장기 Memory에 저장하지 않고, 중요도와 저장 규칙을 적용한다.

### 6.9 Scenario I: Failure & Retry (MVP부터 단계적으로)

모든 자동화 작업은 실패할 수 있으므로 공통 Failure Handling을 쓴다.

```text
Job → Running → Error → Error Classification
```

| Error Type | 처리 | 현재 구현 |
|---|---|---|
| Transient Error | Automatic Retry | ✅ 연결 끊김, Storage 오류 → 재시도 |
| Timeout | Automatic Retry | ✅ ComfyUI 시간 초과 → 재시도 |
| Generation Error | GPU 메모리 부족은 Retry, 그 외는 failed | ✅ OOM만 재시도 |
| Validation Error | Regenerate / Fix | 🔶 지금은 바로 failed (템플릿·자리표시자·워크플로우 검증 오류) |
| API Error | 응답 코드에 따라 Retry 또는 failed | V1 (SNS API) |
| Authentication Error | Operator Notification | V1 (SNS 토큰 만료 등) |
| Policy Error | Approval Required | V1 |
| Unknown Error | Operator Notification | 🔶 지금은 재시도 후 failed |

> 현재 브릿지는 오류를 "재시도 가능 / 불가능" 두 가지로만 나눈다. 위 분류는 `error_type` 칸을 추가해 단계적으로 넓힌다.

### 6.10 Scenario J: Human-in-the-Loop (V1)

완전 자동화가 항상 가능하지는 않으므로, 중요한 작업에는 Operator가 개입할 수 있어야 한다.

```text
AI Decision → Risk Assessment ─┬─ Low Risk  → Execute
                               └─ High Risk → Approval → Operator Review ─┬─ Approve → Execute
                                                                          └─ Reject  → Cancel / Retry
```

> **V1 규칙:** 게시 작업은 위험도와 상관없이 모두 High Risk 경로로 보낸다 (6.2와 같은 규칙).

### 6.11 Scenario K: Daily Autonomous Operation (Long-term)

장기적으로 시스템은 Operator가 매일 직접 작업하지 않아도 운영되어야 한다. Operator는 작업마다 참여하지 않고, Dashboard에서 전체 시스템을 감독한다.

```text
Observe → Analyze → Decide → Create → Publish → Interact → Measure → (다시 Observe)
```

### 6.12 Scenario L: End-to-End Lifecycle

최종적으로 하나의 Content Job은 아래 전체 Lifecycle을 갖는다. 각 콘텐츠는 독립적으로 끝나지 않고, **다음 콘텐츠를 만드는 데이터**가 된다.

```text
1. IDEA → 2. PLANNED → 3. QUEUED → 4. CLAIMED → 5. GENERATING → 6. GENERATED → 7. REVIEW
→ 8. APPROVED → 9. SCHEDULED → 10. PUBLISHED → 11. PERFORMANCE COLLECTED → 12. ANALYZED
→ 13. LEARNING → 14. NEXT DECISION → 15. NEW CONTENT JOB
```

**Lifecycle 단계와 5.15 객체별 상태의 대응**

이 15단계는 한 테이블의 상태 칸 하나로 표현하지 않는다. 단계마다 그 일을 맡은 객체의 상태로 표현하고, 화면에서는 이를 이어 붙여 하나의 Lifecycle로 보여준다.

| Lifecycle 단계 | 담당 객체 · 상태 | 단계 |
|---|---|---|
| 1 IDEA | Decision Record | V2 |
| 2 PLANNED | Content Job `draft` | MVP |
| 3 QUEUED | Content Job `queued`, Generation Job `pending` | MVP |
| 4 CLAIMED · 5 GENERATING | Generation Job `processing`, Content Job `generating` | MVP |
| 6 GENERATED | Asset `generated`, Content Job `ready` | MVP |
| 7 REVIEW | Post `pending_approval` | V1 |
| 8 APPROVED | Post `approved` | V1 |
| 9 SCHEDULED | Post `scheduled` | V1 |
| 10 PUBLISHED | Post `published`, Content Job `published` | V1 |
| 11 PERFORMANCE COLLECTED | Performance 행 생성 | V1 |
| 12 ANALYZED · 13 LEARNING | Insight 기록 | V2 |
| 14 NEXT DECISION | Decision Record | V2 |
| 15 NEW CONTENT JOB | 새 Content Job (어떤 Decision에서 나왔는지 연결) | V2 |

### 6.13 Automation Architecture

```text
                Lovable (Control Center)
                          │
                Supabase (Source of Truth)
                          │
                  n8n (Orchestrator)
          ┌───────────────┼───────────────┐
   Python Bridge      LLM (Brain)      SNS API
          │
  ComfyUI / RTX 5080
          │
   Asset Storage
```

### 6.14 Automation Principles

1. **모든 작업은 Job으로 관리한다.** 모든 자동화 작업에는 고유한 `Job ID`가 있다.
2. **모든 상태 변경을 기록한다.** 현재 상태뿐 아니라 실행 결과와 오류도 기록한다.
3. **모든 작업은 다시 실행할 수 있어야 한다.** 일시적인 오류가 나도 전체 Workflow를 처음부터 다시 돌리지 않고, 실패한 단계부터 재시도한다. (객체별로 상태를 나누면 실패한 객체만 다시 처리하면 된다.)
4. **AI와 실행 계층을 분리한다.** LLM은 무엇을 할지 결정하고, n8n/Python은 그 결정을 실제 시스템에서 실행한다.
5. **Source of Truth는 Supabase다.** Lovable, n8n, Python, ComfyUI가 각자 별도의 상태를 관리하지 않는다.
6. **Operator는 Exception Handler다.** 정상 작업은 자동화하고, 판단이 필요한 상황에만 사람이 개입한다. (단, V1에서는 게시 승인이 예외가 아닌 기본 단계다.)
7. **모든 자동화는 관찰할 수 있어야 한다.** Operator는 Dashboard에서 다음을 확인할 수 있어야 한다: 실행 중인 Job, 완료된 Job, 실패한 Job, Retry 상태, 생성된 Asset, SNS 게시 결과, AI Decision, Approval 요청, Error Log.

---

## 7. Scope ✅

### 7.1 Scope Definition

persona-automation-agent의 최종 목표는 버추얼 인플루언서의 콘텐츠 제작부터 SNS 운영, 팬과의 상호작용, 성과 분석, 다음 행동 결정까지 자동화하는 것이다. 하지만 모든 기능을 동시에 만들지 않고 아래 단계로 나눠 개발한다.

```text
MVP → V1 → V2 → Long-term Autonomous Agent
```

> **핵심 원칙:** MVP에서는 자동화의 기반을 만들고, 이후 단계에서 AI의 자율성을 점진적으로 높인다.

| 단계 | 한 줄 목표 |
|---|---|
| **MVP** | 콘텐츠 생성 자동화 파이프라인을 안정적으로 돌린다 |
| **V1** | 콘텐츠 생성뿐 아니라 실제 SNS 운영까지 자동화한다 |
| **V2** | AI가 콘텐츠를 만드는 데서 나아가 무엇을 만들지 결정한다 |
| **Long-term** | 완전한 Autonomous Virtual Influencer Operating System |

### 7.2 MVP Scope

**MVP 목표**

> Operator가 Content Job을 만들면 `AI → n8n → Python → ComfyUI → Supabase Storage`까지 자동으로 실행되어 최종 Asset이 생성되고 상태가 기록되는 시스템을 구축한다. 그리고 생성된 Asset을 SNS 게시 Workflow와 연결할 수 있는 기반까지 만든다.

**MVP에서 반드시 작동해야 하는 한 줄**

```text
Google Login → Persona → Content Job → n8n → Python → ComfyUI / RTX 5080 → Supabase Storage → Asset Library
```

#### MVP 포함 기능

**1. Authentication**

- Google Login
- 사용자 계정과 기본 User Profile
- User별 데이터 분리 (Supabase Auth + RLS. Lovable은 브라우저에서 동작하므로 `service_role` 키 없이 접근해야 한다)

**2. Persona Management**

최소한의 Persona 정보를 관리한다. 복잡한 세계관이나 관계 그래프보다 **AI가 콘텐츠 생성에 쓸 수 있는 기본 Context**를 우선한다.

```text
Persona
├── Name
├── Profile Image
├── Description
├── Personality
├── Speaking Style
├── Interests
├── Content Rules
└── Visual Settings
```

**3. Content Job**

콘텐츠 제작 요청을 Job으로 관리한다.

```text
Content Job
├── Job ID
├── Persona ID
├── Content Type
├── Topic
├── Prompt
├── Priority
├── Status
├── Retry Count
└── Created At
```

- 지원 범위: Image, 기본 Video Job 구조, Text Prompt, **AI-generated Prompt** (LLM이 Persona Context와 Topic으로 프롬프트 생성)
- 실제 Generation은 **Image Generation을 우선**한다.

**4. Job Queue**

Supabase를 기반으로 작업 Queue를 만든다. 상태는 5.15 상태 모델을 따른다.

```text
Content Job:     draft → queued → generating → ready   (+ failed, cancelled)
Generation Job:  pending → processing → done            (+ failed)
재시도:          failed 대신 pending + run_after 로 되돌려 대기
```

같은 Job의 중복 실행을 막기 위해 **Atomic Claim**을 쓴다. (✅ 구현됨)

**5. n8n Orchestration**

n8n을 자동화 Orchestrator로 쓴다. n8n Workflow는 가능한 한 **Business Logic보다 Orchestration에 집중**한다.

```text
Supabase → Pending Job Detection → Job Claim → Python Bridge → ComfyUI → Result → Supabase
```

(✅ M3 `n8n/pa_001`~`pa_006` Workflow로 구현됨, TECH 16.8)

**6. Python Local Execution**

로컬 PC의 Python Bridge가 ComfyUI와 통신한다. 역할: Job 수신, ComfyUI Workflow 호출, Generation 상태 확인, 결과 파일 다운로드, Storage Upload, Job 상태 업데이트, Error 처리 (✅ `src/comfy_bridge.py`로 구현됨)

**7. ComfyUI Integration**

RTX 5080 로컬 PC의 ComfyUI를 Generation Engine으로 쓴다.

| MVP 지원 | 이후 확장 |
|---|---|
| Text → Image, Image → Image, LoRA, 기본 Character Reference, 기본 Workflow 선택, **FaceSwap 기본 workflow** | Video Generation, FaceSwap 고도화, Upscaling, 고급 Character Consistency |

> FaceSwap은 브릿지가 이미 입력 이미지 업로드를 지원하므로, MVP에서는 기본 workflow 템플릿만 추가한다. 품질 튜닝과 복잡한 파이프라인은 V1이다.

**8. Asset Storage**

생성된 파일은 Supabase Storage에, Asset Metadata는 Database에 저장한다.

```text
Storage
└── persona/
      └── {persona_id}/
            └── assets/
                  └── {asset_id}.png
```

> 현재 브릿지는 `{persona_id}/{job_type}/{yyyy}/{mm}/{job_id}/{filename}` 경로를 쓴다. `assets` 테이블을 만들 때 위 경로로 바꾼다.

**9. Asset Library**

Lovable Dashboard에서 생성된 Asset을 확인한다. 지원 기능: Thumbnail, Preview, Persona Filter, Status Filter, Date Filter, Job 연결, Download, Asset Detail

**10. Basic Dashboard**

```text
Dashboard
├── Personas
├── Content Jobs
├── Asset Library
├── Active Jobs
├── Failed Jobs
└── System Status
```

**11. Basic Error Handling**

자동화 실패를 기록한다. Operator가 Dashboard에서 실패한 Job을 확인하고 Retry할 수 있어야 한다.

```text
Error
├── Job ID
├── Error Type      (6.9 분류)
├── Error Message
├── Retry Count
├── Failed Step
└── Created At
```

### 7.3 MVP SNS Scope

MVP에서 SNS는 완전한 멀티 플랫폼 자동 운영보다 **Publishing Pipeline의 기반**을 먼저 만든다. 실제 Instagram 게시는 V1이다.

| MVP | V1 |
|---|---|
| SNS Account 구조, Platform 정보, Post 데이터 구조, Asset → Post 연결, Caption 생성, 게시 상태 관리 구조, 게시 결과 저장 구조, API 연동 구조 (Platform Adapter 인터페이스) | Instagram 실제 Publishing, 예약 게시, 승인, 실패 처리·Retry |

첫 플랫폼은 Instagram이고 (2번 확정 결정), 이후 같은 Interface로 다른 플랫폼을 추가한다.

```text
Platform Adapter
├── Instagram   ← V1
├── TikTok
├── X
└── Future Platforms
```

### 7.4 MVP에서 제외하는 기능

개발 범위를 통제하기 위해 아래 기능은 MVP에서 뺀다.

| 분류 | 제외 기능 | 시점 |
|---|---|---|
| Fan Interaction | 자동 DM, 자동 댓글, 장기 Fan Memory, Relationship Management | V1 이후 |
| Advanced AI Agent | 완전 자율 콘텐츠 기획, 장기 전략 수립, Autonomous Decision Loop, Autonomous Experimentation | V2 이후 |
| Advanced Video | 고급 Video Generation, 복잡한 FaceSwap Pipeline, Character Consistency Video, 자동 Video Editing | V1 / V2 |
| Advanced Analytics | 예측 분석, A/B Testing 자동화, 콘텐츠 성과 예측, Audience Segmentation | V2 |
| Multi-Persona Automation | 여러 Persona를 동시에 독립적으로 자율 운영 | V2 이후 |

> Multi-Persona: MVP는 `1 User → 1~N Persona` **데이터 구조만** 지원한다. Autonomous Multi-Persona Management는 이후 단계에서 만든다.

### 7.5 V1 Scope

> **V1 목표:** 콘텐츠 생성뿐 아니라 실제 SNS 운영까지 자동화한다. 성공 기준은 3.6의 V1 항목(주 7개 게시를 4주 연속 유지)이다.

**주요 기능:** SNS API Publishing (Instagram), 예약 게시, Platform-specific Caption, Hashtag Generation, Post Management, Publishing Retry, Approval Workflow (모든 게시물 사전 승인), Notification, Basic Analytics, Performance Collection, Video Generation, FaceSwap 고도화, 기본 Fan Interaction (수집)

```text
Content Job → Generate → Review → Approve → Schedule → Publish → Collect Performance
```

### 7.6 V2 Scope

> **V2 목표:** AI가 콘텐츠를 만드는 데서 나아가 무엇을 만들지 결정한다.

**주요 기능:** Performance Analysis, Content Recommendation, AI Content Planning, Autonomous Content Job Creation, Content Performance Comparison, Topic Optimization, Posting Time Optimization, Basic Fan Interaction (자동 응답), Fan Memory, Decision Log

```text
Performance → AI Analysis → AI Decision → Content Job → Generation → Publishing → Performance
```

### 7.7 Long-term Scope

최종 목표는 완전한 Autonomous Virtual Influencer Operating System이다.

**주요 기능:** Autonomous Content Planning, Autonomous Content Generation, Autonomous Publishing, Autonomous Fan Interaction, Long-term Fan Memory, Relationship Management, Performance Learning, Strategy Optimization, Multi-Persona Management, Autonomous Experimentation, Self-Optimization

```text
Observe → Analyze → Decide → Create → Publish → Interact → Measure → (다시 Observe)
```

### 7.8 Scope Matrix

| 기능 | MVP | V1 | V2 | Long-term |
|---|:-:|:-:|:-:|:-:|
| Google Login | ✓ | ✓ | ✓ | ✓ |
| User Management | ✓ | ✓ | ✓ | ✓ |
| Persona Management | ✓ | ✓ | ✓ | ✓ |
| Content Job | ✓ | ✓ | ✓ | ✓ |
| Job Queue | ✓ | ✓ | ✓ | ✓ |
| n8n Automation | ✓ | ✓ | ✓ | ✓ |
| Python Bridge | ✓ | ✓ | ✓ | ✓ |
| AI Prompt Generation | ✓ | ✓ | ✓ | ✓ |
| ComfyUI Image Generation | ✓ | ✓ | ✓ | ✓ |
| LoRA | ✓ | ✓ | ✓ | ✓ |
| Character Reference | ✓ | ✓ | ✓ | ✓ |
| FaceSwap | 기본 | ✓ | ✓ | ✓ |
| Video Generation | 구조 | ✓ | ✓ | ✓ |
| Asset Library | ✓ | ✓ | ✓ | ✓ |
| Caption Generation | ✓ | ✓ | ✓ | ✓ |
| SNS Account | 기본 | ✓ | ✓ | ✓ |
| SNS Publishing | 구조 | ✓ | ✓ | ✓ |
| Approval Workflow | | ✓ | ✓ | ✓ |
| Scheduling | | ✓ | ✓ | ✓ |
| Performance Collection | | ✓ | ✓ | ✓ |
| Analytics | 기본 | ✓ | ✓ | ✓ |
| AI Performance Analysis | | | ✓ | ✓ |
| AI Content Planning | | | ✓ | ✓ |
| Autonomous Decision | | | ✓ | ✓ |
| Fan Interaction | | 기본 | ✓ | ✓ |
| Fan Memory | | | ✓ | ✓ |
| Autonomous Fan Response | | | ✓ | ✓ |
| Multi-Persona | 구조 | 기본 | ✓ | ✓ |
| Autonomous Experimentation | | | | ✓ |
| Self Optimization | | | | ✓ |

### 7.9 MVP Definition of Done

MVP는 아래 조건을 모두 만족하면 완료로 본다.

| 영역 | 완료 조건 |
|---|---|
| User | Google Login → Dashboard 진입 |
| Persona | Persona 생성 → Persona Context 저장 |
| Content | Content Job 생성 → Queue 등록 |
| Automation | Supabase → n8n → Python → ComfyUI 자동 실행 |
| Generation | ComfyUI가 Image 생성 → Supabase Storage 저장 → Asset 등록 |
| Tracking | Generation Job이 `pending → processing → done`으로 바뀌고 Content Job이 `ready`가 됨. 실패 시 `failed`가 기록되고 Retry할 수 있음 |
| Dashboard | Operator가 Persona, Content Job, Job Status, Generated Asset, Error, Retry를 확인할 수 있음 |

**최종 MVP Test**

Operator가 Dashboard에서 **"이 Persona로 이 주제의 이미지를 만들어줘."**라고 Content Job을 만들었을 때,

```text
Dashboard → Supabase → n8n → Python → ComfyUI / RTX 5080 → Image Generation → Supabase Storage → Asset Library → Dashboard
```

까지 **사람이 중간에 파일을 옮기거나 ComfyUI를 직접 조작하지 않고 완료되는 것**을 MVP의 핵심 성공 조건으로 한다.

> MVP의 중심은 AI가 모든 것을 자율적으로 운영하는 것이 아니라, **자동화 파이프라인을 실제로 안정적으로 돌리는 것**이다. 팬 DM 자동 응답이나 완전 자율 의사결정부터 만들지 않는다. 위 한 줄이 안정화된 뒤 그 위에 SNS, Analytics, AI Decision을 얹는다.

---

## 8. Future Roadmap ✅

### 8.1 Roadmap Overview

persona-automation-agent는 처음부터 완전한 Autonomous Agent를 만들지 않는다. 기본 자동화 Pipeline을 먼저 안정화한 뒤, AI의 판단 범위와 실행 권한을 점진적으로 넓힌다.

```text
Phase 1 Automation Foundation → Phase 2 SNS Operation → Phase 3 AI Decision → Phase 4 Fan Interaction
→ Phase 5 Autonomous Influencer → Phase 6 Experimentation → Phase 7 Multi-Persona → Phase 8 Self-Optimization
```

최종적으로는 Operator가 모든 작업을 직접 지시하지 않고, **목표와 Persona를 설정하면 AI Agent가 계속 운영하는 구조**를 목표로 한다.

**Phase와 7번 Scope 단계의 대응**

| Phase | 내용 | Scope 단계 | Autonomy Level (8.10) |
|---|---|---|---|
| Phase 1 | Automation Foundation | **MVP** | L2 |
| Phase 2 | SNS Operation | **V1** | L2 (게시 전 사람 승인) |
| Phase 3 | AI Decision Engine | **V2** | L3 |
| Phase 4 | Fan Interaction & Memory | **V2** (V1에서는 수집만) | L3 |
| Phase 5 | Autonomous Virtual Influencer | Long-term | L4 |
| Phase 6 | Autonomous Experimentation | Long-term | L4 |
| Phase 7 | Multi-Persona Operation | Long-term (데이터 구조는 MVP부터) | L4 |
| Phase 8 | Self-Optimization | Long-term | L5 |

### 8.2 Phase 1: Automation Foundation (MVP)

**목표:** 콘텐츠 생성 Pipeline을 안정적으로 구축한다.

**핵심 기능:** Google Authentication, Persona Management, Content Job, AI Prompt Generation, Job Queue, n8n Orchestration, Python Bridge, ComfyUI Integration (FaceSwap 기본 workflow 포함), RTX 5080 Local Generation, Supabase Storage, Asset Library, Caption Generation, Job Status, Error Handling, Retry, Dashboard

```text
Operator → Persona → Content Job → Supabase → n8n → Python → ComfyUI / RTX 5080 → Asset → Supabase Storage
```

**성공 기준:** 사람이 중간에 개입하지 않고 Content Job이 생성부터 Storage까지 완료된다 (7.9 MVP Definition of Done).

### 8.3 Phase 2: SNS Operation (V1)

**목표:** 생성된 콘텐츠를 실제 SNS에 자동으로 게시한다.

**주요 기능:** SNS Account Connection, Platform Adapter, Hashtag Generation, **Approval Workflow (모든 게시물 사전 승인)**, Scheduled Publishing, Immediate Publishing, Publishing Status, Retry, Post Management, Basic Analytics, Performance Collection

```text
Asset → Caption → Platform Formatting → Approval → Schedule → SNS API → Published Post → Performance
```

**핵심 변화:** Phase 1이 "콘텐츠를 만든다"라면, Phase 2는 "콘텐츠를 만들고 게시한다"다.

### 8.4 Phase 3: AI Decision Engine (V2)

**목표:** AI가 사용자의 명령을 실행하는 데서 나아가 **다음 콘텐츠를 결정**한다.

**주요 기능:** Performance Analysis, Content Analysis, Topic Analysis, Content Recommendation, AI Content Planning, Autonomous Content Job Creation, Decision Log, Confidence Score, Human Approval

```text
Performance → AI Analysis → AI Decision → Content Job → Generation → Publishing
```

**예시**

```text
최근 여행 콘텐츠의 평균 조회수: +42%
최근 패션 콘텐츠: -18%

Decision: 여행 관련 콘텐츠를 추가 생성
Action:   Content Job 생성
```

이 단계부터 `Content Job`은 Operator뿐 아니라 **AI Agent도 만들 수 있다.**

### 8.5 Phase 4: Fan Interaction & Memory (V2)

**목표:** AI가 콘텐츠 생성을 넘어 SNS 사용자와 계속 상호작용한다.

**주요 기능:** Comment Collection, DM Collection, Conversation Management, Persona Context, Fan Memory, Response Generation, Safety Rules, Interaction Rules, Human Approval, Relationship Context

```text
Fan → Comment / DM → Message → Conversation → Fan Memory → Persona → LLM → Safety Check → Response → SNS
```

**Memory 구조**

```text
Fan
 ├── Profile
 ├── Interests
 ├── Conversation History
 ├── Important Memories
 └── Relationship Context
```

이를 통해 AI는 일회성 chatbot이 아니라 **장기적인 관계 Context를 유지하는 Persona**로 발전한다.

### 8.6 Phase 5: Autonomous Virtual Influencer (Long-term)

**목표:** Operator의 직접 명령 없이 AI Agent가 버추얼 인플루언서 운영을 계속 수행한다.

```text
Observe → Analyze → Decide → Create → Publish → Interact → Measure → (다시 Observe)
```

AI Agent가 독립적으로 수행하는 일: 콘텐츠 아이디어 생성, 콘텐츠 제작, 콘텐츠 변형, 게시 일정 결정, Caption 생성, SNS 게시, 댓글·DM 응답, 성과 분석, 콘텐츠 전략 수정, 새 콘텐츠 생성, 실패 작업 Retry, Operator 승인 요청

### 8.7 Phase 6: Autonomous Experimentation (Long-term)

장기적으로 AI Agent가 운영뿐 아니라 **실험을 통해 전략을 개선**하도록 확장한다.

```text
Experiment
  Hypothesis: 짧은 영상이 이미지보다 Engagement가 높을 것이다.
  Test A: Image
  Test B: 10 sec Video
  Test C: 15 sec Video

Generate → Publish → Collect Data → Compare → Analyze → Learn
```

결과는 다음 전략에 반영한다.

### 8.8 Phase 7: Multi-Persona Operation (Long-term)

한 명의 Operator가 여러 버추얼 인플루언서를 운영한다.

```text
Operator
 ├── Persona A ── Instagram · TikTok · X
 ├── Persona B ── Instagram · TikTok
 └── Persona C ── Instagram · X
```

각 Persona는 독립적인 Identity, Memory, Content Strategy, SNS Accounts, Audience, Performance, AI Decision을 가진다. (`user → persona` 데이터 구조는 MVP부터 지원한다.)

### 8.9 Phase 8: Self-Optimization (Long-term)

최종 단계에서는 AI Agent가 시스템의 운영 효율 자체를 최적화한다.

**Optimization 대상:** Generation Workflow, Prompt, LoRA, Model, Image Style, Video Style, Posting Time, Content Type, Caption, Hashtag, Audience Strategy

```text
Performance Data → AI Analysis → Workflow Performance Comparison → Best Workflow Selection → Future Content
```

단, 시스템 자체를 자동으로 바꾸는 기능은 위험이 크므로 처음에는 **Operator Approval을 필수**로 한다.

### 8.10 Autonomy Level

AI Agent의 자율성은 단계적으로 높인다.

| Level | AI Capability | Human Role | 적용 단계 |
|---|---|---|---|
| L0 | Manual | 모든 작업 수행 | – |
| L1 | Assisted | AI가 제안 | – |
| L2 | Automated Execution | AI가 정해진 Workflow 실행 | **MVP, V1** |
| L3 | Autonomous Planning | AI가 다음 작업 결정 | **V2** |
| L4 | Autonomous Operation | AI가 콘텐츠·SNS·Interaction 운영 | Long-term |
| L5 | Self-Optimizing Agent | AI가 전략과 Workflow까지 최적화 | Long-term |

- **MVP 목표 (L2):** 정해진 Workflow를 안정적으로 자동 실행한다.
- **V1 목표 (L2):** 게시까지 자동 실행하되, 모든 게시물은 사람이 승인한다.
- **V2 목표 (L3):** AI가 다음 콘텐츠와 행동을 결정한다.
- **Long-term 목표 (L4~L5):** AI가 버추얼 인플루언서의 운영과 최적화를 계속 수행한다.

### 8.11 Product Evolution

```text
Tool → Automation System → AI Assistant → AI Agent → Autonomous Influencer
```

| Stage | 이름 | 설명 |
|---|---|---|
| 1 | Tool | 사용자가 직접 콘텐츠를 만든다 |
| 2 | Automation | 시스템이 반복 작업을 대신한다 |
| 3 | Assistant | AI가 다음 행동을 추천한다 |
| 4 | Agent | AI가 작업을 직접 결정하고 실행한다 |
| 5 | Autonomous Influencer | AI가 Persona를 바탕으로 콘텐츠를 만들고, 게시하고, 팬과 상호작용하고, 데이터를 분석해 스스로 다음 행동을 결정한다 |

### 8.12 Final Product Vision

persona-automation-agent는 단순한 AI 콘텐츠 생성기가 아니다. 사용자는 아래 네 가지만 설정한다.

```text
Persona + Goals + Rules + Permissions
```

그러면 AI Agent가 아래 Loop를 수행한다.

```text
Observe → Think → Create → Publish → Interact → Measure → Learn → Decide → Repeat
```

> 최종 제품의 핵심 가치는 **"콘텐츠를 만들어주는 AI"가 아니라 "하나의 버추얼 인플루언서를 운영하는 AI"**를 만드는 것이다.

---

## 다음 문서: 기술 설계

PRD 1~8로 제품 정의는 1차 완성이다. 이제부터는 실제로 개발할 수 있는 기술 설계로 넘어간다. 기술 설계는 [TECH_DESIGN.md](TECH_DESIGN.md)에 작성한다.

```text
PRD ✅ → 9. System Architecture → 10. Database / ERD → 11. State Machine → 12. API Specification
→ 13. ComfyUI Workflow Specification → 14. n8n Workflow Specification → 15. Security → 16. Implementation Plan
```
