# persona-automation-agent

버추얼 인플루언서 한 명을 하나의 AI Agent로 보고, 콘텐츠 생성·SNS 운영·팬 상호작용·성과 분석을 자동화하는 **자율형 버추얼 인플루언서 운영 플랫폼**입니다.

```text
Lovable (관제센터) → Supabase (Source of Truth) → n8n (Orchestrator) → Python 브릿지 → ComfyUI / GPU (Local 또는 Cloud)
                                                     ├→ LLM (프롬프트·캡션·판단)
                                                     └→ SNS API (V1)
```

## 문서

| 문서 | 내용 |
|---|---|
| [docs/PRD.md](docs/PRD.md) | 제품 정의 v1.0: 문제, 목표·지표, 사용자, 기능, 자동화 시나리오, 범위(MVP·V1·V2), 로드맵 |
| [docs/TECH_DESIGN.md](docs/TECH_DESIGN.md) | 기술 설계 v1.0 (**처음 읽을 때는 40장 최종 통합 명세부터**): 아키텍처, DB, State Machine, API, ComfyUI·n8n 명세, 보안, 구현 계획, UI/UX, Frontend, Python 실행 계층, n8n 구현, Supabase 구현, Lovable 빌드 명세, SNS 연동, Analytics, AI Decision Engine, Fan Interaction & Memory, Autonomous Loop, AI Permission·Safety, Experimentation, Self-Optimization, Multi-Persona, Monitoring, Backup·DR, Cost·Resource |
| [docs/architecture/personal.md](docs/architecture/personal.md) | **현재 활성 구현 대상 = Personal Edition** (사용자 1명, Local/Cloud GPU 선택). SaaS 설계는 [saas.md](docs/architecture/saas.md)에 보존, GPU 선택은 [execution-targets.md](docs/architecture/execution-targets.md) |
| [docs/MVP_SCOPE_LOCK.md](docs/MVP_SCOPE_LOCK.md) | **MVP v1.0의 범위를 고정** (무엇이 안과 밖인가, 설계와의 대응, 열린 결정) |
| [docs/MVP_CHECKLIST.md](docs/MVP_CHECKLIST.md) | 기능 완료 12칸 체크 + MVP v1.0 완료 목록 24개 |
| [CLAUDE.md](CLAUDE.md) | Claude Code 작업 규칙 (계층 경계·보안·AI·DB·테스트). 세부 규칙은 `.claude/skills/pa-*` |
| [docs/lovable_master_prompt.md](docs/lovable_master_prompt.md) | Lovable에 붙여 넣는 Master Prompt와 Phase별 프롬프트 (M4) |
| [docs/n8n_guide.md](docs/n8n_guide.md) | n8n Workflow import·Credential·Webhook 연결·동작 확인 (M3) |
| [supabase/README.md](supabase/README.md) | DB 마이그레이션 적용·테스트 방법, 적용 후 점검 SQL (`verify_production.sql`) |

## 폴더 구조

```text
app/                   Python 브릿지 (M2): FastAPI /v1, GPU Worker, ComfyUI·Supabase 연동
workflows/             ComfyUI Workflow 템플릿 + registry.json
supabase/migrations/   DB 마이그레이션 0001~0010
supabase/tests/stubs/  로컬 테스트 전용 Supabase 흉내 스키마
tests/db/              DB 테스트
tests/bridge/          브릿지 테스트 (실제 DB + 가짜 ComfyUI)
n8n/                   n8n Workflow JSON (M3: WF-001~006 + LLM 하위 Workflow)
deploy/n8n/            n8n 원격 서버 배포 (Docker Compose + Caddy, TECH_DESIGN 26)
docs/                  PRD, 기술 설계, MVP 범위·체크리스트, 가이드
docs/architecture/     Personal(활성) · SaaS(보존) · Execution Target 설계
docs/product/          Personal MVP · SaaS 로드맵 (링크)
.claude/skills/        Claude Code용 프로젝트 Skill 11개 (pa-architecture, pa-supabase, pa-lovable, pa-python-execution, pa-n8n, pa-comfyui, pa-ai-decision, pa-sns-publishing, pa-fan-interaction, pa-testing, pa-security)
```

## 진행 상황

| Milestone | 상태 |
|---|---|
| PRD v1.0 (1~8) | ✅ |
| 기술 설계 v1.0 (9~56) | ✅ (17 UI/UX, 18 Frontend, 19 Python 실행 계층, 20 n8n 구현, 21 Supabase 구현, 22 Lovable 빌드 명세, 23 Lovable 프롬프트, 24 Supabase 적용 절차, 25 로컬 PC 운영, 26 n8n 서버 운영, 27 MVP E2E 테스트, 28 SNS 연동 V1 설계, 29 Analytics 설계, 30 AI Decision Engine 설계, 31 Fan Interaction & Memory 설계, 32 Autonomous Operation Loop 설계, 33 AI Permission·Safety 설계, 34 Experimentation 설계, 35 Self-Optimization 설계, 36 Multi-Persona 설계, 37 Monitoring·Observability 설계(37-A 데이터 모델 포함), 38 Backup·DR 설계, 39 Cost·Resource 설계, 40 최종 통합 명세, 41 예약 게시 모듈, 42 Scheduler 프론트엔드 명세, 43 Scheduler 실행 통합 명세, 44 구현 로드맵(실행 순서·작업 분리), 45 Foundation 실행 명세, 46 Content Job 대응·실행, 47 Python 실행 계층 대응·실행, 48 ComfyUI Workflow 대응·첫 실제 생성 준비, 49 n8n Production Generation Workflow 대응·첫 자동 실행, 50 Asset Management 대응, 51 Post·SNS Publishing 대응, 52 Analytics·Performance Intelligence 대응, 53 AI Decision Engine 대응, 54 AI Decision Engine 구현 사양 대응, 55 Fan Interaction·Memory 대응, 56 Personal Edition·Execution Target 포함) |
| M0 Environment (Supabase, Cloudflare Tunnel, n8n 서버, Lovable) | 대기 (계정·결제가 필요한 직접 작업. 순서와 사람·Claude Code·Lovable별 작업: TECH_DESIGN 44.5) |
| M1 Database Foundation | ✅ 로컬 테스트 통과 · 실제 Supabase 적용 전 |
| M2 Python Bridge v1 | ✅ 로컬 테스트 통과 · 실제 ComfyUI·Supabase 연결 전 (첫 실제 생성 절차: TECH_DESIGN 25.6) |
| M3 n8n Workflows | ✅ 작성 · 실제 n8n에 import·연결 전 ([n8n_guide](docs/n8n_guide.md)) |
| M4 Lovable · M5 MVP 통합 | 대기 (실행 순서·기대 결과: TECH_DESIGN 27) |

## 브릿지 실행

```bash
cp .env.example .env   # 값을 채운다
.venv/Scripts/python -m app.main
```

ComfyUI는 `--listen 127.0.0.1`로 먼저 실행해 둡니다. 브릿지는 시작할 때 `workflows/registry.json`을 검증하고, ComfyUI에 없는 노드를 쓰는 Workflow는 끈 뒤 `comfy_workflows` 테이블에 동기화합니다.

## 테스트

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest tests -q
```

DB 테스트는 Docker 없이 내장 PostgreSQL(`pgserver`)로 실행됩니다.
