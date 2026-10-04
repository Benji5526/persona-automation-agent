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
| [docs/TECH_DESIGN.md](docs/TECH_DESIGN.md) | 기술 설계 v1.0: 아키텍처, DB, State Machine, API, ComfyUI·n8n 명세, 보안, 구현 계획, UI/UX(초안) |
| [docs/n8n_guide.md](docs/n8n_guide.md) | n8n 연동·터널 설정 가이드 (현재 브릿지 기준) |
| [supabase/README.md](supabase/README.md) | DB 마이그레이션 적용·테스트 방법 |

## 폴더 구조

```text
supabase/migrations/   DB 마이그레이션 (M1, 새 스키마)
supabase/tests/stubs/  로컬 테스트 전용 Supabase 흉내 스키마
tests/db/              DB 테스트
tests/bridge/          브릿지 테스트 (가짜 ComfyUI·Supabase)
src/comfy_bridge.py    Python 브릿지 (현재 버전, M2에서 app/으로 개편)
workflows/             ComfyUI Workflow 템플릿
n8n/                   n8n Workflow JSON (현재 버전, M3에서 개편)
database/schema.sql    초기 스키마 (현재 브릿지용, M2에서 supabase/migrations로 대체 후 삭제)
docs/                  PRD, 기술 설계, 가이드
```

## 진행 상황

| Milestone | 상태 |
|---|---|
| PRD v1.0 (1~8) | ✅ |
| 기술 설계 v1.0 (9~16) | ✅ · 17 UI/UX는 초안 |
| M0 Environment (Supabase, Cloudflare Tunnel, n8n 서버, Lovable) | 대기 (계정·결제가 필요한 직접 작업) |
| M1 Database Foundation | ✅ 로컬 테스트 통과 · 실제 Supabase 적용 전 |
| M2 Python Bridge v1 | 다음 |
| M3 n8n Workflows · M4 Lovable · M5 MVP 통합 | 대기 |

## 테스트

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest tests -q
```

DB 테스트는 Docker 없이 내장 PostgreSQL(`pgserver`)로 실행됩니다.
