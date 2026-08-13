"""유크라테스 저작 파이프라인의 로컬 어댑터.

원본(read-only 참조): /Users/yedulab/opencrab-study/opencrab
  - ontology/builder.py   : OntologyBuilder — 그래머 검증 → receipt → 스토어 쓰기 순서
  - ontology/promotion.py : PromotionEngine — candidate → validated → promoted / rejected

원격 스토어 fan-out(Neo4j·MongoDB·PostgreSQL)만 팩 버퍼 + 로컬 SQLite로 교체한다.
완성 ZIP은 이 경로에서 열지도 쓰지도 않는다 (저작은 draft 버퍼에서만 일어난다).

원본과의 의도적 차이:
  - validate_candidate/promote/reject는 existing_properties를 인자로 받지 않고 로컬
    버퍼에서 바로 읽는다. 원격 스토어 왕복이 없어 호출자가 상태를 들고 다닐 이유가 없다.
  - rejected 후보는 원본처럼 status만 바꾸지 않고 버퍼에서 빼 draft audit에만 남긴다.
    로컬은 버퍼 자체가 완성 ZIP의 재료라서, 남겨 두면 질의 산출물로 새어 나간다.
  - 그래머 위반은 ValueError 대신 GrammarError(세부 안내 포함)로 올려 MCP 도구가
    기존 error 딕트 계약을 그대로 유지한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import yucrates_contract as contract

# 승격 전 상태 — 이 상태의 노드가 있으면 완성 ZIP을 만들지 않는다.
UNSAVABLE_STATUSES = ("candidate", "validated")

# 승격 시 Evidence를 어떤 관계로 투영할지 (그래머: evidence→claim supports, evidence→kinetic records)
EVIDENCE_PROJECTION = {"claim": "supports", "concept": "supports", "kinetic": "records"}


class AuthoringError(ValueError):
    """저작 계약 위반. MCP 도구는 이 예외를 error 딕트로 바꿔 돌려준다."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class GrammarError(AuthoringError):
    """그래머(공간·타입·관계) 위반."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _receipt() -> tuple[str, str]:
    return f"rcpt_{uuid.uuid4().hex[:12]}", _now_iso()


def _extra_types(buffer: dict) -> dict[str, list[str]]:
    """팩 커스텀 타입 + 설치 스키마팩 타입(스키마팩은 공간 제약 없이 열린다)."""
    custom = buffer.get("custom_types") or {}
    pack_types = contract.schema_pack_types(buffer.get("schema_packs"))
    spaces = set(contract.MANIFEST["spaces"]) | set(custom)
    return {space: list(custom.get(space, [])) + pack_types for space in spaces}


def _extra_relations(buffer: dict) -> list[dict]:
    return list(buffer.get("custom_relations") or [])


def unpromoted_node_ids(buffer: dict) -> list[str]:
    """승격 전(candidate·validated) 노드 id. 비어야 완성 ZIP을 만들 수 있다."""
    return sorted(nid for nid, node in buffer.get("nodes", {}).items()
                  if (node.get("properties") or {}).get("status") in UNSAVABLE_STATUSES)


class LocalAuthoringStore:
    """저작 버퍼(nodes/edges) + 로컬 SQLite 영속. 완성 ZIP은 접촉하지 않는다."""

    def __init__(self, pack: str, buffer: dict, persist: Callable[[], str]) -> None:
        self.pack = pack
        self.buffer = buffer
        self._persist = persist

    def get_node(self, node_id: str) -> dict | None:
        return self.buffer["nodes"].get(node_id)

    def upsert_node(self, space: str, node_type: str, node_id: str,
                    properties: dict) -> dict:
        node = {"space": space, "node_type": node_type, "properties": properties}
        self.buffer["nodes"][node_id] = node
        return node

    def drop_node(self, node_id: str) -> tuple[dict | None, list[dict]]:
        """노드와 그 노드에 붙은 엣지를 버퍼에서 제거한다 (rejected 전용)."""
        node = self.buffer["nodes"].pop(node_id, None)
        attached = [e for e in self.buffer["edges"]
                    if node_id in (e["from_id"], e["to_id"])]
        if attached:
            self.buffer["edges"] = [e for e in self.buffer["edges"]
                                    if node_id not in (e["from_id"], e["to_id"])]
        return node, attached

    def append_edge(self, from_space: str, from_id: str, relation: str, to_space: str,
                    to_id: str, properties: dict) -> tuple[dict, bool]:
        """같은 (from, relation, to)는 다시 쌓지 않고 속성만 갱신한다."""
        for edge in self.buffer["edges"]:
            if (edge["from_id"], edge["relation"], edge["to_id"]) == (from_id, relation, to_id):
                if properties:
                    edge["properties"] = {**edge.get("properties", {}), **properties}
                return edge, False
        edge = {"from_space": from_space, "from_id": from_id, "relation": relation,
                "to_space": to_space, "to_id": to_id, "properties": properties}
        self.buffer["edges"].append(edge)
        return edge, True

    def audit(self, node_id: str, lifecycle_status: str, **detail: Any) -> dict:
        """draft 전용 감사 기록. 완성 ZIP 산출물에는 포함되지 않는다."""
        row = {"node_id": node_id, "lifecycle_status": lifecycle_status,
               "ts": _now_iso(), **detail}
        self.buffer.setdefault("authoring_audit", []).append(row)
        return row

    def persist(self) -> str:
        return self._persist()


class LocalOntologyBuilder:
    """opencrab OntologyBuilder 이식본 — 검증이 언제나 쓰기보다 먼저 실행된다."""

    def __init__(self, store: LocalAuthoringStore) -> None:
        self._store = store

    @property
    def buffer(self) -> dict:
        return self._store.buffer

    def add_node(self, space: str, node_type: str, node_id: str,
                 properties: dict | None = None) -> dict:
        props = properties or {}
        result = contract.validate_node(space, node_type, _extra_types(self.buffer))
        if not result:
            raise GrammarError(result.error, **self._node_details(result, space, node_type))

        existing = self._store.get_node(node_id)
        if existing and (existing["space"] != space or existing["node_type"] != node_type):
            raise AuthoringError(
                f"id 충돌: '{node_id}'는 이미 {existing['space']}/{existing['node_type']} 노드입니다. "
                "기존 노드를 덮어쓰지 않습니다. 다른 id를 사용하세요.")

        receipt_id, receipt_ts = _receipt()
        node_data = self._store.upsert_node(space, node_type, node_id, props)
        return {"node_id": node_id, "space": space, "node_type": node_type,
                "properties": props, "receipt_id": receipt_id, "receipt_ts": receipt_ts,
                "stores": {"buffer": "ok", "sqlite": self._store.persist()},
                "node_data": {node_id: node_data}}

    def add_edge(self, from_space: str, from_id: str, relation: str, to_space: str,
                 to_id: str, properties: dict | None = None) -> dict:
        for side, node_id, space in (("from", from_id, from_space), ("to", to_id, to_space)):
            node = self._store.get_node(node_id)
            if not node:
                raise AuthoringError(
                    f"{side} 노드가 팩에 없습니다: '{node_id}'. ontology_add_node로 먼저 추가하세요.")
            if node["space"] != space:
                raise AuthoringError(
                    f"{side} 노드 '{node_id}'의 실제 공간은 '{node['space']}'입니다 ('{space}' 아님). "
                    f"{side}_space='{node['space']}'로 다시 시도하세요.")

        result = contract.validate_edge(from_space, to_space, relation,
                                        _extra_relations(self.buffer))
        if not result:
            raise GrammarError(result.error,
                               **self._edge_details(result, from_space, to_space, relation))

        receipt_id, receipt_ts = _receipt()
        edge, created = self._store.append_edge(from_space, from_id, relation, to_space,
                                                to_id, properties or {})
        return {"from": {"space": from_space, "id": from_id}, "relation": relation,
                "to": {"space": to_space, "id": to_id}, "created": created,
                "receipt_id": receipt_id, "receipt_ts": receipt_ts,
                "stores": {"buffer": "ok", "sqlite": self._store.persist()}, "edge": edge}

    # --- 오류 안내 (기존 MCP 도구 응답 형태 유지) ---
    def _node_details(self, result: contract.ValidationResult, space: str,
                      node_type: str) -> dict:
        if result.code == "unknown_space":
            return {"spaces": sorted(contract.MANIFEST["spaces"])}
        if result.code == "wrong_space":
            return {"hint": "예: Claim은 claim 공간, TextUnit은 evidence 공간 소속입니다."}
        return {"declared_types": contract.MANIFEST["spaces"][space]["node_types"],
                "custom_types": (self.buffer.get("custom_types") or {}).get(space, []),
                "hint": f'커스텀 타입은 schema_declare(node_types={{"{space}": ["{node_type}"]}})로 '
                        "먼저 선언한 뒤 추가하세요. 도메인 묶음은 schema_pack_install 참고."}

    def _edge_details(self, result: contract.ValidationResult, from_space: str,
                      to_space: str, relation: str) -> dict:
        if result.code != "pair_not_declared":
            return {}
        pairs = sorted({(me["from_space"], me["to_space"])
                        for me in contract.MANIFEST["meta_edges"] + _extra_relations(self.buffer)
                        if me["from_space"] == from_space})
        return {"declared_pairs_from_here": [f"{a}→{b}" for a, b in pairs],
                "hint": f'schema_declare(relations=[{{"from_space": "{from_space}", '
                        f'"to_space": "{to_space}", "relations": ["{relation}"]}}])로 먼저 선언하세요.'}


class LocalPromotionEngine:
    """opencrab PromotionEngine 이식본 — 상태는 노드 properties['status']에 남는다."""

    def __init__(self, builder: LocalOntologyBuilder, store: LocalAuthoringStore) -> None:
        self._builder = builder
        self._store = store

    def _require(self, node_id: str) -> dict:
        node = self._store.get_node(node_id)
        if not node:
            raise AuthoringError(f"저작 버퍼에 노드가 없습니다: {node_id}")
        return node

    def register_candidate(self, space: str, node_type: str, node_id: str,
                           properties: dict | None = None, confidence: float | None = None,
                           source_id: str | None = None) -> dict:
        props = {**(properties or {})}
        props.setdefault("status", "candidate")
        if confidence is not None:
            props["confidence"] = round(min(max(confidence, 0.0), 1.0), 3)
        if source_id is not None:
            props["source_id"] = source_id
        result = self._builder.add_node(space, node_type, node_id, props)
        result["lifecycle_status"] = "candidate"
        return result

    def validate_candidate(self, node_id: str, validator_id: str | None = None,
                           note: str | None = None) -> dict:
        node = self._require(node_id)
        props = {**node["properties"], "status": "validated", "validated_at": _now_iso()}
        if validator_id:
            props["validated_by"] = validator_id
        if note:
            props["validation_note"] = note
        result = self._builder.add_node(node["space"], node["node_type"], node_id, props)
        result["lifecycle_status"] = "validated"
        return result

    def promote(self, node_id: str, promoted_by: str | None = None,
                evidence_ids: list[str] | None = None) -> dict:
        node = self._require(node_id)
        space = node["space"]
        receipt_id, receipt_ts = _receipt()
        props = {**node["properties"], "status": "promoted", "promoted_at": receipt_ts}
        if promoted_by:
            props["promoted_by"] = promoted_by
        refs = list(props.get("evidence_refs") or [])
        refs += [ev for ev in (evidence_ids or []) if ev not in refs]
        if refs:
            props["evidence_refs"] = refs

        result = self._builder.add_node(space, node["node_type"], node_id, props)
        result["lifecycle_status"] = "promoted"
        result["promotion_receipt_id"] = receipt_id
        result["promotion_receipt_ts"] = receipt_ts

        links = []
        for evidence_id in evidence_ids or []:
            relation = EVIDENCE_PROJECTION.get(space, "supports")
            try:
                self._builder.add_edge("evidence", evidence_id, relation, space, node_id)
                links.append({"evidence_id": evidence_id, "relation": relation,
                              "status": "linked"})
            except AuthoringError as exc:
                links.append({"evidence_id": evidence_id, "status": f"error: {exc}"})
        result["evidence_links"] = links
        return result

    def reject(self, node_id: str, rejected_by: str | None = None,
               reason: str | None = None) -> dict:
        node = self._require(node_id)
        props = {**node["properties"], "status": "rejected", "rejected_at": _now_iso()}
        if rejected_by:
            props["rejected_by"] = rejected_by
        if reason:
            props["rejection_reason"] = reason
        dropped, edges = self._store.drop_node(node_id)
        row = self._store.audit(node_id, "rejected", reason=reason, rejected_by=rejected_by,
                                node={**(dropped or {}), "properties": props}, edges=edges)
        return {"node_id": node_id, "lifecycle_status": "rejected", "audit": row,
                "dropped_edges": len(edges),
                "stores": {"buffer": "ok", "sqlite": self._store.persist()}}


# --- 팩 이름으로 어댑터를 얻는 진입점 (server는 이 모듈을 먼저 임포트하므로 지연 임포트) ---
def store_for(pack: str) -> LocalAuthoringStore:
    from . import server

    return LocalAuthoringStore(pack, server._get_pack(pack), lambda: server._persist(pack))


def builder_for(pack: str) -> LocalOntologyBuilder:
    return LocalOntologyBuilder(store_for(pack))


def engine_for(pack: str) -> LocalPromotionEngine:
    store = store_for(pack)
    return LocalPromotionEngine(LocalOntologyBuilder(store), store)
