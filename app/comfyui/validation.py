"""실행 전·후 검증 (TECH_DESIGN 13.10, 13.11)."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from app.comfyui.registry import MAX_OUTPUT_BYTES_LIMIT, WorkflowSpec
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


OUTPUT_SUBFOLDER = "pa"
DEFAULT_MAX_OUTPUT_BYTES = MAX_OUTPUT_BYTES_LIMIT  # 50MB. media 버킷 한도 이하로만 둔다 (47.3 3번)


def output_unexpected(message: str) -> JobError:
    """ComfyUI가 예상 밖의 파일 이름·폴더를 돌려줌. 재시도해도 같으므로 재시도하지 않는다 (47.3 2번)."""
    return JobError(message, error_type="validation", error_code="OUTPUT_UNEXPECTED", retryable=False)


def check_output_names(files: list[dict], job_id: str, spec: WorkflowSpec) -> None:
    """출력 파일 이름을 허용 목록으로 확인한다 (TECH_DESIGN 47.3 2번).

    브릿지가 filename_prefix를 `pa/{job_id}`로 고정하므로(13.8) ComfyUI SaveImage가 만드는 이름은
    subfolder `pa`, filename `{job_id}_NNNNN_.{확장자}`뿐이다. 하나라도 다르면 /view를 한 번도 부르지 않고 실패한다.
    (/view는 파일 이름 끝 표기 ` [input]` 등으로 다른 폴더를 읽을 수 있다.)
    """
    exts = "|".join(sorted(re.escape(MIME_EXT[m]) for m in spec.output["mime"] if m in MIME_EXT)) or "png"
    pattern = re.compile(rf"^{re.escape(job_id)}_[0-9]{{5}}_\.(?:{exts})$")
    for f in files:
        if f.get("subfolder", "") != OUTPUT_SUBFOLDER or not pattern.fullmatch(str(f.get("filename", ""))):
            raise output_unexpected("ComfyUI returned an output file name outside the allowed pattern")


def validate_output(data: bytes, spec: WorkflowSpec, width: int | None, height: int | None) -> OutputInfo:
    """크기·형식·열림·해상도 확인 (13.11)."""
    min_bytes = int(spec.output.get("min_bytes", 10240))
    max_bytes = int(spec.output.get("max_bytes", DEFAULT_MAX_OUTPUT_BYTES))
    if len(data) > max_bytes:  # 업로드·디코드 전에 거른다. 재시도해도 같은 결과라 재시도하지 않는다 (47.3 3번)
        raise JobError(f"output too large: {len(data)} bytes (max {max_bytes})", error_type="validation",
                       error_code="OUTPUT_TOO_LARGE", retryable=False)
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
