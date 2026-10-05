"""실행 전·후 검증 (TECH_DESIGN 13.10, 13.11)."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from app.comfyui.registry import WorkflowSpec
from app.errors import JobError, validation

FORMAT_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def combo_options(object_info: dict, node: str, input_name: str) -> list | None:
    """object_info에서 선택 입력의 선택지를 꺼낸다. 예전 형식([[...]])과 새 형식(["COMBO", {"options": [...]}]) 모두."""
    try:
        entry = object_info[node]["input"]["required"][input_name]
    except (KeyError, TypeError):
        try:
            entry = object_info[node]["input"]["optional"][input_name]
        except (KeyError, TypeError):
            return None
    if isinstance(entry, list) and entry:
        if isinstance(entry[0], list):
            return entry[0]
        if entry[0] == "COMBO" and len(entry) > 1 and isinstance(entry[1], dict):
            return entry[1].get("options", [])
    return None


def check_models(spec: WorkflowSpec, values: dict, object_info: dict) -> None:
    """ComfyUI에 실제로 있는 모델·LoRA 파일인지 실행 전에 확인한다 (13.10)."""
    for name, model in spec.models.items():
        options = combo_options(object_info, model["node"], model["input"])
        if options is None:
            raise validation("WORKFLOW_INVALID", f"node {model['node']} is not installed in ComfyUI")
        if values.get(name) not in options:
            code = "LORA_NOT_FOUND" if "lora" in name else "MODEL_NOT_FOUND"
            raise validation(code, f"{values.get(name)} is not in {model['node']}.{model['input']} options")


@dataclass
class OutputInfo:
    mime: str
    width: int
    height: int


def output_invalid(message: str) -> JobError:
    return JobError(message, error_type="generation", error_code="OUTPUT_INVALID", retryable=True)


def validate_output(data: bytes, spec: WorkflowSpec, width: int | None, height: int | None) -> OutputInfo:
    """크기·형식·열림·해상도 확인 (13.11)."""
    min_bytes = int(spec.output.get("min_bytes", 10240))
    if len(data) < min_bytes:
        raise output_invalid(f"output too small: {len(data)} bytes (min {min_bytes})")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            fmt, size = img.format, img.size
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise output_invalid(f"output is not a readable image: {exc}") from exc
    mime = FORMAT_MIME.get(fmt or "")
    if mime not in spec.output["mime"]:
        raise output_invalid(f"unexpected output format {fmt} (expected {spec.output['mime']})")
    if spec.output.get("check_dimensions", True) and width and height and size != (width, height):
        raise output_invalid(f"output size {size[0]}x{size[1]} != requested {width}x{height}")
    return OutputInfo(mime=mime, width=size[0], height=size[1])


def validate_input_image(data: bytes) -> str:
    """Operator가 올린 참조 이미지: 확장자가 아니라 실제 내용으로 확인한다 (15.17)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise validation("INPUT_NOT_FOUND", f"input file is not a valid image: {exc}") from exc
    mime = FORMAT_MIME.get(fmt or "")
    if not mime:
        raise validation("INPUT_NOT_FOUND", f"unsupported input image format: {fmt}")
    return MIME_EXT[mime]


def make_thumbnail(data: bytes, max_side: int = 512) -> bytes:
    """긴 변 512px WebP (14.10)."""
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=82)
        return out.getvalue()
