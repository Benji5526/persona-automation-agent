"""저장소 아키텍처 불변식 (최종 게이트 6·7번, TECH_DESIGN 56).

코드·설정·문서를 훑어서 확정한 아키텍처를 깨는 변경이 들어오면 실패한다. 동작은 tests/db·tests/bridge가 검증하고,
여기서는 "저장소에 이런 것이 있으면 안 된다 / 반드시 있어야 한다"만 본다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.config import CLOUD_FORBIDDEN_ENV, Settings

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".sql", ".md", ".json", ".yml", ".yaml", ".example", ".txt", ".toml", ".ps1", ""}
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "data", "node_modules"}


def repo_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS) \
                and (path.suffix in TEXT_SUFFIXES or path.name.endswith(".example")):
            yield path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# -----------------------------------------------------------------------------
# D4: active_worker 개념은 활성 아키텍처에서 사라졌다
# -----------------------------------------------------------------------------
# 이 단어를 "제거됐다"고 설명하거나 부재를 단언하는 파일만 허용한다
ACTIVE_WORKER_MAY_APPEAR_IN = {
    "CLAUDE.md", "docs/TECH_DESIGN.md", "docs/architecture/execution-targets.md",
    ".claude/skills/pa-supabase/SKILL.md", "supabase/migrations/0010_execution_targets.sql",
    "supabase/verify_production.sql", "tests/test_architecture_invariants.py",
    # 아래 테스트는 active_worker가 **없음**을 단언한다 (아래 test_...only_asserts_its_absence가 그 사실을 확인한다)
    "tests/db/test_execution_targets.py", "tests/db/test_migrations_upgrade.py", "tests/db/test_verify_production_sql.py",
}
ABSENCE_ASSERTING_TESTS = {"tests/db/test_execution_targets.py", "tests/db/test_migrations_upgrade.py",
                           "tests/db/test_verify_production_sql.py"}


def test_active_worker_survives_only_as_a_removed_concept():
    offenders = [rel(p) for p in repo_files() if "active_worker" in read(p) and rel(p) not in ACTIVE_WORKER_MAY_APPEAR_IN]
    assert offenders == []


def test_the_tests_that_mention_active_worker_only_assert_its_absence():
    """허용된 테스트 파일이 active_worker를 '있어야 하는 것'으로 쓰지 않는다: 모든 언급은 0건·없음 단언이다."""
    allowed_shapes = ("== 0", "not in", "{\"app_mode\"", "{'app_mode'", "# act", "'active_worker'")
    for name in ABSENCE_ASSERTING_TESTS:
        for number, line in enumerate(read(ROOT / name).splitlines(), 1):
            if "active_worker" in line:
                assert any(shape in line for shape in allowed_shapes), f"{name}:{number}: {line.strip()}"
                assert "update_app_setting('active_worker'" not in line, f"{name}:{number} sets it"


def test_no_runtime_code_reads_or_writes_an_active_worker_setting():
    code = [p for p in repo_files() if p.suffix == ".py" and rel(p).startswith("app/")]
    assert [rel(p) for p in code if "active_worker" in read(p)] == []


# -----------------------------------------------------------------------------
# D5·D7: 비밀과 service_role
# -----------------------------------------------------------------------------
def test_the_worker_code_never_builds_a_service_role_request_in_jwt_mode():
    """worker_jwt 경로(auth_headers)는 secret key를 헤더에 넣지 않는다."""
    from app.config import auth_headers
    headers = auth_headers(secret_key="sb_secret_SHOULD_NOT_APPEAR", worker_jwt="wjwt", anon_key="anon")
    assert "sb_secret_SHOULD_NOT_APPEAR" not in json.dumps(headers)


def test_no_real_looking_secrets_are_committed():
    """sb_secret_/JWT/개인키 모양의 값. 마스킹 테스트용 가짜 값(tests/)과 정규식 정의는 제외한다."""
    pattern = re.compile(r"sb_secret_[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
                         r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
    # 로컬 .env는 git에서 제외된 파일이다(아래 테스트가 그 사실을 확인한다). "커밋된" 파일만 본다.
    hits = [rel(p) for p in repo_files()
            if not rel(p).startswith("tests/") and rel(p) not in GIT_IGNORED_LOCAL_FILES and pattern.search(read(p))]
    assert hits == []


GIT_IGNORED_LOCAL_FILES = {".env"}


def test_the_local_files_skipped_above_really_are_git_ignored():
    ignored = {line.strip() for line in read(ROOT / ".gitignore").splitlines()}
    assert GIT_IGNORED_LOCAL_FILES <= ignored


def parse_env(path: Path) -> dict[str, str]:
    values = {}
    for line in read(path).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def test_env_examples_hold_no_values_for_secrets():
    for path in ROOT.rglob(".env.example"):
        if set(path.relative_to(ROOT).parts) & SKIP_DIRS:
            continue
        for key, value in parse_env(path).items():
            if any(w in key for w in ("KEY", "SECRET", "TOKEN", "JWT", "PASSWORD")):
                assert value == "", f"{rel(path)}: {key} must be empty in an example file"


def test_the_cloud_worker_env_example_contains_only_what_a_pod_may_hold():
    env = parse_env(ROOT / "deploy" / "cloud-worker.env.example")
    assert env["WORKER_AUTH"] == "worker_jwt" and env["WORKER_TARGET"] == "cloud"
    assert set(env) & set(CLOUD_FORBIDDEN_ENV) == set(), "a forbidden variable is set in the cloud example"
    assert "SUPABASE_SECRET_KEY" not in env and "N8N_CALLBACK_TOKEN" not in env
    assert env["COMFY_URL"].startswith("http://127.0.0.1")
    assert env["BRIDGE_HOST"] == "127.0.0.1"
    # 값만 채우면 실제로 기동 검증을 통과하는 모양이다
    filled = {**env, "WORKER_JWT": "wjwt", "SUPABASE_ANON_KEY": "anon", "BRIDGE_TOKENS": "t" * 40,
              "SUPABASE_URL": "https://ref.supabase.co"}
    Settings(supabase_url=filled["SUPABASE_URL"], supabase_secret_key="", bridge_tokens=(filled["BRIDGE_TOKENS"],),
             worker_auth=filled["WORKER_AUTH"], worker_jwt=filled["WORKER_JWT"],
             supabase_anon_key=filled["SUPABASE_ANON_KEY"], worker_target=filled["WORKER_TARGET"],
             comfy_url=filled["COMFY_URL"])


def test_the_local_env_example_defaults_to_the_local_worker():
    env = parse_env(ROOT / ".env.example")
    assert env["WORKER_AUTH"] == "secret_key" and env["WORKER_TARGET"] == "local"
    assert env["COMFY_URL"].startswith("http://127.0.0.1")
    assert "rtx" not in " ".join(env.values()).lower()           # GPU 모델을 고정하지 않는다


# -----------------------------------------------------------------------------
# D7: ComfyUI와 Python 실행 엔드포인트는 공개되지 않는다
# -----------------------------------------------------------------------------
def test_comfyui_and_the_bridge_are_never_bound_publicly_by_default():
    for path in repo_files():
        if rel(path).startswith("tests/") or path.suffix not in {".py", ".yml", ".yaml", ".example", ".md"}:
            continue
        text = read(path)
        assert "--listen 0.0.0.0" not in text, rel(path)
        assert not re.search(r"BRIDGE_HOST\s*=\s*0\.0\.0\.0", text), rel(path)
        assert not re.search(r"COMFY_URL\s*=\s*https?://(?!127\.0\.0\.1|localhost)", text), rel(path)


def test_settings_reject_a_public_comfyui_address():
    from app.config import ConfigError
    with pytest.raises(ConfigError):
        Settings(supabase_url="https://r.supabase.co", supabase_secret_key="k", bridge_tokens=("t" * 40,),
                 comfy_url="http://10.0.0.5:8188")


# -----------------------------------------------------------------------------
# D2·D6: n8n은 보조 경로이고 비즈니스 로직을 갖지 않는다
# -----------------------------------------------------------------------------
def workflow(name_part: str) -> dict:
    path = next((ROOT / "n8n").glob(f"*{name_part}*.json"))
    return json.loads(read(path))


def test_wf003_is_a_disabled_local_only_fallback():
    wf = workflow("pa_003")
    assert wf["active"] is False
    triggers = {n["name"]: n for n in wf["nodes"]
                if n["type"].endswith((".webhook", ".scheduleTrigger"))}
    assert len(triggers) == 2 and all(n.get("disabled") is True for n in triggers.values())
    text = json.dumps(wf, ensure_ascii=False)
    assert "execution_target=eq.local" in text                       # 폴링 질의는 local만
    conditions = [c for n in wf["nodes"] if n["type"].endswith(".if")
                  for c in n["parameters"]["conditions"]["conditions"]]
    assert any("execution_target" in c["leftValue"] and c["rightValue"] == "local" for c in conditions)


def test_no_other_workflow_picks_an_execution_target():
    """n8n·LLM이 Target을 고르지 않는다. Target은 설정 기본값이거나 사람의 명시 선택이다 (D3)."""
    for path in (ROOT / "n8n").glob("*.json"):
        if "pa_003" in path.name:
            continue
        assert "execution_target" not in read(path), rel(path)


def test_n8n_workflows_embed_no_credentials():
    for path in (ROOT / "n8n").glob("*.json"):
        wf = json.loads(read(path))
        for node in wf["nodes"]:
            creds = node.get("credentials") or {}
            for name, cred in creds.items():
                assert set(cred) <= {"id", "name"}, f"{rel(path)}: credential values must not be inlined ({name})"


# -----------------------------------------------------------------------------
# D1·D3·D8: 파이프라인과 Personal Edition
# -----------------------------------------------------------------------------
def test_no_saas_tables_exist_in_the_active_migrations():
    sql = " ".join(read(p) for p in (ROOT / "supabase" / "migrations").glob("*.sql")).lower()
    for table in ("organizations", "memberships", "subscriptions", "tenants", "billing", "customer_quotas"):
        assert not re.search(rf"create table (public\.)?{table}\b", sql), table


def test_python_execution_layer_does_not_pick_the_target_itself():
    """Python은 local/cloud를 고르지 않는다 — 보고만 한다. 선점 질의에 target을 넘기는 코드가 없다."""
    database = read(ROOT / "app" / "database.py")
    claim = re.findall(r'_rpc\("claim_(?:next_)?automation_job",\s*\{[^}]*\}', database)
    assert claim and not any("target" in c for c in claim)


def test_lovable_does_not_execute_python_or_comfyui():
    """프런트엔드 명세는 브릿지·ComfyUI를 부르라고 하지 않는다 (D1). 부르지 말라는 문구만 있어야 한다."""
    text = read(ROOT / "docs" / "lovable_master_prompt.md")
    assert "create_content_job" in text
    for line in text.splitlines():
        if re.search(r"localhost:8188|/v1/jobs", line):
            assert re.search(r"(?i)(never|not|no |find|search|nothing|금지|않)", line), line
