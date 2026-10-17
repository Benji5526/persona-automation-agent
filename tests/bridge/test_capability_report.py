"""Workflow Capability 요구사항 현황 (최종 게이트 4번).

registry.json의 요구사항이 **측정되기 전**에는 UNSET이다. 이 테스트는 값을 만들어 채우지 않고, 현황 보고가 사실을
정확히 말하는지만 확인한다. 값을 채우는 일은 대상 GPU에서 측정한 뒤 registry에 적는다 (scripts/report_capability_requirements.py).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.comfyui.registry import load_registry
from tests.bridge.conftest import ROOT

_spec = importlib.util.spec_from_file_location("capability_report", ROOT / "scripts" / "report_capability_requirements.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


def rows():
    return {r["workflow"]: r for r in report.report(ROOT / "workflows")}


def test_every_registered_workflow_is_reported():
    assert set(rows()) == set(load_registry(ROOT / "workflows"))


def test_unset_requirements_are_marked_unset_not_invented():
    """현재 registry에는 requirements가 없다. 보고는 UNSET이라고 말해야지 기본값을 지어내면 안 된다."""
    for wid, row in rows().items():
        declared = load_registry(ROOT / "workflows")[wid].requirements
        if not declared:
            assert row["min_vram_gb"] == "UNSET" and row["required_files"] == "UNSET", wid


def test_literal_model_files_are_reported_only_as_candidates():
    faceswap = rows()["faceswap_v1"]
    candidates = {(c["node"], c["input"], c["file"]) for c in faceswap["required_files_candidates"]}
    assert ("ReActorFaceSwap", "swap_model", "inswapper_128.onnx") in candidates
    assert faceswap["required_files"] == "UNSET"              # 후보일 뿐 요구사항으로 확정하지 않는다


def test_persona_selected_models_are_not_reported_as_required_files():
    """체크포인트·LoRA는 Persona 설정이다 — Workflow가 고정적으로 요구하는 파일이 아니다."""
    for wid in ("image_generation_v1", "image_generation_lora_v1", "image_to_image_v1"):
        assert rows()[wid]["required_files_candidates"] == [], wid
    assert rows()["image_generation_lora_v1"]["model_inputs_from_persona"] == ["checkpoint", "lora_name"]


def test_declared_requirements_are_reported_as_declared(tmp_path: Path):
    (tmp_path / "w.json").write_text(json.dumps({"1": {"class_type": "X", "inputs": {}}}))
    (tmp_path / "registry.json").write_text(json.dumps({"w": {
        "version": "1", "type": "image", "file": "w.json", "output": {"mime": ["image/png"]},
        "requirements": {"min_vram_gb": 12, "required_files": []}}}))
    row = report.report(tmp_path)[0]
    assert row["min_vram_gb"] == 12 and row["required_files"] == []


def test_strict_mode_fails_while_anything_is_unset(capsys):
    with pytest.raises(SystemExit) as exc:
        import sys
        sys.argv = ["report", "--strict"]
        raise SystemExit(report.main())
    assert exc.value.code == 1                                  # 배포 점검: 측정 전에는 통과시키지 않는다
