"""Workflow Capability 요구사항 현황 (TECH_DESIGN 56.5).

`workflows/registry.json`의 각 Workflow에 `requirements`(min_vram_gb, required_files)가 채워져 있는지 보여 준다.
**값을 만들어 채우지 않는다.** 비어 있으면 UNSET으로 표시하고, 채우려면 어떤 측정이 필요한지 알려 준다.
registry·템플릿은 읽기만 한다.

  python scripts/report_capability_requirements.py          # 사람이 읽는 표
  python scripts/report_capability_requirements.py --json   # 기계가 읽는 JSON
  python scripts/report_capability_requirements.py --strict # UNSET이 있으면 종료 코드 1 (배포 점검용)

required_files "후보"는 템플릿에 **직접 적힌**(자리표시자가 아닌) 모델 파일 이름이다. Persona가 고르는 체크포인트·
LoRA는 `models`/`{{…}}`로 들어가므로 여기에 나오지 않는다 — 그건 Workflow가 고정적으로 요구하는 파일이 아니다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.comfyui.registry import PLACEHOLDER_RE, load_registry  # noqa: E402

MODEL_FILE_RE = re.compile(r"\.(safetensors|ckpt|pt|pth|onnx|bin|gguf)$", re.IGNORECASE)


def literal_model_files(template: dict) -> list[dict]:
    """템플릿 노드 입력 중 자리표시자가 아닌 모델 파일 이름 → {node, input, file}."""
    found = []
    for node in template.values():
        if not isinstance(node, dict):
            continue
        for key, value in (node.get("inputs") or {}).items():
            if isinstance(value, str) and MODEL_FILE_RE.search(value) and not PLACEHOLDER_RE.search(value):
                found.append({"node": node.get("class_type", "?"), "input": key, "file": value})
    return found


def report(workflow_dir: Path) -> list[dict]:
    rows = []
    for wid, spec in load_registry(workflow_dir).items():
        req = spec.requirements or {}
        candidates = literal_model_files(spec.template)
        declared = req.get("required_files")
        rows.append({
            "workflow": wid,
            "enabled": spec.enabled,
            "min_vram_gb": req.get("min_vram_gb", "UNSET"),
            "required_files": declared if declared is not None else "UNSET",
            "required_files_candidates": candidates,
            "model_inputs_from_persona": sorted(spec.models),
            "node_count": len(spec.template),
            "params_that_drive_vram": sorted(p for p in spec.params if p in
                                             ("width", "height", "batch_size", "steps", "frames", "length")),
        })
    return rows


MEASURE = """\
측정 방법 (workflow별로 채우려면)
  min_vram_gb
    1. 대상 GPU(Target)에서 ComfyUI를 새로 띄운다 (다른 모델이 VRAM을 차지하지 않게 /free로 비운다).
    2. 그 Workflow를 **레지스트리가 허용하는 최대 파라미터**로 실행한다 (width·height 최대, batch_size 최대, 해당되면 frames 최대).
    3. 실행 중 최대 VRAM 사용량을 기록한다: `nvidia-smi --query-gpu=memory.used --format=csv -l 1` 의 최댓값.
       (ComfyUI /system_stats의 vram_free 변화로도 확인할 수 있다.)
    4. 최댓값에 여유 약 10%를 더해 올림한 값이 min_vram_gb다. OOM이 나지 않는 가장 작은 GPU로 교차 확인한다.
  required_files
    Workflow가 **고정적으로** 요구하는 파일만 적는다 (예: 얼굴 교체 모델, 업스케일러). Persona가 고르는 체크포인트·LoRA는 넣지 않는다.
    각 항목은 {node, input, file} 이고, 그 파일이 ComfyUI의 /object_info 에서 해당 노드 입력의 선택지로 보이는지 확인한다.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="UNSET이 하나라도 있으면 종료 코드 1")
    parser.add_argument("--dir", type=Path, default=ROOT / "workflows")
    args = parser.parse_args()

    rows = report(args.dir)
    unset = [r["workflow"] for r in rows if r["min_vram_gb"] == "UNSET"]
    if args.json:
        print(json.dumps({"workflows": rows, "unset_min_vram": unset}, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print(f"{r['workflow']}{'' if r['enabled'] else '  (disabled)'}")
            print(f"  min_vram_gb:  {r['min_vram_gb']}")
            print(f"  required_files: {r['required_files']}")
            if r["required_files_candidates"]:
                print("    템플릿에 직접 적힌 파일 후보 (확인 후 required_files로 옮길지 결정):")
                for c in r["required_files_candidates"]:
                    print(f"      - {c['node']}.{c['input']} = {c['file']}")
            print(f"  Persona가 고르는 모델: {r['model_inputs_from_persona'] or '(없음)'}")
            print(f"  VRAM에 영향 주는 파라미터: {r['params_that_drive_vram'] or '(없음)'}")
            print()
        print(MEASURE)
        print(f"min_vram_gb UNSET: {len(unset)}/{len(rows)} → {', '.join(unset) or '없음'}")
    return 1 if (args.strict and unset) else 0


if __name__ == "__main__":
    raise SystemExit(main())
