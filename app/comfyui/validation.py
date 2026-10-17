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


# GPU 보고값은 드라이버마다 조금씩 다르다. 아주 작은 차이로 막지 않는다.
VRAM_TOLERANCE_GB = 0.1


def unsupported_on_target(message: str) -> JobError:
    """이 Worker에서는 이 Workflow를 실행할 수 없다 (TECH_DESIGN 56.7).

    Capability 불일치는 같은 Target에서 다시 해도 같은 결과다 → **재시도하지 않는다**.
    실행 중에 터지는 OUT_OF_MEMORY(일시적, 재시도 가능)와는 다른 종류의 실패다.
    """
    return JobError(message, error_type="validation", error_code="WORKFLOW_UNSUPPORTED_ON_TARGET",
                    retryable=False)


def vram_total_gb(system_stats: dict) -> float | None:
    """ComfyUI /system_stats가 보고한 첫 장치의 전체 VRAM (GB). GPU를 못 찾으면 None."""
    devices = system_stats.get("devices") or []
    total = (devices[0] or {}).get("vram_total") if devices else None
    return round(total / 2**30, 2) if total else None


def check_capability(spec: WorkflowSpec, object_info: dict, system_stats: dict, target: str = "local") -> None:
    """실행 전 Capability 검증 (56.7): VRAM과 Workflow가 고정적으로 요구하는 파일.

    Workflow = 구조 + 고정 파일 / Persona = 모델·LoRA 선택 / Worker = 실제 설치 상태.
    모델·LoRA 값 검증은 check_models가 맡는다 (MODEL_NOT_FOUND·LORA_NOT_FOUND).
    """
    requirements = spec.requirements or {}

    min_vram = requirements.get("min_vram_gb")
    if min_vram:
        have = vram_total_gb(system_stats)
        if have is None:
            raise unsupported_on_target(
                f"{spec.id} needs {min_vram} GB VRAM but this {target} worker reported no GPU")
        if have + VRAM_TOLERANCE_GB < float(min_vram):
            raise unsupported_on_target(
                f"{spec.id} needs {min_vram} GB VRAM, this {target} worker has {have} GB")

    for entry in requirements.get("required_files") or []:
        options = combo_options(object_info, entry["node"], entry["input"])
        if options is None:
            raise unsupported_on_target(
                f"{spec.id} needs node {entry['node']}, which is not installed on this {target} worker")
        if entry["file"] not in options:
            raise unsupported_on_target(
                f"{spec.id} needs {entry['file']} in {entry['node']}.{entry['input']}, "
                f"which is not installed on this {target} worker")


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
