"""유크라테스 그래머 계약의 로컬 고정본.

원본(read-only 참조): /Users/yedulab/opencrab-study/opencrab
  - grammar/manifest.py  : SPACES / META_EDGES / GRAMMAR_VERSION
  - grammar/validator.py : ValidationResult / validate_node / validate_edge /
                           get_allowed_relations

이 모듈은 유크라테스의 검증 **의미론**만 가져오고 원격 스토어 패키지 그래프
(neo4j/mongo/postgres, 스키마 레지스트리 로더)는 가져오지 않는다. 그래머 원본은
유팩에 동봉된 manifest.json 스냅샷이며, 팩 전용 선언(schema_declare,
schema_pack_install)은 호출 인자로만 확장되고 스냅샷을 변형하지 않는다.

원본과의 의도적 차이:
  - 사용자 대면 메시지는 한국어(유팩 도구 계약)로 지역화했다.
  - ValidationResult에 code를 더해 호출자가 메시지 대신 사유로 분기한다.
  - node_type이 다른 공간의 정본 타입이면 '허용 목록에 없음'이 아니라
    wrong_space로 구분해 돌려준다(유팩 기존 오류 안내 유지).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST: dict = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
GRAMMAR_VERSION: str = MANIFEST["version"]

PROVENANCE: dict = {
    "source": "opencrab (Yucrates) grammar/manifest.py + grammar/validator.py",
    "source_repo": "/Users/yedulab/opencrab-study/opencrab",
    "grammar_version": GRAMMAR_VERSION,
    "snapshot": "yupack_mcp/manifest.json",
    "ported": ["ValidationResult", "validate_node", "validate_edge", "get_allowed_relations"],
    "not_ported": ["stores/* (neo4j·mongo·postgres)", "schemas/loader.py 타입 스키마 레지스트리",
                   "rebac·metadata layer 검증"],
}

# 공간별 정본 노드 타입 (원본 _SPACE_NODE_TYPES)
_SPACE_NODE_TYPES: dict[str, set[str]] = {
    space: set(info["node_types"]) for space, info in MANIFEST["spaces"].items()
}

# (from_space, to_space) -> 허용 관계 (원본 _EDGE_RELATION_MAP)
_EDGE_RELATION_MAP: dict[tuple[str, str], set[str]] = {}
for _edge in MANIFEST["meta_edges"]:
    _EDGE_RELATION_MAP.setdefault((_edge["from_space"], _edge["to_space"]), set()).update(
        _edge["relations"])


@dataclass
class ValidationResult:
    """그래머 검증 결과 (원본 grammar/validator.py ValidationResult)."""

    valid: bool
    error: str | None = None
    code: str | None = None

    def raise_if_invalid(self) -> None:
        if not self.valid:
            raise ValueError(self.error or "Validation failed")

    def __bool__(self) -> bool:
        return self.valid


def space_for_node_type(node_type: str) -> str | None:
    """노드 타입의 정본 공간을 돌려준다 (원본 manifest.space_for_node_type)."""
    for space, types in _SPACE_NODE_TYPES.items():
        if node_type in types:
            return space
    return None


def schema_pack_types(packs: list[str] | None) -> list[str]:
    """설치된 스키마팩이 여는 노드 타입 (공간 제약 없음)."""
    return [t for name in (packs or []) for t in MANIFEST["schema_packs"].get(name, [])]


def allowed_node_types(space_id: str, extra_types: dict[str, list[str]] | None = None) -> list[str]:
    """스냅샷 정본 타입 + 호출자가 선언한 확장 타입."""
    return sorted(_SPACE_NODE_TYPES.get(space_id, set())) + list((extra_types or {}).get(space_id, []))


def get_allowed_relations(from_space: str, to_space: str,
                          extra_relations: list[dict] | None = None) -> list[str] | None:
    """공간 쌍의 허용 관계. 쌍 자체가 미선언이면 None(원본은 빈 리스트).

    유팩은 '쌍 미선언'과 '관계 미허용'을 다른 안내로 갈라야 해서 None으로 구분한다.
    """
    relations = set(_EDGE_RELATION_MAP.get((from_space, to_space), set()))
    declared = (from_space, to_space) in _EDGE_RELATION_MAP
    for entry in extra_relations or []:
        if entry.get("from_space") == from_space and entry.get("to_space") == to_space:
            relations.update(entry.get("relations", []))
            declared = True
    return sorted(relations) if declared else None


def validate_node(space_id: str, node_type: str,
                  extra_types: dict[str, list[str]] | None = None) -> ValidationResult:
    """space_id 안에서 node_type이 유효한지 검사한다 (원본 validate_node)."""
    if space_id not in _SPACE_NODE_TYPES:
        known = ", ".join(sorted(_SPACE_NODE_TYPES))
        return ValidationResult(False, f"알 수 없는 space: {space_id}. 사용 가능: {known}",
                                "unknown_space")

    canonical = space_for_node_type(node_type)
    if canonical and canonical != space_id:
        return ValidationResult(
            False,
            f"'{node_type}'의 정본 공간은 '{canonical}'입니다. space='{canonical}'로 추가하세요.",
            "wrong_space")

    if node_type not in allowed_node_types(space_id, extra_types):
        return ValidationResult(
            False, f"'{space_id}' 공간에 선언되지 않은 노드 타입: {node_type}",
            "node_type_not_allowed")

    return ValidationResult(True)


def validate_edge(from_space: str, to_space: str, relation: str,
                  extra_relations: list[dict] | None = None) -> ValidationResult:
    """공간 쌍 사이에서 relation이 유효한지 검사한다 (원본 validate_edge)."""
    for side, space in (("source", from_space), ("target", to_space)):
        if space not in _SPACE_NODE_TYPES:
            return ValidationResult(False, f"알 수 없는 {side} space: {space}", "unknown_space")

    allowed = get_allowed_relations(from_space, to_space, extra_relations)
    if allowed is None:
        return ValidationResult(
            False, f"'{from_space}'→'{to_space}' 공간 쌍에 선언된 관계가 없습니다.",
            "pair_not_declared")

    if relation not in allowed:
        return ValidationResult(
            False,
            f"'{from_space}'→'{to_space}' 간 허용되지 않은 관계: {relation}. 허용된 관계: {allowed}",
            "relation_not_allowed")

    return ValidationResult(True)


def describe() -> dict[str, Any]:
    """계약 출처와 그래머 요약 (원본 describe_grammar의 로컬 축약본)."""
    return {"provenance": PROVENANCE, "version": GRAMMAR_VERSION,
            "spaces": MANIFEST["spaces"], "meta_edges": MANIFEST["meta_edges"]}
