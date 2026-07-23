"""yupack 로컬 거버넌스 도구 구현.

모든 상태 변경은 열린 팩의 runtime SQLite와 overlay에만 기록한다.
정본 nodes.jsonl 및 기타 원본 데이터 파일은 수정하지 않는다.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import uuid

from . import local_pack


_WORKFLOW_STAGES = ("authoring", "validation", "independent_review", "promotion")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _get_pack(pack_handle: str):
    pk = local_pack.get(pack_handle)
    if pk is None:
        return None, {"error": f"핸들 없음: {pack_handle}. 먼저 pack_open_local을 호출하세요."}
    return pk, None


def _detail(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _row_dict(row: sqlite3.Row | tuple, columns: tuple[str, ...]) -> dict:
    return dict(zip(columns, row))


def _find_promotion(con: sqlite3.Connection, candidate_id: str):
    return con.execute(
        "SELECT candidate_id, target, status, validation, ts "
        "FROM promotions WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()


def workflow_create_run(pack_handle: str, kind: str) -> dict:
    """authoring 상태의 로컬 workflow run을 생성한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if not kind or not kind.strip():
        return {"error": "workflow kind가 비어 있습니다."}
    run_id = f"run_{uuid.uuid4().hex}"
    ts = _now()
    try:
        with pk._runtime_db() as con:
            con.execute(
                "INSERT INTO workflow_runs(run_id, kind, state, ts) VALUES(?,?,?,?)",
                (run_id, kind, "authoring", ts),
            )
        pk._audit("workflow_create_run", _detail({"run_id": run_id, "kind": kind}))
        return {"run_id": run_id, "kind": kind, "state": "authoring", "ts": ts}
    except Exception as exc:
        return {"error": f"workflow 생성 실패: {exc}"}


def workflow_advance(pack_handle: str, run_id: str) -> dict:
    """workflow를 정해진 한 단계만 전진시킨다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    try:
        with pk._runtime_db() as con:
            row = con.execute(
                "SELECT kind, state, ts FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return {"error": f"workflow run 없음: {run_id}"}
            current = row[1]
            try:
                next_state = _WORKFLOW_STAGES[_WORKFLOW_STAGES.index(current) + 1]
            except (ValueError, IndexError):
                return {"error": f"workflow를 더 전진할 수 없습니다: {current}"}
            con.execute(
                "UPDATE workflow_runs SET state = ?, ts = ? WHERE run_id = ?",
                (next_state, _now(), run_id),
            )
        pk._audit("workflow_advance", _detail({"run_id": run_id, "from": current, "to": next_state}))
        return {"run_id": run_id, "kind": row[0], "previous_state": current,
                "state": next_state}
    except Exception as exc:
        return {"error": f"workflow 전진 실패: {exc}"}


def approval_request(pack_handle: str, target: str, reason: str) -> dict:
    """승인 요청을 pending으로 기록한다. 자동 승인하지 않는다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if not target or not reason:
        return {"error": "approval target과 reason은 필수입니다."}
    ts = _now()
    try:
        with pk._runtime_db() as con:
            cur = con.execute(
                "INSERT INTO approvals(target, status, ts) VALUES(?,?,?)",
                (target, "pending", ts),
            )
            approval_id = cur.lastrowid
        pk._audit("approval_request", _detail({"approval_id": approval_id,
                                                 "target": target, "reason": reason,
                                                 "status": "pending"}))
        return {"approval_id": approval_id, "target": target, "reason": reason,
                "status": "pending", "ts": ts}
    except Exception as exc:
        return {"error": f"approval 요청 실패: {exc}"}


