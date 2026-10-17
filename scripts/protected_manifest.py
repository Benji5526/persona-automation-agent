"""보호 파일 해시 매니페스트 (최종 게이트 9번).

보호 대상(적용된 마이그레이션 0001~0009, PRD, F0 테스트, n8n WF-001/002/004/005/006, Workflow registry·템플릿,
TECH_DESIGN 9~55장)의 sha256을 `tests/protected_manifest.json`에 고정한다. 줄바꿈 차이(CRLF/LF)는 무시한다.
`tests/test_protected_files.py`가 이 매니페스트와 작업 트리를 비교해서, 보호 파일이 바뀌면 테스트가 실패한다.

  python scripts/protected_manifest.py --write-from-head   # git HEAD에서 매니페스트를 만든다 (작업 트리 변경과 무관)
  python scripts/protected_manifest.py --check             # 작업 트리와 비교한다

보호 파일을 **의도적으로** 바꿀 때만 매니페스트를 다시 만든다. 그 변경은 CLAUDE.md 18조 절차(설계 문서 먼저)를 따른다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tests" / "protected_manifest.json"

PROTECTED_GLOBS = [
    "supabase/migrations/000[1-9]_*.sql",
    "docs/PRD.md",
    "tests/db/test_f0_isolation.py",
    "n8n/pa_001_*.json", "n8n/pa_002_*.json", "n8n/pa_004_*.json", "n8n/pa_005_*.json", "n8n/pa_006_*.json",
    "workflows/*.json",
]
TECH_DESIGN = "docs/TECH_DESIGN.md"
TECH_DESIGN_END_MARKER = "\n## 56. "      # 9~55장은 이 줄 앞까지. 56장은 갱신 대상이다


def normalize(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(normalize(data)).hexdigest()


def tech_design_prefix(data: bytes) -> bytes:
    text = normalize(data)
    marker = TECH_DESIGN_END_MARKER.encode("utf-8")
    index = text.find(marker)
    if index < 0:
        raise SystemExit("TECH_DESIGN: '## 56.' header not found")
    return text[:index]


def find_git() -> str:
    for candidate in (shutil.which("git"), r"C:\Program Files\Git\cmd\git.exe", "/usr/bin/git"):
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("git not found")


def git_show(path: str) -> bytes:
    return subprocess.run([find_git(), "show", f"HEAD:{path}"], cwd=ROOT, capture_output=True, check=True).stdout


def git_files() -> list[str]:
    out = subprocess.run([find_git(), "ls-tree", "-r", "--name-only", "HEAD"], cwd=ROOT,
                         capture_output=True, check=True, text=True, encoding="utf-8").stdout.splitlines()
    from fnmatch import fnmatch
    return sorted(p for p in out if any(fnmatch(p, g) for g in PROTECTED_GLOBS))


def working_files() -> list[str]:
    paths: set[str] = set()
    for glob in PROTECTED_GLOBS:
        paths.update(p.relative_to(ROOT).as_posix() for p in ROOT.glob(glob))
    return sorted(paths)


def build(read) -> dict:
    files = {p: sha(read(p)) for p in sorted(read.paths)}
    return {"files": files, "tech_design_9_to_55": hashlib.sha256(tech_design_prefix(read(TECH_DESIGN))).hexdigest()}


class FromHead:
    paths = property(lambda self: git_files())

    def __call__(self, p: str) -> bytes:
        return git_show(p)


class FromWorkingTree:
    paths = property(lambda self: working_files())

    def __call__(self, p: str) -> bytes:
        return (ROOT / p).read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-from-head", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.write_from_head:
        manifest = build(FromHead())
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {MANIFEST.relative_to(ROOT)}: {len(manifest['files'])} files + TECH_DESIGN 9-55")
        return 0

    expected = json.loads(MANIFEST.read_text(encoding="utf-8"))
    actual = build(FromWorkingTree())
    bad = [p for p in sorted(set(expected["files"]) | set(actual["files"]))
           if expected["files"].get(p) != actual["files"].get(p)]
    if expected["tech_design_9_to_55"] != actual["tech_design_9_to_55"]:
        bad.append("docs/TECH_DESIGN.md (sections 9-55)")
    for p in bad:
        print("CHANGED:", p)
    print("protected files unchanged" if not bad else f"{len(bad)} protected file(s) changed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
