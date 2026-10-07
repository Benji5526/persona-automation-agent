# Personal Edition — 활성 구현 대상

> **CURRENT PRODUCT MODE = PERSONAL.** 지금 구현하는 것은 이 문서의 구조다. SaaS 설계는 [saas.md](saas.md)에 보존되어 있고 요청이 있기 전에는 구현하지 않는다.
> 정본 설계: [TECH_DESIGN 56장](../TECH_DESIGN.md). 범위: [MVP_SCOPE_LOCK](../MVP_SCOPE_LOCK.md).

## 전제

- 실제 사용자는 **1명(OWNER)** 이다. OWNER ≡ `users.role = 'admin'` (DB 값은 바꾸지 않는다).
- 로그인은 Google OAuth → Supabase Auth, 가입 허용 목록에 본인 이메일 하나.
- RLS는 유지한다 (`Persona → 소유자`). 복잡한 멀티테넌트 정책은 만들지 않는다.
- GPU는 장비에 고정하지 않는다: **Local GPU 또는 Cloud GPU**를 설정에서 고른다 ([execution-targets.md](execution-targets.md)).

## 구조

```text
Lovable (Control Center)
   │ RPC / Realtime
Supabase (Source of Truth: Auth · DB · Storage · Realtime · app_settings)
   │  Content Job (queued)
   ├─ 수동 [생성]  create_content_job ─┐
   └─ AI Decision(승인 후)             ├─ 같은 경로
        execute_ai_decision ──────────┘
                │
              n8n (Orchestrator)  ──▶ generation Job (pending)
                │
   활성 워커만 선점 (DB가 강제)  ◀── 브릿지가 pull
        ┌───────┴────────┐
     LOCAL             CLOUD            같은 Python 브릿지 코드
   내 PC GPU          Cloud GPU          (EXECUTION_TARGET만 다름)
        └───────┬────────┘
              ComfyUI (127.0.0.1)
                │
   Asset → 승인 → Post → SNS → Analytics → AI Decision → 새 Content Job
```

| 구성 | 역할 |
|---|---|
| Lovable | 관제 화면. Supabase만 호출한다. GPU 인프라(주소·키·Pod)를 모른다 |
| Supabase | 진실의 원천, 인증, RLS, 상태 전이, 선점·권한 판정(DB 함수) |
| n8n | 오케스트레이션(Job 감지·프롬프트·캡션·게시·수집·알림) |
| Python 브릿지 | 실행 서비스. ComfyUI 호출, 출력 검증, Storage 업로드, Asset 등록 |
| ComfyUI | 생성 엔진. `127.0.0.1`에서만 |
| LLM | 의사결정 엔진. 실행하지 않는다 |

## 유지하는 기능

Google 로그인, Persona(+Assets), Content Job, Asset 관리, ComfyUI, Python 실행, n8n, Scheduler, SNS 게시, Analytics, AI Decision, 승인, 긴급 정지, 모니터링, 감사 로그.

## 구현하지 않는 것 (SaaS 전용, 문서만 보존)

Multi-user, Multi-tenant, 조직·팀·멤버·초대, 구독·과금·플랜 한도, 고객별 Quota, 고객 온보딩, 테넌트 관리 화면, 고객별 GPU·n8n·ComfyUI 워커, 수평 확장·로드 밸런서·다중 리전, Kubernetes·Service Mesh·API Gateway·마이크로서비스, Redis(필요가 입증될 때만), Message Queue(n8n + Supabase Job 큐로 충분하다).

## 최종 MVP 흐름

```text
로그인 → Persona → Content Job → (활성 Execution Target) → 브릿지 → ComfyUI → Asset
  → 사람 승인 → SNS → Analytics → LLM Decision → 새 Content Job
```
