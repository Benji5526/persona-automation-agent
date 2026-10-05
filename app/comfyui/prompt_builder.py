"""Prompt Builder (TECH_DESIGN 13.7).

LLM이 만든 구성 요소(prompt_parts)를 정해진 순서로 이어 붙인다. 같은 입력이면 항상 같은 문자열이 나온다.
"""

from __future__ import annotations

PART_ORDER = ("subject", "appearance", "outfit", "location", "action", "camera", "lighting", "mood", "style")


def _clean(value: object) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def build_prompt(parts: dict | None) -> str:
    if not parts:
        return ""
    return ", ".join(p for p in (_clean(parts.get(k)) for k in PART_ORDER) if p)


def build_negative(*sources: object) -> str:
    """여러 출처(Persona 기본값, Content Job, LLM 추가 항목)를 순서대로 합치고 중복을 뺀다."""
    seen: list[str] = []
    for source in sources:
        if not source:
            continue
        items = source if isinstance(source, (list, tuple)) else str(source).split(",")
        for item in items:
            text = _clean(item)
            if text and text.lower() not in (s.lower() for s in seen):
                seen.append(text)
    return ", ".join(seen)
