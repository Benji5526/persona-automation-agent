"""Workflow Registry (TECH_DESIGN 13.3).

workflows/registry.json + 템플릿 JSON을 읽고, 시작할 때 형식을 검증한다.
템플릿의 동적값은 "{{name}}" 자리표시자이고, 이름은 params·models·inputs·filename_prefix 중 하나여야 한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ID_RE = re.compile(r"^[a-z0-9_]+$")
FILE_RE = re.compile(r"^[a-z0-9_]+\.json$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
BUILTIN_PLACEHOLDERS = {"filename_prefix"}


class RegistryError(ValueError):
    pass


@dataclass
class WorkflowSpec:
    id: str
    version: str
    type: str
    stage: str
    enabled: bool
    file: str
    template: dict
    params: dict[str, dict] = field(default_factory=dict)
    models: dict[str, dict] = field(default_factory=dict)
    inputs: dict[str, dict] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    oom_fallback: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @property
    def node_types(self) -> set[str]:
        return {node["class_type"] for node in self.template.values() if isinstance(node, dict) and "class_type" in node}

    @property
    def placeholders(self) -> set[str]:
        return set(PLACEHOLDER_RE.findall(json.dumps(self.template)))

    def sync_payload(self, enabled: bool) -> dict:
        """comfy_workflows 테이블로 보낼 값 (12.5)."""
        return {"version": self.version, "type": self.type, "stage": self.stage, "enabled": enabled,
                "params": self.params, "inputs": self.inputs}


def load_registry(workflow_dir: Path) -> dict[str, WorkflowSpec]:
    path = workflow_dir / "registry.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc

    specs: dict[str, WorkflowSpec] = {}
    for wf_id, entry in raw.items():
        if wf_id.startswith("$"):
            continue  # "$comment" 등
        if not ID_RE.match(wf_id):
            raise RegistryError(f"invalid workflow id: {wf_id!r}")
        file = entry.get("file", f"{wf_id}.json")
        if not FILE_RE.match(file):
            raise RegistryError(f"{wf_id}: invalid file name {file!r}")
        try:
            template = json.loads((workflow_dir / file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RegistryError(f"{wf_id}: cannot read template {file}: {exc}") from exc

        spec = WorkflowSpec(
            id=wf_id, version=str(entry["version"]), type=entry["type"], stage=entry.get("stage", "mvp"),
            enabled=bool(entry.get("enabled", True)), file=file, template=template,
            params=entry.get("params", {}), models=entry.get("models", {}), inputs=entry.get("inputs", {}),
            output=entry.get("output", {}), oom_fallback=entry.get("oom_fallback", {}), notes=entry.get("notes", ""),
        )
        allowed = set(spec.params) | set(spec.models) | set(spec.inputs) | BUILTIN_PLACEHOLDERS
        unknown = spec.placeholders - allowed
        if unknown:
            raise RegistryError(f"{wf_id}: template placeholders not declared in registry: {sorted(unknown)}")
        if not spec.output.get("mime"):
            raise RegistryError(f"{wf_id}: output.mime is required")
        for name, model in spec.models.items():
            if not {"node", "input"} <= set(model):
                raise RegistryError(f"{wf_id}: models.{name} needs node and input")
        specs[wf_id] = spec
    return specs


def missing_nodes(spec: WorkflowSpec, object_info: dict) -> set[str]:
    """ComfyUI에 설치되지 않은 노드 (커스텀 노드 미설치 등)."""
    return {t for t in spec.node_types if t not in object_info}
