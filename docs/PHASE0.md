# Phase 0 — 환경 구축 점검과 진행 순서

> 근거: [TECH_DESIGN 44.5](TECH_DESIGN.md) (Sprint 1 0~6번), 25장(PC), 26장(n8n 서버), 24장(Supabase). 이 문서는 **실제 환경을 점검한 결과**와 **지금 막힌 것**을 적는다. 점검일 2026-10-07, 읽기 전용 점검이며 운영에 영향을 주는 작업은 하지 않았다.

## 1. 점검 결과

| 항목 | 현재 | 기대 (설계) | 판정 |
|---|---|---|---|
| 코드 테스트 | `pytest tests -q` **107개 통과** (70초) | 44.5 0번 | ✅ |
| Python venv, `requirements-dev.txt` | 있음 | 25.3 | ✅ |
| Node / npm | v24.20 / 11.19 | `npx supabase` 실행용 | ✅ |
| `cloudflared` | 2026.8.3 설치됨 | 25.5 | ✅ (터널은 아직 안 만듦) |
| `gh` / `git` | 있음 | | ✅ |
| Supabase CLI | **없음** (`npx supabase`로 대체 가능) | `supabase link`·`db push` (24장) | ⚠ 필요할 때 실행 |
| Docker | **없음** | n8n은 **원격 VPS**에서 실행 (26장) | ✅ (이 PC엔 불필요) |
| `.env` | **없음** (`.env.example`만) | 25.3 | ❌ Supabase 키가 생긴 뒤 |
| ComfyUI | `D:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable`, 체크포인트 `flux1-dev-fp8.safetensors`(약 16GB) 하나, **지금 실행 중 아님** | 48.2 | ⚠ |
| **GPU** | **NVIDIA GeForce RTX 3070 Laptop, VRAM 8GB** (드라이버 592.82). 시스템 RAM 15.7GB | **RTX 5080, VRAM 16GB** (PRD·25장·`WORKER_ID` 기본값 `python:rtx5080-1`) | ❌ 아래 2-1 |
| **Supabase 프로젝트** | MCP(`D:\Git\.mcp.json`)가 가리키는 `yaonpdxrlsigdnqfdujf`는 **다른 앱(주식 분석)의 데이터가 있는 프로젝트** (`companies` 3,998행, `analyses`, `disclosures` …). 이 프로젝트의 마이그레이션 0001~0008은 **적용되어 있지 않다** | persona-automation-agent 전용 **새 프로젝트** (24.2) | ❌ 아래 2-2 |
| n8n 서버, Cloudflare Tunnel, Meta 앱 | 없음 | 26장, 25.5, 28.14 | 대기 (사람) |

## 2. 막힌 것 (결정이 필요하다)

### 2-1. 생성 PC의 GPU가 설계와 다르다

설계 전체가 **RTX 5080(16GB)** 기준이다. 이 PC는 **RTX 3070 Laptop(8GB)** 이다.

- 지금 설치된 `flux1-dev-fp8`(체크포인트 약 16GB, 확산 모델 약 11.9GB + T5 약 4.8GB)은 8GB VRAM에 올라가지 않는다. ComfyUI가 RAM으로 내려서 돌릴 수는 있지만(RAM 15.7GB) **매우 느리고 메모리 부족이 날 가능성이 크다.** 48.2의 "16GB에 들어갈 가능성이 크다"는 판단은 이 PC에는 맞지 않는다.
- **SDXL 계열(체크포인트 약 6.5GB)** 은 8GB에 들어간다. 48.2의 선택지 중 SDXL이 이 PC의 현실적인 선택이다. 신원 LoRA도 SDXL용으로 학습해야 한다 (48.2).
- 5080이 **다른 PC**에 있다면 이 점검 결과는 그 PC에서 다시 해야 한다.

필요한 결정: ① 생성은 이 PC(3070 8GB)에서 한다 → SDXL로 가고 `.env`의 `WORKER_ID`를 `python:rtx3070-1` 등으로 바꾼다. ② 5080이 있는 PC에서 한다 → 그 PC에서 같은 점검을 다시 한다.

### 2-2. Supabase 프로젝트를 새로 만들어야 한다

`yaonpdxrlsigdnqfdujf`에는 다른 서비스의 실제 데이터가 있고 RLS가 이미 켜진 테이블 36개가 있다. 여기에 이 프로젝트의 마이그레이션을 적용하면 안 된다 (`profiles`·`users` 같은 이름이 겹치고, 그 데이터가 위험해진다).

