"""보호 파일 무결성 (최종 게이트 9번).

적용된 마이그레이션 0001~0009, PRD, F0 테스트, n8n WF-001/002/004/005/006, Workflow registry·템플릿,
TECH_DESIGN 9~55장은 이 작업에서 바뀌면 안 된다. `tests/protected_manifest.json`(git HEAD에서 만든 sha256)과
작업 트리를 비교한다. 줄바꿈(CRLF/LF) 차이는 무시한다. 의도적으로 바꿀 때만 매니페스트를 다시 만든다
(`python scripts/protected_manifest.py --write-from-head`, CLAUDE.md 18조 절차).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "protected_manifest.json").read_text(encoding="utf-8"))

_spec = importlib.util.spec_from_file_location("protected_manifest", ROOT / "scripts" / "protected_manifest.py")
pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm)


def test_the_manifest_covers_every_protected_file():
    """보호 패턴에 걸리는 파일이 매니페스트에서 빠지거나, 사라진 파일이 매니페스트에 남아 있지 않다."""
    assert sorted(MANIFEST["files"]) == pm.working_files()
    migrations = [p for p in MANIFEST["files"] if p.startswith("supabase/migrations/")]
    assert len(migrations) == 9                       # 0001 ~ 0009. 0010·0011은 보호 대상이 아니다
    assert len([p for p in MANIFEST["files"] if p.startswith("n8n/")]) == 5   # WF-001/002/004/005/006 (WF-003은 갱신 대상)


@pytest.mark.parametrize("path", sorted(MANIFEST["files"]))
def test_protected_file_is_unchanged(path):
    actual = pm.sha((ROOT / path).read_bytes())
    assert actual == MANIFEST["files"][path], f"{path} was modified — protected files must stay as they were"


def test_tech_design_sections_9_to_55_are_unchanged():
    data = (ROOT / pm.TECH_DESIGN).read_bytes()
    prefix = pm.tech_design_prefix(data)
    import hashlib
    assert hashlib.sha256(prefix).hexdigest() == MANIFEST["tech_design_9_to_55"], \
        "TECH_DESIGN sections 9-55 were modified — only section 56 may change"


def test_hashing_ignores_line_endings_but_detects_any_other_change():
    assert pm.sha(b"a\r\nb\r\n") == pm.sha(b"a\nb\n")
    assert pm.sha(b"a\nb\n") != pm.sha(b"a\nb \n")           # 공백 한 칸도 잡는다
    assert pm.sha(b"a\nb\n") != pm.sha(b"a\nB\n")


def test_tech_design_prefix_stops_exactly_at_section_56():
    sample = b"## 55. x\nbody\n\n## 56. y\nnew\n"
    assert pm.tech_design_prefix(sample) == b"## 55. x\nbody\n"
    assert pm.tech_design_prefix(sample.replace(b"new", b"changed")) == pm.tech_design_prefix(sample)   # 56장 변경은 허용
    assert pm.tech_design_prefix(sample.replace(b"body", b"edited")) != pm.tech_design_prefix(sample)   # 55장 변경은 잡는다


def test_the_manifest_does_not_protect_the_files_this_work_must_change():
    """WF-003, 0010, 0011, TECH_DESIGN 56장은 이 작업의 대상이라 매니페스트에 없어야 한다."""
    assert not [p for p in MANIFEST["files"] if "pa_003" in p or "0010" in p or "0011" in p]