def harness_promotion_apply(pack_handle: str, candidate_id: str) -> dict:
    """promoted 후보만 로컬 overlay에 적용 표시한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    try:
        with pk._runtime_db() as con:
            row = _find_promotion(con, candidate_id)
            if row is None:
                return {"error": f"promotion 후보 없음: {candidate_id}"}
            if row[2] != "promoted":
                return {"error": "promoted 상태의 후보만 적용할 수 있습니다."}
            overlay_id = f"promotion:{candidate_id}"
            ts = _now()
            con.execute(
                "INSERT OR REPLACE INTO overlay_nodes(id, data, op, ts) VALUES(?,?,?,?)",
                (overlay_id, json.dumps({"candidate_id": candidate_id, "target": row[1]},
                                        ensure_ascii=False), "promotion_applied", ts),
            )
        pk._audit("harness_promotion_apply", _detail({"candidate_id": candidate_id,
                                                        "target": row[1], "overlay_id": overlay_id,
                                                        "status": "applied"}))
        return {"candidate_id": candidate_id, "target": row[1], "status": "applied",
                "overlay_id": overlay_id}
    except Exception as exc:
        return {"error": f"promotion 적용 실패: {exc}"}


def identity_add_alias(pack_handle: str, alias: str, canonical: str) -> dict:
    """identity alias와 canonical id의 연결을 추가한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if not alias or not canonical:
        return {"error": "alias와 canonical은 필수입니다."}
    if alias == canonical:
        return {"error": "alias와 canonical은 달라야 합니다."}
    ts = _now()
    try:
        with pk._runtime_db() as con:
            con.execute(
                "INSERT INTO identity_aliases(alias, canonical, ts) VALUES(?,?,?)",
                (alias, canonical, ts),
            )
        pk._audit("identity_add_alias", _detail({"alias": alias, "canonical": canonical}))
        return {"alias": alias, "canonical": canonical, "ts": ts}
    except Exception as exc:
        return {"error": f"alias 추가 실패: {exc}"}


def identity_resolve_canonical(pack_handle: str, identity: str) -> dict:
    """alias 체인을 따라 최종 canonical id를 찾는다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if not identity:
        return {"error": "identity는 필수입니다."}
    try:
        with pk._runtime_db() as con:
            current = identity
            seen = set()
            chain = []
            while True:
                if current in seen:
                    return {"error": f"identity alias 순환 참조: {identity}"}
                seen.add(current)
                row = con.execute(
                    "SELECT canonical FROM identity_aliases WHERE alias = ? "
                    "ORDER BY rowid DESC LIMIT 1", (current,)
                ).fetchone()
                if row is None:
                    break
                chain.append({"alias": current, "canonical": row[0]})
                current = row[0]
        return {"identity": identity, "canonical": current, "chain": chain}
    except Exception as exc:
        return {"error": f"canonical resolve 실패: {exc}"}


def identity_propose_duplicate(pack_handle: str, a: str, b: str) -> dict:
    """두 identity를 duplicate 후보로 pending 상태에 등록한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if not a or not b or a == b:
        return {"error": "서로 다른 두 identity가 필요합니다."}
    ts = _now()
    try:
        with pk._runtime_db() as con:
            cur = con.execute(
                "INSERT INTO duplicates(a, b, status, ts) VALUES(?,?,?,?)",
                (a, b, "pending", ts),
            )
            duplicate_id = cur.lastrowid
        pk._audit("identity_propose_duplicate", _detail({"duplicate_id": duplicate_id,
                                                           "a": a, "b": b,
                                                           "status": "pending"}))
        return {"duplicate_id": duplicate_id, "a": a, "b": b,
                "status": "pending", "ts": ts}
    except Exception as exc:
        return {"error": f"duplicate 제안 실패: {exc}"}


