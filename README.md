# persona-automation-agent

버추얼 인플루언서 한 명을 하나의 AI Agent로 보고, 콘텐츠 생성·SNS 운영·팬 상호작용·성과 분석을 자동화하는 **자율형 버추얼 인플루언서 운영 플랫폼**입니다.

```text
Lovable (관제센터) → Supabase (Source of Truth) → n8n (Orchestrator) → Python 브릿지 → ComfyUI / RTX 5080
                                                     ├→ LLM (프롬프트·캡션·판단)
                                                     └→ SNS API (V1)
```

## 문서

| 문서 | 내용 |
|---|---|
| [docs/PRD.md](docs/PRD.md) | 제품 정의 v1.0: 문제, 목표·지표, 사용자, 기능, 자동화 시나리오, 범위(MVP·V1·V2), 로드맵 |
| [docs/TECH_DESIGN.md](docs/TECH_DESIGN.md) | 기술 설계 v1.0: 아키텍처, DB, State Machine, API, ComfyUI·n8n 명세, 보안, 구현 계획, UI/UX, Frontend, Python 실행 계층 |
| [docs/n8n_guide.md](docs/n8n_guide.md) | n8n 연동·터널 설정 가이드 (현재 브릿지 기준) |
| [supabase/README.md](supabase/README.md) | DB 마이그레이션 적용·테스트 방법 |

## 폴더 구조

```text
app/                   Python 브릿지 (M2): FastAPI /v1, GPU Worker, ComfyUI·Supabase 연동
workflows/             ComfyUI Workflow 템플릿 + registry.json
supabase/migrations/   DB 마이그레이션 0001~0007
supabase/tests/stubs/  로컬 테스트 전용 Supabase 흉내 스키마
tests/db/              DB 테스트
tests/bridge/          브릿지 테스트 (실제 DB + 가짜 ComfyUI)
n8n/                   n8n Workflow JSON (예전 브릿지 기준, M3에서 개편)
docs/                  PRD, 기술 설계, 가이드
```

## 진행 상황

| Milestone | 상태 |
|---|---|
| PRD v1.0 (1~8) | ✅ |
| 기술 설계 v1.0 (9~19) | ✅ (17 UI/UX, 18 Frontend, 19 Python 실행 계층 포함) |
| M0 Environment (Supabase, Cloudflare Tunnel, n8n 서버, Lovable) | 대기 (계정·결제가 필요한 직접 작업) |
| M1 Database Foundation | ✅ 로컬 테스트 통과 · 실제 Supabase 적용 전 |
| M2 Python Bridge v1 | ✅ 로컬 테스트 통과 · 실제 ComfyUI·Supabase 연결 전 |
| M3 n8n Workflows | 다음 |
| M4 Lovable · M5 MVP 통합 | 대기 |

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