- **`D:\Git\.mcp.json`의 Supabase MCP는 이 작업에 쓰지 않는다.** 새 프로젝트를 만든 뒤 `D:\Projects\persona-automation-agent\.mcp.json`에 새 프로젝트의 `project_ref`로 따로 연결한다 (프로젝트 범위, 이름 `supabase`가 겹치므로 한 세션에서는 하나만 켠다).
- 새 프로젝트를 만드는 일은 사람이 한다 (계정·요금제·리전 선택). 24.3의 순서를 따른다.

## 3. 진행 순서 (44.5 0~6번과 대응)

| # | 할 일 | 누가 | 상태 |
|---|---|---|---|
| 0 | 코드 F0: 36.12 MVP 수정(`persona_isolation` 0009), 45.6·46.7·47.3·49.5·50.5·51.5 테스트·보강. 첫 `db push` **전에** | Claude Code | ✅ 구현·테스트 통과 (아래 4번). 리뷰 반영 후 커밋 |
| 1 | **Supabase 새 프로젝트** 생성 → Google OAuth(Google Cloud), 이메일 로그인 끔, 가입 허용 목록 → `supabase link` → `db push` → admin 지정 → `verify_production.sql` 1~14 | 사람 + Claude Code(순서 안내·결과 해석) | ❌ 프로젝트가 없다 |
| 2 | PC: 드라이버 ✅, ComfyUI(`--listen 127.0.0.1`), 체크포인트(2-1의 결정), `.env` 채우기, ComfyUI 화면에서 수동 생성 (48.8) | 사람 | ⚠ 2-1 결정 후 |
| 3 | 첫 생성 (n8n 없이): SQL로 Persona·Content Job·generation Job → 브릿지 `POST /v1/jobs` → Content Job `ready`, Asset 1행 (25.6) | 사람 + Claude Code | 1·2번 뒤 |
| 4 | Cloudflare Named Tunnel, Access Service Token (25.5) | 사람 | 대기 |
| 5 | n8n 서버(VPS), Credential, import, DB Webhook 2개, 백업 timer (26.3) | 사람 + Claude Code(배포 파일) | 대기 |
| 6 | 화면 없이 파이프라인 (가짜 LLM) | 사람 | 대기 |

지금 바로 할 수 있는 것은 **0번(코드 F0)** 이다. 1번은 새 Supabase 프로젝트가 생기면 시작한다.

## 4. 코드 F0 결과 (2026-10-07)

`pytest tests -q` **142개 통과** (이전 107개 + 35개). 독립 리뷰 지적(8건)을 반영했다.

| 항목 | 변경 | 근거 |
|---|---|---|
| 마이그레이션 `0009_persona_isolation.sql` | `automation_jobs`·`posts`의 Persona 일치 트리거(`service_role` 직접 INSERT도 거부), 보관 Persona의 `retry_content_job`·`regenerate_content_job`·`retry_automation_job` 거부(`PT422`), `assets (persona_id, created_at desc)` index | 36.12, 47.3 4번, 50.5 2번 |
| 브릿지 `app/worker.py` | ① Content Job과 Job의 Persona가 다르면 ComfyUI 호출 전 `INPUT_NOT_FOUND` ② **이미 등록된 Asset이 있는 Job은 다시 생성하지 않고 완료**(`RECOVERED`) ③ 출력 파일 이름 허용 목록(`pa/{job_id}_NNNNN_.png`) 위반은 `/view` 호출 없이 `OUTPUT_UNEXPECTED`(재시도 없음, `security_events`) ④ `VALIDATE`·`UPLOAD`의 `duration_ms` | 36.12, 47.3 2번, 49.5 1·2번 |
| `app/comfyui/validation.py`, `workflows/registry.json` | 출력 크기 상한 `output.max_bytes`(50MB), `OUTPUT_TOO_LARGE`(재시도 없음) | 47.3 3번 |
| `app/database.py` | `get_job_assets`, `log_security_event(actor_type)` | |
| `tests/db/test_f0_isolation.py` (28개) | 45.6 직접 수정·삭제·위조 `user_id`·Storage, 46.7 전이·재시도·재생성·종료 상태, 36.12 Persona 일치, 47.3 4번, 49.5 3번 동시 선점(연결 4개), 50.5 1번 Asset, 51.5 3번 Post·Social Account | |
| `tests/bridge` (+7개) | 위 브릿지 변경 | |
| `supabase/verify_production.sql` | 19번: 0009 트리거·index 확인 | |

**아직 안 한 F0**: 첫 실제 생성 때 Registry 상한(해상도 1536·steps 50·batch 1)을 맞추는 일 (MVP_SCOPE_LOCK 열린 결정 2).