def identity_resolve_duplicate(pack_handle: str, a: str, b: str, decision: str) -> dict:
    """duplicate 후보를 merge 또는 distinct로 확정한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if decision not in {"merge", "distinct"}:
        return {"error": "decision은 merge 또는 distinct여야 합니다."}
    try:
        with pk._runtime_db() as con:
            cur = con.execute(
                "UPDATE duplicates SET status = ?, ts = ? "
                "WHERE (a = ? AND b = ?) OR (a = ? AND b = ?)",
                (decision, _now(), a, b, b, a),
            )
            if cur.rowcount == 0:
                return {"error": f"duplicate 후보 없음: {a}, {b}"}
        pk._audit("identity_resolve_duplicate", _detail({"a": a, "b": b,
                                                           "decision": decision}))
        return {"a": a, "b": b, "status": decision}
    except Exception as exc:
        return {"error": f"duplicate resolve 실패: {exc}"}


def identity_list_pending_duplicates(pack_handle: str) -> dict:
    """pending duplicate 후보를 나열한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    try:
        with pk._runtime_db() as con:
            rows = con.execute(
                "SELECT id, a, b, status, ts FROM duplicates "
                "WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        columns = ("duplicate_id", "a", "b", "status", "ts")
        return {"duplicates": [_row_dict(row, columns) for row in rows]}
    except Exception as exc:
        return {"error": f"pending duplicate 조회 실패: {exc}"}


def canonicalize_merge_nodes(pack_handle: str, keep_id: str, drop_id: str) -> dict:
    """두 노드를 원본 수정 없이 overlay merge와 alias로 기록한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if keep_id == drop_id:
        return {"error": "keep_id와 drop_id는 달라야 합니다."}
    if keep_id not in pk.nodes or drop_id not in pk.nodes:
        return {"error": "keep_id와 drop_id 모두 원본 노드에 있어야 합니다."}
    ts = _now()
    op = f"merged_into:{keep_id}"
    try:
        with pk._runtime_db() as con:
            con.execute(
                "INSERT OR REPLACE INTO overlay_nodes(id, data, op, ts) VALUES(?,?,?,?)",
                (drop_id, json.dumps({"keep_id": keep_id, "drop_id": drop_id},
                                     ensure_ascii=False), op, ts),
            )
            con.execute(
                "INSERT INTO identity_aliases(alias, canonical, ts) VALUES(?,?,?)",
                (drop_id, keep_id, ts),
            )
        pk._audit("canonicalize_merge_nodes", _detail({"keep_id": keep_id,
                                                         "drop_id": drop_id, "op": op}))
        return {"keep_id": keep_id, "drop_id": drop_id, "op": op, "overlay": True}
    except Exception as exc:
        return {"error": f"노드 merge 실패: {exc}"}


def canonicalize_find_and_propose(pack_handle: str, threshold: float = 0.9) -> dict:
    """label이 정확히 같은 노드쌍을 duplicate pending 후보로 제안한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if threshold > 1.0:
        return {"proposals": [], "threshold": threshold}
    groups: dict[str, list[str]] = {}
    for node_id, node in sorted(pk.nodes.items()):
        props = node.get("properties") or {}
        label = node.get("label") or props.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        normalized = re.sub(r"\s+", "", label).casefold()
        groups.setdefault(normalized, []).append(node_id)

    proposals = []
    try:
        with pk._runtime_db() as con:
            for ids in groups.values():
                for index, a in enumerate(ids):
                    for b in ids[index + 1:]:
                        if len(proposals) >= 20:
                            break
                        existing = con.execute(
                            "SELECT 1 FROM duplicates WHERE "
                            "((a = ? AND b = ?) OR (a = ? AND b = ?)) "
                            "AND status = 'pending' LIMIT 1", (a, b, b, a)
                        ).fetchone()
                        if existing:
                            continue
                        cur = con.execute(
                            "INSERT INTO duplicates(a, b, status, ts) VALUES(?,?,?,?)",
                            (a, b, "pending", _now()),
                        )
                        proposals.append({"duplicate_id": cur.lastrowid, "a": a, "b": b,
                                          "status": "pending", "score": 1.0})
                    if len(proposals) >= 20:
                        break
                if len(proposals) >= 20:
                    break
        if proposals:
            pk._audit("canonicalize_find_and_propose", _detail({"threshold": threshold,
                                                                  "count": len(proposals)}))
        return {"threshold": threshold, "proposals": proposals}
    except Exception as exc:
        return {"error": f"duplicate 탐색 실패: {exc}"}


def promotion_register_candidate(pack_handle: str, node_id: str) -> dict:
    """노드를 독립 검수 전 후보로 등록한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if node_id not in pk.nodes:
        return {"error": f"원본 노드 없음: {node_id}"}
    candidate_id = f"candidate_{uuid.uuid4().hex}"
    ts = _now()
    status = "candidate_not_independently_reviewed"
    try:
        with pk._runtime_db() as con:
            con.execute(
                "INSERT INTO promotions(candidate_id, target, status, validation, ts) "
                "VALUES(?,?,?,?,?)", (candidate_id, node_id, status, None, ts)
            )
        pk._audit("promotion_register_candidate", _detail({"candidate_id": candidate_id,
                                                             "node_id": node_id,
                                                             "status": status}))
        return {"candidate_id": candidate_id, "target": node_id, "status": status, "ts": ts}
    except Exception as exc:
        return {"error": f"promotion 후보 등록 실패: {exc}"}


def promotion_validate_candidate(pack_handle: str, candidate_id: str) -> dict:
    """후보의 로컬 노드와 evidence_refs를 검사하고 validation만 갱신한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    try:
        with pk._runtime_db() as con:
            row = _find_promotion(con, candidate_id)
            if row is None:
                return {"error": f"promotion 후보 없음: {candidate_id}"}
            node = pk.nodes.get(row[1])
            props = (node or {}).get("properties") or {}
            refs = props.get("evidence_refs") or []
            if not isinstance(refs, list):
                refs = [refs]
            refs = [str(ref) for ref in refs if ref]
            validation = {
                "node_exists": node is not None,
                "evidence_refs_present": bool(refs),
                "evidence_refs_resolved": bool(refs) and all(ref in pk.evidence for ref in refs),
                "evidence_ref_count": len(refs),
            }
            validation["valid"] = (
                validation["node_exists"] and validation["evidence_refs_present"]
                and validation["evidence_refs_resolved"]
            )
            con.execute(
                "UPDATE promotions SET validation = ?, ts = ? WHERE candidate_id = ?",
                (json.dumps(validation, ensure_ascii=False), _now(), candidate_id),
            )
            status = row[2]
        pk._audit("promotion_validate_candidate", _detail({"candidate_id": candidate_id,
                                                             "validation": validation,
                                                             "status": status}))
        return {"candidate_id": candidate_id, "status": status, "validation": validation}
    except Exception as exc:
        return {"error": f"promotion 후보 검증 실패: {exc}"}


def promotion_promote(pack_handle: str, candidate_id: str,
                      independent_review: bool = False) -> dict:
    """독립 검수를 명시한 후보만 promoted 상태로 바꾼다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if not independent_review:
        return {"error": "독립 검수 없이 승격 불가"}
    try:
        with pk._runtime_db() as con:
            row = _find_promotion(con, candidate_id)
            if row is None:
                return {"error": f"promotion 후보 없음: {candidate_id}"}
            if row[2] == "rejected":
                return {"error": "거부된 후보는 승격 불가"}
            con.execute(
                "UPDATE promotions SET status = ?, ts = ? WHERE candidate_id = ?",
                ("promoted", _now(), candidate_id),
            )
            target = row[1]
        pk._audit("promotion_promote", _detail({"candidate_id": candidate_id,
                                                  "target": target,
                                                  "status": "promoted",
                                                  "independent_review": True}))
        return {"candidate_id": candidate_id, "target": target,
                "status": "promoted", "independent_review": True}
    except Exception as exc:
        return {"error": f"promotion 승격 실패: {exc}"}


def promotion_reject(pack_handle: str, candidate_id: str) -> dict:
    """promotion 후보를 rejected 상태로 기록한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    try:
        with pk._runtime_db() as con:
            row = _find_promotion(con, candidate_id)
            if row is None:
                return {"error": f"promotion 후보 없음: {candidate_id}"}
            con.execute(
                "UPDATE promotions SET status = ?, ts = ? WHERE candidate_id = ?",
                ("rejected", _now(), candidate_id),
            )
            target = row[1]
        pk._audit("promotion_reject", _detail({"candidate_id": candidate_id,
                                                 "target": target, "status": "rejected"}))
        return {"candidate_id": candidate_id, "target": target, "status": "rejected"}
    except Exception as exc:
        return {"error": f"promotion 거부 실패: {exc}"}


def _directory_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return total
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def billing_get_usage(pack_handle: str) -> dict:
    """로컬 zip, 캐시, 노드, 벡터, audit event 사용량을 반환한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    try:
        with pk._runtime_db() as con:
            audit_events = con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        zip_bytes = os.path.getsize(pk.zip_path)
        return {
            "pack_handle": pack_handle,
            "zip_size_bytes": zip_bytes,
            "cache_size_bytes": _directory_size(pk.cache),
            "nodes": len(pk.nodes),
            "vectors": len(pk.vec_meta),
            "audit_events": audit_events,
        }
    except Exception as exc:
        return {"error": f"사용량 조회 실패: {exc}"}


def billing_list_events(pack_handle: str, limit: int = 50) -> dict:
    """로컬 audit event를 최신순으로 나열한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if limit < 1:
        return {"error": "limit은 1 이상이어야 합니다."}
    try:
        with pk._runtime_db() as con:
            rows = con.execute(
                "SELECT id, ts, actor, action, detail FROM audit_events "
                "ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        columns = ("id", "ts", "actor", "action", "detail")
        return {"events": [_row_dict(row, columns) for row in rows]}
    except Exception as exc:
        return {"error": f"audit event 조회 실패: {exc}"}


def _parse_rebac_policy(policy: str) -> tuple[dict[str, set[str]], str]:
    """rebac 블록의 간단한 roles/default YAML만 파싱한다."""
    lines = policy.splitlines()
    start = None
    base_indent = 0
    for index, line in enumerate(lines):
        if re.match(r"^\s*rebac\s*:\s*$", line):
            start = index
            base_indent = len(line) - len(line.lstrip())
            break
    if start is None:
        return {}, "read_only"
    block = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= base_indent:
            break
        block.append(line)
    body = "\n".join(block)
    default_match = re.search(r"(?m)^\s*default\s*:\s*([^\s#]+)", body)
    default = default_match.group(1).strip("'\"") if default_match else "read_only"
    roles: dict[str, set[str]] = {}
    roles_match = re.search(r"roles\s*:\s*\{([^}]*)\}", body, re.DOTALL)
    role_text = roles_match.group(1) if roles_match else body
    for match in re.finditer(r"['\"]?([\w.-]+)['\"]?\s*:\s*\[([^]]*)\]", role_text):
        role = match.group(1)
        actions = {token.strip().strip("'\"") for token in match.group(2).split(",")
                   if token.strip()}
        roles[role] = actions
    return roles, default


def ontology_rebac_check(pack_handle: str, actor: str, action: str) -> dict:
    """runtime/policy.yaml의 rebac 규칙으로 actor의 action 허용 여부를 확인한다."""
    pk, error = _get_pack(pack_handle)
    if error:
        return error
    if not actor or not action:
        return {"error": "actor와 action은 필수입니다."}
    try:
        roles, default = _parse_rebac_policy(pk._read("runtime/policy.yaml"))
        if actor in roles:
            allowed = action in roles[actor]
            role = actor
        else:
            allowed = action == "read"
            role = default
        return {"actor": actor, "action": action, "allowed": allowed,
                "role": role, "policy_default": default}
    except Exception as exc:
        return {"error": f"ReBAC 정책 조회 실패: {exc}"}
