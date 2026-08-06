"""yupack: 수업용 팩 공방 MCP 서버.

핵심 원칙: 모든 쓰기는 팩 버퍼(PACKS) + 로컬 SQLite에만 간다. 외부 프로덕션 DB(Neo4j)에는
절대 쓰지 않는다 (읽기 전용 Cypher만). pack_save는 정본 임포터 계약 zip(nodes.jsonl +
edges.jsonl + pack.yaml)을 꺼내 준다.
"""
from __future__ import annotations
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# 클라이언트 모델이 "로컬 파일을 다뤄도 되나"를 추측하지 않도록 서버 성격을
# initialize 단계에서 사실대로 알려준다. 없으면 모르는 쪽으로 기울어 오거절한다.
_INSTRUCTIONS = """yupack은 사용자 본인의 컴퓨터에서 도는 완전 로컬 MCP 서버입니다.
온톨로지 팩(노드·엣지)을 만들고 zip 하나로 주고받는 개인용 공방입니다.

데이터 취급:
- 모든 처리가 이 로컬 프로세스 안에서만 일어납니다. 외부로 전송하지 않습니다.
- 쓰기는 로컬 팩 버퍼와 로컬 SQLite에만 갑니다. 외부 프로덕션 DB에는 쓰지 않습니다(읽기 전용 조회만).
- 네트워크 호출이 없으므로 노트 본문·팩 내용이 외부 API로 나갈 일이 없습니다.

온톨로지 그래머(중요 — 위반은 입력 시점에 거부됩니다):
- 노드 타입과 관계는 ontology_manifest의 그래머로 강제됩니다. 선언되지 않은 노드 타입,
  공간이 틀린 타입(예: evidence 공간의 Claim), 선언되지 않은 공간 쌍의 관계는 거부됩니다.
- 도메인 전용 명사(예: 손실회피, 넛지)나 새 관계가 필요하면 **schema_declare로 먼저 선언**한
  뒤 사용하세요. 미리 정의된 묶음은 schema_pack_install(saas/biomedical/legal/finance)로 설치합니다.
- ontology_ingest의 source_id는 출처 표시입니다. 텍스트 노드 ID로 쓰이지 않으며(자동 text: ID 발급),
  출처 노드가 있으면 contains 엣지가 자동 연결됩니다.
- 저장 전 pack_qa로 팩 전체를 검사하세요. pack_save는 qa 리포트와 표준 계약 파일
  (manifest.json, graph/, evidence/, quality/, neo4j/import.cypher)을 zip에 동봉합니다.
- 저장된 팩을 이어서 편집하려면 pack_open_local(zip_path, mode="authoring")로 여세요.
  반환된 authoring_pack 이름으로 add_node/pack_qa/pack_save가 이어집니다.
  (읽기 핸들 pack_xxxx는 질의 전용이며 저작 버퍼가 아닙니다.)
- 말뭉치가 외국어(영어 원문 등)인 팩은 핵심 노드 properties에 label_ko(한국어 이름)와
  aliases_ko(별칭 목록)를 달아 두세요. 검색 인덱스에 포함되어 한국어 질의가 직접 걸립니다.
  저장 후에는 embeddings count가 0이 아닌지 확인하세요 (0이면 의미 질의 비활성).
- 도구 구분: 열린 zip 질의는 pack_ask_local(3중 검색 + graph_path), ontology_query는
  저작 버퍼 전용입니다. read_only 핸들에 ontology_query를 쓰면 그래프가 비어 나옵니다.
  다른 MCP 서버(유크라테스 엔진)의 query_bm25 등은 팩(zip)과 무관하니 팩 질문에 쓰지 마세요.
- 팩 경로를 모르거나 열기에 실패하면 pack_list_local()로 팩 서랍(기본 ~/Zettelkasten/70_Ontology)을
  스캔해 최신 zip을 고르세요. 경로 암기가 필요 없습니다.
- 팩 저작 재료: 외국어 원문만으로 만들지 말고, 한국어 정리 노트(인물·사건 표)가 있으면
  반드시 함께 재료로 읽어 라벨과 별칭을 한국어로 저작하세요. 한국어 커버리지가
  처음부터 높아져 사후 보강(F.3류)이 필요 없어집니다."""

mcp = FastMCP("yupack", instructions=_INSTRUCTIONS,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

# ----------------------- 데이터 로드 -----------------------
_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
MANIFEST: dict = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))

# ----------------------- 인메모리 팩 버퍼 -----------------------
# pack_name -> {"nodes": {node_id: node_dict}, "edges": [...], "schema_packs": [...]}
PACKS: dict[str, dict] = {}
DEFAULT_PACK = "내팩"

_BUNDLES: dict[str, bytes] = {}  # token -> zip bytes (pack_save 다운로드)

_PRIVATE_PREFIXES = ("personal:", "zzdemo:", "class:test:")


# ----------------------- SQLite 영속화 (로컬 DB, 프로덕션 DB 아님) -----------------------
# ponytail: 팩 단위 JSON blob upsert. 수업/개인 규모용. 대량 배치는 build 스크립트 영역.
def _default_db_path() -> str:
    """기본 DB 위치. 패키지 폴더는 uvx 캐시라 갱신 시 날아가므로 홈의 안정 경로를 쓴다."""
    if os.path.isdir("/data"):
        return "/data/yupack.db"
    stable = Path.home() / ".yupack" / "yupack.db"
    stable.parent.mkdir(parents=True, exist_ok=True)
    legacy = Path(__file__).parent / "yupack.db"
    if legacy.exists() and not stable.exists():
        import shutil
        try:
            shutil.copy2(legacy, stable)  # 구버전 DB 1회 이관
        except Exception:
            pass
    return str(stable)


_DB_PATH = os.environ.get("YUPACK_DB") or _default_db_path()


def _db():
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS packs (name TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return conn


def _load_packs() -> None:
    try:
        with _db() as conn:
            for name, data in conn.execute("SELECT name, data FROM packs"):
                PACKS[name] = json.loads(data)
    except Exception:
        pass  # DB 문제는 buffer-only로 폴백


def _persist(pack: str) -> str:
    try:
        with _db() as conn:
            conn.execute("INSERT INTO packs(name, data) VALUES(?, ?) "
                         "ON CONFLICT(name) DO UPDATE SET data=excluded.data",
                         (pack, json.dumps(PACKS[pack], ensure_ascii=False)))
        return "ok"
    except Exception as e:
        return f"failed({e})"


def _get_pack(pack: str) -> dict:
    return PACKS.setdefault(pack, {"nodes": {}, "edges": [], "schema_packs": []})


_load_packs()


def _space_of(node_type: str) -> str | None:
    for space, info in MANIFEST["spaces"].items():
        if node_type in info["node_types"]:
            return space
    return None


def _allowed_relations(from_space: str | None, to_space: str | None) -> list[str] | None:
    if not from_space or not to_space:
        return None
    for me in MANIFEST["meta_edges"]:
        if me["from_space"] == from_space and me["to_space"] == to_space:
            return me["relations"]
    return None


def _allowed_types_for(buf: dict, space: str) -> list[str]:
    """공간별 허용 노드 타입 = 기본 그래머 + 설치 스키마팩(공간 자유) + 팩 커스텀 선언."""
    types = list(MANIFEST["spaces"].get(space, {}).get("node_types", []))
    types += buf.get("custom_types", {}).get(space, [])
    for sp in buf.get("schema_packs", []):
        types += MANIFEST["schema_packs"].get(sp, [])
    return types


def _edge_relations(buf: dict, from_space: str, to_space: str) -> list[str] | None:
    """공간 쌍의 허용 관계(기본 meta_edges + 팩 커스텀 선언). 쌍 자체가 미선언이면 None."""
    rels, declared = [], False
    for me in MANIFEST["meta_edges"] + buf.get("custom_relations", []):
        if me["from_space"] == from_space and me["to_space"] == to_space:
            rels += me["relations"]
            declared = True
    return rels if declared else None


def _qa_scan(pack: str) -> dict:
    """팩 전체를 그래머로 검사한다. opencrab-pack-v1 quality/report.json 형태로 반환."""
    buf = _get_pack(pack)
    issues = []
    for nid, n in buf["nodes"].items():
        if n["space"] not in MANIFEST["spaces"]:
            issues.append({"kind": "unknown_space", "id": nid, "detail": n["space"]})
            continue
        if n["node_type"] not in _allowed_types_for(buf, n["space"]):
            issues.append({"kind": "undeclared_node_type", "id": nid,
                           "detail": f"{n['space']}/{n['node_type']}"})
        canonical = _space_of(n["node_type"])
        if canonical and canonical != n["space"]:
            issues.append({"kind": "wrong_space", "id": nid,
                           "detail": f"{n['node_type']}의 정본 공간은 '{canonical}' (현재 '{n['space']}')"})
    linked = set()
    broken_edges = 0
    for i, e in enumerate(buf["edges"]):
        fn, tn = buf["nodes"].get(e["from_id"]), buf["nodes"].get(e["to_id"])
        eid = f"edge#{i}({e['from_id']}-{e['relation']}->{e['to_id']})"
        if not fn or not tn:
            issues.append({"kind": "broken_edge", "id": eid,
                           "detail": "끝점 노드가 팩에 없음"})
            broken_edges += 1
            continue
        linked.update((e["from_id"], e["to_id"]))
        if fn["space"] != e["from_space"] or tn["space"] != e["to_space"]:
            issues.append({"kind": "space_mismatch", "id": eid,
                           "detail": f"실제 공간 {fn['space']}->{tn['space']}, 기록 {e['from_space']}->{e['to_space']}"})
        allowed = _edge_relations(buf, fn["space"], tn["space"])
        if allowed is None or e["relation"] not in allowed:
            issues.append({"kind": "undeclared_relation", "id": eid,
                           "detail": f"{fn['space']}->{tn['space']}에 '{e['relation']}' 미선언"})
    orphans = [nid for nid in buf["nodes"] if nid not in linked] if buf["edges"] else list(buf["nodes"])
    # 조언(상태에 영향 없음): 한국어 커버리지 게이지 - 한국어 질의가 목적인 팩에서는 사실상 필수
    # 커버 판정 = 라벨에 한글이 있거나 label_ko/aliases_ko 보유
    covered, uncovered = 0, []
    checked = 0
    for nid, n in buf["nodes"].items():
        props = n.get("properties") or {}
        label = str(props.get("label") or "")
        if not label:
            continue
        checked += 1
        if re.search(r"[가-힣]", label) or props.get("label_ko") or props.get("aliases_ko"):
            covered += 1
        else:
            uncovered.append(nid)
    advisories = []
    korean_coverage = round(covered / checked, 3) if checked else None
    if checked >= 5 and korean_coverage is not None and korean_coverage < 0.8:
        advisories.append(
            f"한국어 커버리지 {covered}/{checked} ({korean_coverage:.0%}). 한국어 질의 대상 팩이라면 "
            "전 사건·근거 노드에 label_ko/aliases_ko(질문 동사형 포함)를 채우세요. "
            f"미커버 예시: {uncovered[:10]}")
    kinds = {i["kind"] for i in issues}
    return {
        "status": "pass" if not issues else "fail",
        "checks": {
            "grammar": "fail" if kinds & {"undeclared_node_type", "undeclared_relation", "unknown_space"} else "pass",
            "schema": "fail" if "wrong_space" in kinds else "pass",
            "broken_edges": "fail" if broken_edges else "pass",
            "orphan_nodes": "warn" if orphans else "pass",
        },
        "counts": {"nodes": len(buf["nodes"]), "edges": len(buf["edges"]),
                   "issues": len(issues), "broken_edges": broken_edges,
                   "orphan_nodes": len(orphans)},
        "orphan_node_ids": orphans[:50],
        "advisories": advisories,
        "issues": issues,
    }


def _cy(v) -> str:
    """Cypher 문자열 리터럴 (json 이스케이프는 Cypher와 호환)."""
    return json.dumps(str(v), ensure_ascii=False)


def _contract_files(pack: str, buf: dict, today: str, qa: dict) -> dict[str, str]:
    """opencrab-pack-v1 계약 아티팩트를 생성한다: manifest.json + graph/ + evidence/ +
    quality/ + neo4j/import.cypher. (opencrab_ingest.jsonl은 Neo4j 통과 후 추출본이라
    Neo4j 미접촉인 yupack에서는 만들지 않는다 — yucrates export 몫.)"""
    ev_refs: dict[str, list] = {}   # node_id -> 연결된 evidence 노드 id들
    ev_links: dict[str, list] = {}  # evidence_id -> 연결된 비evidence 노드 id들
    for e in buf["edges"]:
        for a, b in ((e["from_id"], e["to_id"]), (e["to_id"], e["from_id"])):
            na, nb = buf["nodes"].get(a), buf["nodes"].get(b)
            if na and nb and nb["space"] == "evidence" and na["space"] != "evidence":
                ev_refs.setdefault(a, []).append(b)
                ev_links.setdefault(b, []).append(a)
    nodes_l, edges_l, evidence_l, cypher = [], [], [], []
    for nid, n in buf["nodes"].items():
        props = n.get("properties", {})
        nodes_l.append(json.dumps({
            "id": nid, "label": props.get("label", nid), "space": n["space"],
            "node_type": n["node_type"], "properties": props,
            "evidence_refs": ev_refs.get(nid, []),
            "quality": {"confidence": props.get("confidence"), "promotion_status": "draft"},
        }, ensure_ascii=False))
        safe_type = re.sub(r"[^0-9A-Za-z_가-힣]", "_", n["node_type"])
        cypher.append(f"MERGE (n:`{safe_type}` {{id: {_cy(nid)}}}) "
                      f"SET n.label = {_cy(props.get('label', nid))}, n.space = {_cy(n['space'])}, "
                      f"n.node_type = {_cy(n['node_type'])}, n.props_json = {_cy(json.dumps(props, ensure_ascii=False))};")
        if n["space"] == "evidence":
            evidence_l.append(json.dumps({
                "evidence_id": nid, "kind": "text_chunk",
                "source": {"url": None, "path": None, "title": props.get("source_id") or props.get("label")},
                "hash": "sha256:" + hashlib.sha256(
                    (props.get("text") or props.get("definition") or "").encode()).hexdigest(),
                "collected_at": today,
                "location": {"document_id": props.get("source_id"), "chunk_index": None},
                "links": {"document_id": props.get("source_id"),
                          "node_ids": sorted(set(ev_links.get(nid, [])))},
            }, ensure_ascii=False))
    for i, e in enumerate(buf["edges"]):
        edges_l.append(json.dumps({
            "id": f"edge:{i}", "from_id": e["from_id"], "to_id": e["to_id"],
            "from_space": e["from_space"], "to_space": e["to_space"], "relation": e["relation"],
            "confidence": e.get("properties", {}).get("confidence"),
            "evidence_refs": sorted(set(ev_refs.get(e["from_id"], []) + ev_refs.get(e["to_id"], []))),
            "properties": e.get("properties", {}),
        }, ensure_ascii=False))
        safe_rel = re.sub(r"[^0-9A-Za-z_]", "_", e["relation"])
        cypher.append(f"MATCH (a {{id: {_cy(e['from_id'])}}}), (b {{id: {_cy(e['to_id'])}}}) "
                      f"MERGE (a)-[r:`{safe_rel}`]->(b) "
                      f"SET r.props_json = {_cy(json.dumps(e.get('properties', {}), ensure_ascii=False))};")
    nodes_txt, edges_txt, ev_txt = "\n".join(nodes_l), "\n".join(edges_l), "\n".join(evidence_l)
    non_ev = [nid for nid, n in buf["nodes"].items() if n["space"] != "evidence"]
    manifest = {
        "format_version": "opencrab-pack-v1",
        "pack_id": f"{_safe_filename(pack)}-{today}", "title": pack, "version": "1.0.0",
        "grammar_version": MANIFEST["version"], "created_at": today, "created_by": "yupack",
        "license": {"scope": "personal", "name": "proprietary"},
        "source": {"mode": "manual", "label": pack, "url": None,
                   "description": "yupack 로컬 공방에서 수동/대화형으로 생산된 팩"},
        "schema_packs": buf.get("schema_packs", []),
        "custom_grammar": {"node_types": buf.get("custom_types", {}),
                           "relations": buf.get("custom_relations", [])},
        "counts": {"nodes": len(buf["nodes"]), "edges": len(buf["edges"]),
                   "evidence": len(evidence_l), "documents": 0, "files": 0},
        "quality": {
            "evidence_coverage": (sum(1 for nid in non_ev if ev_refs.get(nid)) / len(non_ev)) if non_ev else None,
            "graph_reference_integrity":
                1.0 - (qa["counts"]["broken_edges"] / len(buf["edges"]) if buf["edges"] else 0.0),
            "promotion_status": "validated" if qa["status"] == "pass" else "draft",
        },
        "hashes": {"nodes_sha256": hashlib.sha256(nodes_txt.encode()).hexdigest(),
                   "edges_sha256": hashlib.sha256(edges_txt.encode()).hexdigest(),
                   "evidence_sha256": hashlib.sha256(ev_txt.encode()).hexdigest()},
        "artifacts": {"nodes": "graph/nodes.jsonl", "edges": "graph/edges.jsonl",
                      "evidence_index": "evidence/index.jsonl",
                      "quality_report": "quality/report.json",
                      "neo4j_cypher": "neo4j/import.cypher"},
    }
    return {
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=1),
        "graph/nodes.jsonl": nodes_txt,
        "graph/edges.jsonl": edges_txt,
        "evidence/index.jsonl": ev_txt,
        "quality/report.json": json.dumps(qa, ensure_ascii=False, indent=1),
        "neo4j/import.cypher": "// yupack pack: " + pack + " (" + today + ")\n"
                               "// 로컬 재현용. graph/*.jsonl이 정본이다.\n" + "\n".join(cypher) + "\n",
    }


def _store_zip(files: dict) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    tok = secrets.token_urlsafe(8)
    _BUNDLES[tok] = buf.getvalue()
    if len(_BUNDLES) > 200:
        for k in list(_BUNDLES)[:100]:
            _BUNDLES.pop(k, None)
    return tok


def _safe_filename(label: str) -> str:
    s = re.sub(r"[^\w가-힣.-]+", "_", label).strip("_")
    return s or "node"


# ----------------------- Neo4j 읽기 전용 -----------------------
def _neo4j_driver():
    """env 없으면 None (buffer-only 폴백)."""
    uri = os.environ.get("NEO4J_URI")
    user = os.environ.get("NEO4J_USERNAME") or os.environ.get("NEO4J_USER")
    pw = os.environ.get("NEO4J_PASSWORD")
    if not (uri and user and pw):
        return None
    from neo4j import GraphDatabase  # lazy import
    return GraphDatabase.driver(uri, auth=(user, pw))


def _cloud_search(question: str, limit: int) -> list[dict]:
    """읽기 전용 Neo4j fulltext/fallback 검색. 개인정보·데모·테스트 노드 제외."""
    driver = _neo4j_driver()
    if driver is None:
        return []
    database = os.environ.get("NEO4J_DATABASE", "neo4j")
    privacy_filter = " AND ".join(
        f"NOT node.id STARTS WITH '{p}'" for p in _PRIVATE_PREFIXES
    )
    privacy_filter_n = privacy_filter.replace("node.", "n.")
    out: list[dict] = []
    try:
        with driver.session(database=database) as session:
            try:
                res = session.run(
                    "CALL db.index.fulltext.queryNodes('concept_fulltext', $q) "
                    f"YIELD node, score WHERE {privacy_filter} "
                    "RETURN node.id AS id, node.label AS label, node.definition AS definition, "
                    "score LIMIT $limit",
                    q=question, limit=limit,
                )
                out = [dict(r) for r in res]
            except Exception:
                tokens = [t for t in re.split(r"\s+", question.strip()) if t]
                if not tokens:
                    return []
                token = tokens[0]
                res = session.run(
                    "MATCH (n:Concept) WHERE n.label CONTAINS $token "
                    f"AND {privacy_filter_n} "
                    "RETURN n.id AS id, n.label AS label, n.definition AS definition "
                    "LIMIT $limit",
                    token=token, limit=limit,
                )
                out = [dict(r) for r in res]
    finally:
        driver.close()
    for r in out:
        r["source"] = "cloud"
    return out


# ----------------------- 버퍼 검색 -----------------------
def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[\w가-힣]+", (text or "").lower()))


def _buffer_search(pack: str, question: str, limit: int) -> list[dict]:
    q_tokens = _tokenize(question)
    scored = []
    for node_id, node in _get_pack(pack)["nodes"].items():
        label = node.get("properties", {}).get("label", node_id)
        definition = node.get("properties", {}).get("definition", "")
        n_tokens = _tokenize(label) | _tokenize(definition)
        overlap = len(q_tokens & n_tokens)
        # 한국어 조사 대응: 라벨이 질문 문자열에 부분 포함되면 강한 매칭 (예: "이차방정식을")
        if label and label in question:
            overlap += 10
        else:
            for t in n_tokens:
                if len(t) >= 2 and any(qt.startswith(t) or t.startswith(qt) for qt in q_tokens):
                    overlap += 1
        if overlap:
            scored.append({"id": node_id, "label": label, "definition": definition,
                            "score": overlap, "exact": len(q_tokens & n_tokens),
                            "source": "buffer"})
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]


# ----------------------- OpenAI 임베딩 -----------------------
def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    from openai import OpenAI  # lazy import
    model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-large")
    client = OpenAI(api_key=key)
    resp = client.embeddings.create(model=model, input=texts)
    return [d.embedding for d in resp.data]


# ======================= TOOLS =======================
@mcp.tool()
def ontology_manifest() -> dict:
    """그래프 온톨로지 그래머(공간·노드타입·관계·스키마팩 목록)를 반환한다."""
    return MANIFEST


@mcp.tool()
def schema_pack_list(pack: str = DEFAULT_PACK) -> dict:
    """설치 가능한 스키마팩 목록과 현재 팩(pack)에 설치된 상태를 함께 보여준다."""
    installed = _get_pack(pack)["schema_packs"]
    return {
        "available": list(MANIFEST["schema_packs"].keys()),
        "installed": installed,
        "types_by_pack": MANIFEST["schema_packs"],
    }


@mcp.tool()
def schema_pack_install(name: str, pack: str = DEFAULT_PACK) -> dict:
    """스키마팩(saas/biomedical/legal 등)을 현재 팩 버퍼에 설치해 노드 타입을 늘린다."""
    if name not in MANIFEST["schema_packs"]:
        return {"error": f"알 수 없는 스키마팩: {name}. 사용 가능: {list(MANIFEST['schema_packs'])}"}
    buf = _get_pack(pack)
    if name not in buf["schema_packs"]:
        buf["schema_packs"].append(name)
    return {"installed": name, "node_types": MANIFEST["schema_packs"][name]}


@mcp.tool()
def schema_pack_uninstall(name: str, pack: str = DEFAULT_PACK) -> dict:
    """현재 팩 버퍼에서 스키마팩을 제거한다 (노드는 남고, 타입 확장만 해제)."""
    buf = _get_pack(pack)
    if name not in buf["schema_packs"]:
        return {"error": f"설치되어 있지 않음: {name}. 현재: {buf['schema_packs']}"}
    buf["schema_packs"].remove(name)
    return {"uninstalled": name, "remaining": buf["schema_packs"],
            "stores": {"buffer": "ok", "sqlite": _persist(pack)}}


@mcp.tool()
def schema_declare(pack: str = DEFAULT_PACK, node_types: dict | None = None,
                   relations: list | None = None) -> dict:
    """팩 전용 커스텀 그래머를 선언한다 (이 팩에서만 유효, SQLite에 영속).

    node_types: {"공간": ["타입", ...]} — 예: {"concept": ["BehavioralBias", "Nudge"], "resource": ["Source"]}
    relations: [{"from_space": "claim", "to_space": "concept", "relations": ["about"]}]
    선언 후 ontology_add_node / ontology_add_edge가 이 타입·관계를 허용한다.
    도메인 명사가 필요할 때 임의 타입을 그냥 넣지 말고 반드시 여기로 먼저 선언할 것.
    """
    buf = _get_pack(pack)
    added_types: dict[str, list] = {}
    added_relations = []
    for space, tlist in (node_types or {}).items():
        if space not in MANIFEST["spaces"]:
            return {"error": f"알 수 없는 space: {space}. 사용 가능: {list(MANIFEST['spaces'])}"}
        cur = buf.setdefault("custom_types", {}).setdefault(space, [])
        for t in tlist:
            canonical = _space_of(t)
            if canonical and canonical != space:
                return {"error": f"'{t}'는 기본 그래머에서 '{canonical}' 공간 소속입니다. 재선언 불가."}
            if t not in cur:
                cur.append(t)
                added_types.setdefault(space, []).append(t)
    for r in relations or []:
        fs, ts = r.get("from_space"), r.get("to_space")
        rels = list(r.get("relations", []))
        if fs not in MANIFEST["spaces"] or ts not in MANIFEST["spaces"]:
            return {"error": f"알 수 없는 space 쌍: {fs}→{ts}. 사용 가능: {list(MANIFEST['spaces'])}"}
        if not rels:
            return {"error": f"{fs}→{ts}: relations 목록이 비어 있습니다."}
        cur_rels = buf.setdefault("custom_relations", [])
        entry = next((r0 for r0 in cur_rels
                      if r0["from_space"] == fs and r0["to_space"] == ts), None)
        if entry is None:
            entry = {"from_space": fs, "to_space": ts, "relations": []}
            cur_rels.append(entry)
        new_only = [x for x in rels if x not in entry["relations"]]
        entry["relations"] += new_only
        if new_only:
            added_relations.append({"from_space": fs, "to_space": ts, "relations": new_only})
    return {"declared": {"node_types": added_types, "relations": added_relations},
            "now_custom": {"node_types": buf.get("custom_types", {}),
                           "relations": buf.get("custom_relations", [])},
            "stores": {"buffer": "ok", "sqlite": _persist(pack)}}


@mcp.tool()
def pack_qa(pack: str = DEFAULT_PACK) -> dict:
    """팩 전체를 그래머로 검사해 pass/fail과 위반 목록을 반환한다 (저장 전 필수 점검).

    검사 항목: 비선언 노드 타입, 정본 공간 오배치, 끊어진 엣지(끝점 부재),
    엣지 공간 불일치, 비선언 관계, 고아 노드. pack_save가 이 리포트를 zip에 동봉한다.
    """
    return _qa_scan(pack)


@mcp.tool()
def ontology_extract(text: str, pack: str = DEFAULT_PACK, max_nodes: int = 8) -> dict:
    """텍스트에서 개념 노드와 관계를 LLM으로 추출해 팩 버퍼에 넣는다 (DB 쓰기 없음).

    추출 관계는 그래머의 concept→concept 허용 목록(커스텀 선언 포함)으로 제한되고,
    삽입 시에도 같은 그래머로 검증한다. 허용 밖 관계·타입 충돌 노드는 건너뛰고 보고한다.
    """
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return {"error": "OPENAI_API_KEY 없음: extract는 LLM이 필요합니다."}
    import urllib.request  # openai 패키지 없이 표준 라이브러리로 호출
    buf = _get_pack(pack)
    allowed_rel = _edge_relations(buf, "concept", "concept") or ["related_to"]
    model = os.environ.get("OPENAI_EXTRACT_MODEL", "gpt-4o-mini")
    prompt = (f"다음 텍스트에서 핵심 개념 최대 {max_nodes}개와 개념 사이 관계를 추출해 JSON으로만 답하라. "
              '형식: {"nodes":[{"id":"c:슬러그","label":"...","definition":"..."}],'
              f'"edges":[{{"from":"c:슬러그","relation":"{"|".join(allowed_rel)}","to":"c:슬러그"}}]}}'
              f"\n관계는 반드시 위 목록에서만 고른다.\n\n텍스트:\n{text[:6000]}")
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "response_format": {"type": "json_object"}}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", body,
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=120))
    except Exception as e:
        return {"error": f"LLM 호출 실패: {e}"}
    data = json.loads(resp["choices"][0]["message"]["content"])
    added_n, added_e = 0, 0
    skipped = []
    for n in data.get("nodes", [])[:max_nodes]:
        nid = n.get("id") or f"c:{secrets.token_hex(3)}"
        existing = buf["nodes"].get(nid)
        if existing and (existing["space"] != "concept" or existing["node_type"] != "Concept"):
            skipped.append({"node": nid, "reason": f"기존 {existing['space']}/{existing['node_type']} 노드와 id 충돌"})
            continue
        buf["nodes"][nid] = {"space": "concept", "node_type": "Concept",
                             "properties": {"label": n.get("label", nid),
                                             "definition": n.get("definition", "")}}
        added_n += 1
    for e in data.get("edges", []):
        rel = e.get("relation", "related_to")
        if e.get("from") not in buf["nodes"] or e.get("to") not in buf["nodes"]:
            continue
        if rel not in allowed_rel:
            skipped.append({"edge": f"{e['from']} -{rel}-> {e['to']}",
                            "reason": f"허용 밖 관계 (허용: {allowed_rel})"})
            continue
        buf["edges"].append({"from_space": "concept", "from_id": e["from"],
                              "relation": rel,
                              "to_space": "concept", "to_id": e["to"], "properties": {}})
        added_e += 1
    out = {"added": {"nodes": added_n, "edges": added_e},
           "stores": {"buffer": "ok", "sqlite": _persist(pack)}}
    if skipped:
        out["skipped"] = skipped
    return out


@mcp.tool()
def ontology_impact(node_id: str, pack: str = DEFAULT_PACK, depth: int = 2) -> dict:
    """노드를 바꾸면 영향이 미치는 반경을 BFS로 계산한다 (홉별 이웃 노드)."""
    buf = _get_pack(pack)
    if node_id not in buf["nodes"]:
        return {"error": f"노드 없음: {node_id}"}
    frontier, visited = {node_id}, {node_id}
    rings = []
    for _ in range(max(1, min(depth, 4))):
        nxt = set()
        for e in buf["edges"]:
            if e["from_id"] in frontier and e["to_id"] not in visited:
                nxt.add(e["to_id"])
            elif e["to_id"] in frontier and e["from_id"] not in visited:
                nxt.add(e["from_id"])
        if not nxt:
            break
        rings.append(sorted(nxt)[:50])
        visited |= nxt
        frontier = nxt
    def _lab(nid):
        n = buf["nodes"].get(nid)
        return n.get("properties", {}).get("label", nid) if n else nid
    return {"node": node_id, "impact_total": len(visited) - 1,
            "rings": [[{"id": i, "label": _lab(i)} for i in ring] for ring in rings]}


@mcp.tool()
def ontology_lever_simulate(lever_id: str, pack: str = DEFAULT_PACK) -> dict:
    """레버 노드에서 raises/lowers/affects 엣지를 따라 기대 효과를 나열한다."""
    buf = _get_pack(pack)
    if lever_id not in buf["nodes"]:
        return {"error": f"레버 노드 없음: {lever_id}"}
    effects = []
    for e in buf["edges"]:
        if e["from_id"] != lever_id or e["relation"] not in ("raises", "lowers", "affects"):
            continue
        t = buf["nodes"].get(e["to_id"], {})
        effects.append({"relation": e["relation"], "target": e["to_id"],
                        "target_label": t.get("properties", {}).get("label", e["to_id"]),
                        "target_space": t.get("space"),
                        "edge_properties": e.get("properties", {})})
    props = buf["nodes"][lever_id].get("properties", {})
    return {"lever": lever_id, "label": props.get("label"),
            "applies_when": props.get("applies_when"), "tradeoffs": props.get("tradeoffs"),
            "effects": effects,
            "note": "연구 근거 기반 기대 효과입니다. 직접 적용 전 로컬 검증(local_validation_required)이 필요합니다."}


@mcp.tool()
def ontology_add_node(space: str, node_type: str, node_id: str,
                       properties: dict | None = None, pack: str = DEFAULT_PACK) -> dict:
    """노드 하나를 팩 버퍼에 추가한다 (외부 DB 쓰기 없음). 그래머 위반은 즉시 거부한다.

    비선언 타입이 필요하면 schema_declare로 먼저 선언하거나 schema_pack_install을 사용.
    같은 id에 같은 space/type이면 갱신, 다른 space/type이면 덮어쓰지 않고 거부한다.
    """
    if space not in MANIFEST["spaces"]:
        return {"error": f"알 수 없는 space: {space}. 사용 가능: {list(MANIFEST['spaces'])}"}
    buf = _get_pack(pack)
    canonical = _space_of(node_type)
    if canonical and canonical != space:
        return {"error": f"'{node_type}'의 정본 공간은 '{canonical}'입니다. space='{canonical}'로 추가하세요.",
                "hint": f"예: Claim은 claim 공간, TextUnit은 evidence 공간 소속입니다."}
    if node_type not in _allowed_types_for(buf, space):
        return {"error": f"'{space}' 공간에 선언되지 않은 노드 타입: {node_type}",
                "declared_types": MANIFEST["spaces"][space]["node_types"],
                "custom_types": buf.get("custom_types", {}).get(space, []),
                "hint": "커스텀 타입은 schema_declare(node_types={\"" + space + "\": [\"" + node_type + "\"]})로 "
                        "먼저 선언한 뒤 추가하세요. 도메인 묶음은 schema_pack_install 참고."}
    existing = buf["nodes"].get(node_id)
    if existing and (existing["space"] != space or existing["node_type"] != node_type):
        return {"error": f"id 충돌: '{node_id}'는 이미 {existing['space']}/{existing['node_type']} 노드입니다. "
                          "기존 노드를 덮어쓰지 않습니다. 다른 id를 사용하세요."}
    node_data = {"space": space, "node_type": node_type, "properties": properties or {}}
    buf["nodes"][node_id] = node_data
    return {"stores": {"buffer": "ok", "sqlite": _persist(pack)}, "node_data": {node_id: node_data}}


@mcp.tool()
def ontology_add_edge(from_space: str, from_id: str, relation: str, to_space: str,
                       to_id: str, properties: dict | None = None,
                       pack: str = DEFAULT_PACK) -> dict:
    """엣지 하나를 팩 버퍼에 추가한다. 그래머(끝점 실존·실제 공간·허용 관계)를 강제한다.

    끝점 노드가 팩에 먼저 있어야 하고, 선언된 공간 쌍·관계만 허용된다.
    새 관계가 필요하면 schema_declare(relations=[...])로 먼저 선언한다.
    """
    buf = _get_pack(pack)
    for side, sid, sspace in (("from", from_id, from_space), ("to", to_id, to_space)):
        n = buf["nodes"].get(sid)
        if not n:
            return {"error": f"{side} 노드가 팩에 없습니다: '{sid}'. ontology_add_node로 먼저 추가하세요."}
        if n["space"] != sspace:
            return {"error": f"{side} 노드 '{sid}'의 실제 공간은 '{n['space']}'입니다 ('{sspace}' 아님). "
                              f"{side}_space='{n['space']}'로 다시 시도하세요."}
    allowed = _edge_relations(buf, from_space, to_space)
    if allowed is None:
        pairs = sorted({(me["from_space"], me["to_space"])
                        for me in MANIFEST["meta_edges"] + buf.get("custom_relations", [])
                        if me["from_space"] == from_space})
        return {"error": f"'{from_space}'→'{to_space}' 공간 쌍에 선언된 관계가 없습니다.",
                "declared_pairs_from_here": [f"{a}→{b}" for a, b in pairs],
                "hint": "schema_declare(relations=[{\"from_space\": \"" + from_space + "\", \"to_space\": \""
                        + to_space + "\", \"relations\": [\"" + relation + "\"]}])로 먼저 선언하세요."}
    if relation not in allowed:
        return {"error": f"'{from_space}'→'{to_space}' 간 허용되지 않은 관계: {relation}. "
                          f"허용된 관계: {allowed}"}
    edge = {"from_space": from_space, "from_id": from_id, "relation": relation,
            "to_space": to_space, "to_id": to_id, "properties": properties or {}}
    buf["edges"].append(edge)
    return {"stores": {"buffer": "ok", "sqlite": _persist(pack)}, "edge": edge}


@mcp.tool()
def ontology_ingest(text: str, source_id: str | None = None, pack: str = DEFAULT_PACK) -> dict:
    """텍스트를 evidence 공간의 TextUnit 노드로 버퍼에 저장한다 (DB 쓰기 없음).

    source_id는 출처 표시(provenance)다: TextUnit의 ID로 쓰이지 않고 속성에 기록되며,
    해당 출처 노드가 resource 공간에 있으면 contains 엣지가 자동 연결된다.
    (과거에는 source_id를 노드 ID로 써서 원본 Source 노드를 덮어쓰는 버그가 있었다.)
    """
    buf = _get_pack(pack)
    node_id = f"text:{secrets.token_hex(4)}"
    while node_id in buf["nodes"]:
        node_id = f"text:{secrets.token_hex(4)}"
    props = {"text": text, "label": node_id}
    provenance = None
    if source_id:
        props["source_id"] = source_id
        src = buf["nodes"].get(source_id)
        if src and src["space"] == "resource":
            buf["edges"].append({"from_space": "resource", "from_id": source_id,
                                 "relation": "contains", "to_space": "evidence",
                                 "to_id": node_id, "properties": {}})
            provenance = f"{source_id} -contains-> {node_id}"
        elif src:
            provenance = f"출처 '{source_id}'는 {src['space']} 공간이라 contains 엣지 미생성 (속성으로만 기록)"
        else:
            provenance = f"출처 노드 '{source_id}'가 팩에 없어 속성으로만 기록 (필요시 resource 노드로 추가)"
    node_data = {"space": "evidence", "node_type": "TextUnit", "properties": props}
    buf["nodes"][node_id] = node_data
    out = {"stores": {"buffer": "ok", "sqlite": _persist(pack)}, "node_data": {node_id: node_data}}
    if provenance:
        out["provenance"] = provenance
    return out


@mcp.tool()
def query_bm25(question: str, limit: int = 5) -> dict:
    """버퍼(토큰 겹침) + Neo4j 읽기전용 fulltext(있으면)를 합쳐 질문과 관련된 노드를 찾는다."""
    buffer_hits = _buffer_search(DEFAULT_PACK, question, limit)
    cloud_hits = _cloud_search(question, limit)
    return {"results": (buffer_hits + cloud_hits)[:limit]}


@mcp.tool()
def ontology_query(question: str, limit: int = 5, pack: str = DEFAULT_PACK) -> dict:
    """query_bm25 결과에 더해, 상위 버퍼 노드의 1-hop 엣지(graph_context)를 같이 반환한다."""
    buffer_hits = _buffer_search(pack, question, limit)
    cloud_hits = _cloud_search(question, limit)
    graph_context = []
    top_ids = {h["id"] for h in buffer_hits}
    for edge in _get_pack(pack)["edges"]:
        if edge["from_id"] in top_ids or edge["to_id"] in top_ids:
            graph_context.append({"from": edge["from_id"], "rel": edge["relation"],
                                   "to": edge["to_id"]})
    return {"results": (buffer_hits + cloud_hits)[:limit], "graph_context": graph_context}


@mcp.tool()
def pack_ask(question: str, pack: str = DEFAULT_PACK) -> dict:
    """질문에 가장 잘 맞는 버퍼 노드를 찾고 그래프 엣지를 최대 3홉 따라가며 근거 사슬을 만든다."""
    buffer_hits = _buffer_search(pack, question, 1)
    # 근거 부족 게이트: 정확히 겹친 토큰이 2개 미만이면(전방일치 잡음뿐) 매칭으로 치지 않는다
    if buffer_hits and buffer_hits[0].get("exact", 0) < 2 and buffer_hits[0]["score"] < 10:
        return {"matched": None, "chain": [], "status": "no_grounded_match",
                "explanation": "팩에 이 질문을 뒷받침할 근거가 없습니다. 팩 밖 지식으로 답하지 마세요."}
    if not buffer_hits:
        cloud_hits = _cloud_search(question, 5)
        return {"matched": None, "chain": [], "explanation": "버퍼에 일치하는 노드가 없습니다.",
                "참고(cloud)": cloud_hits}
    matched = buffer_hits[0]
    edges = _get_pack(pack)["edges"]
    chain = []
    current = matched["id"]
    visited = {current}
    for _ in range(3):
        step = None
        for e in edges:
            other = e["to_id"] if e["from_id"] == current else (e["from_id"] if e["to_id"] == current else None)
            if other and other not in visited:
                step = (e, other)
                break
        if not step:
            break
        e, other = step
        chain.append({"from": e["from_id"], "rel": e["relation"], "to": e["to_id"]})
        visited.add(other)
        current = other

    def _label(nid: str) -> str:
        n = _get_pack(pack)["nodes"].get(nid)
        return n.get("properties", {}).get("label", nid) if n else nid

    explanation = _label(matched["id"])
    prev = matched["id"]
    for c in chain:
        next_id = c["to"] if c["from"] == prev else c["from"]
        explanation += f" -({c['rel']})-> {_label(next_id)}"
        prev = next_id
    cloud_hits = _cloud_search(question, 3)
    return {"matched": matched, "chain": chain, "explanation": explanation, "참고(cloud)": cloud_hits}


_UPLOADS: dict[str, bytes] = {}  # token -> 업로드된 zip bytes


def _import_pack_zip(data: bytes, pack: str) -> dict:
    """정본 계약 zip(nodes.jsonl + edges.jsonl, 하위폴더 허용)을 팩 버퍼로 적재한다."""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()

    def _find(suffix: str) -> str | None:
        cands = [n for n in names if n.endswith(suffix) and "__MACOSX" not in n]
        return min(cands, key=len) if cands else None

    nodes_f, edges_f = _find("nodes.jsonl"), _find("edges.jsonl")
    extras = {}
    for name in ("evidence.jsonl", "reviews.jsonl", "pack.yaml"):
        fn = _find(name)
        if fn:
            extras[name] = z.read(fn).decode("utf-8")
    if not nodes_f:
        return {"error": f"zip 안에 nodes.jsonl이 없습니다. 파일 목록: {names[:10]}"}
    buf = {"nodes": {}, "edges": [], "schema_packs": [], "extras": extras}
    for line in z.read(nodes_f).decode("utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        nid = d.get("id") or d.get("node_id")
        props = dict(d.get("properties", {}))
        # 정본은 label이 top-level에 올 수 있음 → properties로 정규화
        if "label" not in props and d.get("label"):
            props["label"] = d["label"]
        buf["nodes"][nid] = {"space": d.get("space", "concept"),
                             "node_type": d.get("node_type") or d.get("type", "Concept"),
                             "properties": props}
    if edges_f:
        for line in z.read(edges_f).decode("utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            buf["edges"].append({
                "from_space": d.get("from_space", ""),
                "from_id": d.get("from_id") or d.get("source"),
                "relation": d.get("relation", "related_to"),
                "to_space": d.get("to_space", ""),
                "to_id": d.get("to_id") or d.get("target"),
                "properties": d.get("properties", {}),
            })
    PACKS[pack] = buf
    status = _persist(pack)
    return {"pack": pack, "nodes": len(buf["nodes"]), "edges": len(buf["edges"]),
            "stores": {"buffer": "ok", "sqlite": status}}


@mcp.tool()
def pack_import(source: str, pack: str = "정본팩") -> dict:
    """정본 zip을 팩으로 가져온다. source = 업로드 토큰(/upload 응답) 또는 zip URL.

    가져온 뒤에는 pack_ask/ontology_query에 pack 이름을 주면 바로 질의된다.
    """
    data = _UPLOADS.pop(source, None)
    if data is None and source.startswith("http"):
        import urllib.request
        with urllib.request.urlopen(source, timeout=60) as r:
            data = r.read()
    if data is None:
        return {"error": "source가 업로드 토큰도 URL도 아닙니다. 먼저 zip을 POST /upload 하세요."}
    try:
        r = _import_pack_zip(data, pack)
        if isinstance(r, dict) and "error" not in r:
            qa = _qa_scan(pack)
            r["qa_status"] = qa["status"]
            r["qa_issues"] = qa["counts"]["issues"]
        return r
    except Exception as e:
        return {"error": f"가져오기 실패: {e}"}


@mcp.tool()
def pack_build_queryable(source_zip: str, out_zip: str | None = None,
                          include_embeddings: bool = True) -> dict:
    """정본 zip을 self-contained 질의 가능 zip(indexes/ + runtime/ + reports/ 동봉)으로 재구성한다."""
    from . import local_pack
    return local_pack.build_queryable(source_zip, out_zip, include_embeddings)


def _hydrate_from_zip(zip_path: str, pack: str | None = None) -> dict:
    """저장된 팩 zip의 정본(nodes/edges + 커스텀 그래머)을 저작 버퍼(PACKS)로 복원한다."""
    z = zipfile.ZipFile(os.path.expanduser(zip_path))
    names = set(z.namelist())
    meta: dict[str, str] = {}
    if "pack.yaml" in names:
        for ln in z.read("pack.yaml").decode("utf-8", errors="ignore").splitlines():
            if ": " in ln:
                k, v = ln.split(": ", 1)
                meta[k.strip()] = v.strip()
    pack = pack or meta.get("title", "").strip('"') or \
        os.path.splitext(os.path.basename(zip_path))[0]
    buf = _get_pack(pack)
    if buf["nodes"]:
        return {"error": f"저작 버퍼 '{pack}'에 이미 노드 {len(buf['nodes'])}개가 있습니다. "
                          "덮어쓰지 않습니다. pack 파라미터로 다른 이름을 지정하세요."}
    for key, target in (("custom_node_types", "custom_types"), ("custom_relations", "custom_relations")):
        if key in meta:
            try:
                buf[target] = json.loads(meta[key])
            except Exception:
                pass
    if "schema_packs" in meta:
        try:
            buf["schema_packs"] = [p for p in json.loads(meta["schema_packs"])
                                    if p in MANIFEST["schema_packs"]]
        except Exception:
            pass
    nodes_file = "graph/nodes.jsonl" if "graph/nodes.jsonl" in names else "nodes.jsonl"
    edges_file = "graph/edges.jsonl" if "graph/edges.jsonl" in names else "edges.jsonl"
    for ln in z.read(nodes_file).decode("utf-8", errors="ignore").splitlines():
        if not ln.strip():
            continue
        n = json.loads(ln)
        props = n.get("properties") or {}
        if n.get("label") and "label" not in props:
            props["label"] = n["label"]
        buf["nodes"][n["id"]] = {"space": n["space"], "node_type": n["node_type"],
                                  "properties": props}
    for ln in z.read(edges_file).decode("utf-8", errors="ignore").splitlines():
        if not ln.strip():
            continue
        e = json.loads(ln)
        fid = e.get("from_id") or e.get("source")
        tid = e.get("to_id") or e.get("target")
        buf["edges"].append({
            "from_space": e.get("from_space") or buf["nodes"].get(fid, {}).get("space"),
            "from_id": fid, "relation": e["relation"],
            "to_space": e.get("to_space") or buf["nodes"].get(tid, {}).get("space"),
            "to_id": tid, "properties": e.get("properties") or {}})
    _persist(pack)
    return {"authoring_pack": pack,
            "counts": {"nodes": len(buf["nodes"]), "edges": len(buf["edges"])},
            "custom_grammar": {"node_types": buf.get("custom_types", {}),
                                "relations": buf.get("custom_relations", [])},
            "schema_packs": buf["schema_packs"]}


@mcp.tool()
def pack_list_local(directory: str = "") -> dict:
    """팩 서랍을 스캔해 사용 가능한 팩 zip 목록을 반환한다 (최신순).

    directory 생략 시 YUPACK_PACK_DIR, 그것도 없으면 ~/Zettelkasten/70_Ontology.
    경로를 모를 때는 이 도구부터 호출해 최신 zip을 고르면 된다.
    """
    import glob as _glob
    d = os.path.expanduser(directory or os.environ.get("YUPACK_PACK_DIR")
                           or "~/Zettelkasten/70_Ontology")
    if not os.path.isdir(d):
        return {"error": f"디렉토리가 없습니다: {d}", "packs": []}
    packs = []
    for f in _glob.glob(os.path.join(d, "**", "*.zip"), recursive=True):
        if "_archive" in f or not os.path.isfile(f):
            continue
        st = os.stat(f)
        packs.append({"path": f, "size_mb": round(st.st_size / 1e6, 1),
                      "modified": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    packs.sort(key=lambda x: x["modified"], reverse=True)
    return {"directory": d, "count": len(packs), "packs": packs,
            "hint": "최신 팩을 pack_open_local(path)로 여세요."}


@mcp.tool()
def pack_open_local(zip_path: str, mode: str = "read_only") -> dict:
    """로컬 zip 팩을 열고 manifest.lock으로 무결성을 검증한다. pack_handle을 반환한다.

    mode="read_only"(기본): 질의 전용 핸들만 연다.
    mode="authoring": 핸들과 함께 zip의 정본을 저작 버퍼로 복원한다. 이후
      ontology_add_node/pack_qa/pack_save를 반환된 authoring_pack 이름으로 호출하면
      이어서 편집·검사·재저장할 수 있다. (핸들 id는 저작 버퍼가 아니다 - 혼용 금지)
    """
    from . import local_pack
    try:
        r = local_pack.open_local(zip_path, "read_only")
    except Exception as e:
        return {"error": f"열기 실패: {e}",
                "hint": "경로가 바뀌었거나 zip이 아닐 수 있습니다. pack_list_local()로 현재 팩 목록을 확인하세요."}
    if mode == "authoring" and isinstance(r, dict) and "error" not in r:
        h = _hydrate_from_zip(zip_path)
        if "error" in h:
            r["authoring"] = h
        else:
            r["authoring_pack"] = h["authoring_pack"]
            r["authoring_counts"] = h["counts"]
            r["hint"] = (f"이어서 편집하려면 pack=\"{h['authoring_pack']}\"로 "
                         "ontology_add_node/pack_qa/pack_save를 호출하세요.")
    return r


@mcp.tool()
def pack_status(pack_handle: str) -> dict:
    """열린 로컬 팩의 무결성·카운트·모드를 반환한다."""
    from . import local_pack
    pk = local_pack.get(pack_handle)
    return pk.status() if pk else {"error": f"핸들 없음: {pack_handle}"}


@mcp.tool()
def pack_ask_local(question: str, pack_handle: str = "", top_k: int = 6) -> dict:
    """열린 로컬 팩에 질의한다. lexical+vector+graph 3중 검색, 근거 카드와
    retrieval_trace를 반환하며 근거가 없으면 no_local_evidence를 반환한다.
    pack_handle을 생략하면 YUPACK_AUTO_OPEN 팩 또는 마지막으로 연 팩을 쓴다.
    에이전트 개발·MCP·메모리·프롬프트 인젝션·회귀 질문이면 답하기 전에 먼저 호출할 것."""
    from . import local_pack
    if pack_handle:
        pk = local_pack.get(pack_handle)
        if not pk:
            return {"error": f"핸들 없음: {pack_handle}"}
        return pk.ask(question, top_k)
    return local_pack.ask_all(question, top_k)


@mcp.tool()
def pack_close(pack_handle: str) -> dict:
    """열린 로컬 팩 핸들을 닫는다."""
    from . import local_pack
    return local_pack.close(pack_handle)


@mcp.tool()
def pack_set_default(zip_path: str) -> dict:
    """이 컴퓨터의 기본 팩을 지정한다 (Codex 등록의 YUPACK_AUTO_OPEN을 갱신).

    이후 새 Codex 작업마다 이 팩이 자동으로 열려, 사용자는 pack_open_local 없이 바로 질문할 수 있다.
    zip_path는 사용자가 자기 팩을 저장한 경로다. 모르면 사용자에게 물어라 (사람마다 다르다).
    """
    import tomllib
    zip_path = os.path.expanduser(zip_path)
    if not os.path.exists(zip_path):
        return {"error": f"팩 파일이 없습니다: {zip_path}. 저장한 팩 zip 경로를 확인하세요."}
    cfg = os.path.expanduser("~/.codex/config.toml")
    if not os.path.exists(cfg):
        return {"error": "~/.codex/config.toml이 없습니다. Codex CLI 설치 후 yupack MCP를 먼저 등록하세요."}
    text = open(cfg, encoding="utf-8").read()
    try:
        data = tomllib.loads(text)
    except Exception as e:
        return {"error": f"config.toml 파싱 실패: {e}"}
    srv = (data.get("mcp_servers") or {})
    name = "yupack-local" if "yupack-local" in srv else ("yupack" if "yupack" in srv else None)
    if not name:
        return {"error": "config.toml에 yupack MCP 등록이 없습니다. 먼저 codex mcp add로 등록하세요."}
    # [mcp_servers.<name>.env] 섹션의 YUPACK_AUTO_OPEN 라인을 갱신/추가 (다른 섹션 무손상)
    lines = text.splitlines()
    env_hdr = f"[mcp_servers.{name}.env]"
    out, in_env, done = [], False, False
    for ln in lines:
        st = ln.strip()
        if st.startswith("[") and st != env_hdr and in_env:
            if not done:
                out.append(f'YUPACK_AUTO_OPEN = "{zip_path}"')
                out.append("")
                done = True
            in_env = False
        if st == env_hdr:
            in_env = True
        if in_env and st.startswith("YUPACK_AUTO_OPEN"):
            out.append(f'YUPACK_AUTO_OPEN = "{zip_path}"')
            done = True
            continue
        out.append(ln)
    if in_env and not done:
        out.append(f'YUPACK_AUTO_OPEN = "{zip_path}"')
        done = True
    if not done:  # env 섹션 자체가 없으면 추가
        out += ["", env_hdr, f'YUPACK_AUTO_OPEN = "{zip_path}"']
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return {"ok": True, "default_pack": zip_path, "server": name,
            "note": "기본 팩 지정 완료. Codex를 재시작하면 이 팩이 자동으로 열려 바로 질문할 수 있습니다."}


@mcp.tool()
def pack_create(pack: str) -> dict:
    """새 팩 버퍼를 만든다 (이후 add_node/ingest/extract로 채우고 pack_save로 내보낸다).

    같은 이름이 이미 있으면 오류가 아니라 현황을 돌려준다 (이어서 작업 가능).
    """
    if pack in PACKS:
        buf = PACKS[pack]
        return {"created": False, "exists": pack,
                "nodes": len(buf["nodes"]), "edges": len(buf["edges"]),
                "custom_types": buf.get("custom_types", {}),
                "hint": "기존 팩입니다. 그대로 add_node/ingest로 이어서 작업하거나, 다른 이름으로 새로 만드세요."}
    _get_pack(pack)
    return {"created": pack, "stores": {"buffer": "ok", "sqlite": _persist(pack)}}


@mcp.tool()
def pack_ingest_local_zip(zip_path: str, pack: str = DEFAULT_PACK,
                           max_files: int = 0, max_nodes_per_file: int = 6) -> dict:
    """로컬 md 묶음 zip을 한 번에 읽어 파일마다 개념·관계를 추출해 팩에 누적한다.

    원본 노트 zip(예: 921개 md)을 통째로 넣어 정본 팩을 만드는 진입점.
    각 md는 ontology_extract와 같은 방식으로 LLM 추출한다(OPENAI_API_KEY 필요).
    max_files=0이면 전부. 대량이면 파일마다 LLM 호출이라 비용·시간이 든다.
    각 파일은 evidence 노드(원문)로도 남겨 근거를 보존한다.
    """
    zip_path = os.path.expanduser(zip_path)
    if not os.path.exists(zip_path):
        return {"error": f"zip이 없습니다: {zip_path}",
                "hint": "경로가 바뀌었을 수 있습니다. pack_list_local()로 현재 팩 목록을 확인하세요."}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"error": "OPENAI_API_KEY 없음: 대량 추출은 LLM이 필요합니다. "
                          "임베딩만 있는 팩이 필요하면 build 스크립트로 만든 정본을 pack_import 하세요."}
    z = zipfile.ZipFile(zip_path)
    mds = [n for n in z.namelist()
           if n.lower().endswith((".md", ".markdown", ".txt"))
           and "__MACOSX" not in n and not n.endswith("/")]
    if max_files > 0:
        mds = mds[:max_files]
    if not mds:
        return {"error": "zip 안에 md/txt 파일이 없습니다."}
    _get_pack(pack)
    done, nodes_added, edges_added, errors = 0, 0, 0, 0
    for name in mds:
        try:
            text = z.read(name).decode("utf-8", errors="ignore")
        except Exception:
            errors += 1
            continue
        if not text.strip():
            continue
        # 원문을 evidence 노드로 보존 (동명 파일 충돌 시 전체 경로 해시로 고유화)
        buf = _get_pack(pack)
        src_id = f"src:{_safe_filename(os.path.basename(name))[:40]}"
        prev = buf["nodes"].get(src_id)
        if prev and prev.get("properties", {}).get("source_locator") != name:
            src_id = f"{src_id}-{hashlib.sha256(name.encode()).hexdigest()[:6]}"
        buf["nodes"][src_id] = {
            "space": "evidence", "node_type": "TextUnit",
            "properties": {"label": os.path.basename(name), "text": text[:4000],
                            "source_locator": name}}
        nodes_added += 1
        # 개념·관계 추출 (extract 재사용)
        r = ontology_extract(text=text, pack=pack, max_nodes=max_nodes_per_file)
        if "error" in r:
            errors += 1
        else:
            nodes_added += r.get("added", {}).get("nodes", 0)
            edges_added += r.get("added", {}).get("edges", 0)
        done += 1
    qa = _qa_scan(pack)
    return {"ingested_files": done, "total_files": len(mds),
            "nodes_added": nodes_added, "edges_added": edges_added, "errors": errors,
            "qa_status": qa["status"], "qa_issues": qa["counts"]["issues"],
            "stores": {"buffer": "ok", "sqlite": _persist(pack)},
            "next": ("pack_qa로 위반을 확인·수정한 뒤 " if qa["status"] != "pass" else "")
                    + f"pack_save(pack=\"{pack}\", save_to=\"<폴더>\")로 질의 가능 팩을 저장하세요."}


@mcp.tool()
def pack_list() -> dict:
    """현재 세션의 팩 이름과 노드/엣지 개수를 나열한다."""
    return {
        name: {"nodes": len(p["nodes"]), "edges": len(p["edges"]),
               "schema_packs": p["schema_packs"],
               "custom_types": {s: len(t) for s, t in p.get("custom_types", {}).items()},
               "custom_relation_pairs": len(p.get("custom_relations", []))}
        for name, p in PACKS.items()
    }


@mcp.tool()
def pack_save(pack: str = DEFAULT_PACK, include_embeddings: bool = True,
              save_to: str | None = None) -> dict:
    """현재 팩을 질의 가능 완성 zip으로 저장한다.

    구조(표준 계약): pack.yaml + nodes/edges/evidence/reviews.jsonl + query-docs/ +
    embeddings.json + lexical-index/ + vector-index/ + graph-index/ +
    query-contract.json + integrity.json (+ runtime/, notes/).
    이 zip은 pack_open_local로 열어 바로 질의할 수 있다.

    save_to: 저장할 폴더/파일 경로. 볼트 위치는 사람마다 다르므로 임의로 정하지 말고
      **저장 전에 반드시 사용자에게 "압축파일을 어느 폴더에 저장할까요? (옵시디언 볼트의
      팩 폴더 경로)"라고 물어서** 받은 경로를 save_to로 넘겨라. save_to가 비어 있으면
      이 도구는 저장하지 않고 경로를 물으라는 안내(needs_save_path)를 반환한다.
      서버(챗지피티) 모드에서는 디스크에 못 쓰므로 다운로드 링크를 준다.
    """
    import tempfile as _tf
    from . import local_pack
    buf = _get_pack(pack)
    if not buf["nodes"]:
        return {"error": f"팩이 비어 있습니다: {pack}"}
    today = datetime.date.today().isoformat()

    # 1) 버퍼 -> 정본 5파일 (evidence 노드는 evidence.jsonl로도 승격)
    nodes_l, evidence_l = [], []
    for nid, n in buf["nodes"].items():
        props = n.get("properties", {})
        nodes_l.append(json.dumps({"id": nid, "space": n["space"],
                                    "node_type": n["node_type"], "label": props.get("label"),
                                    "properties": props}, ensure_ascii=False))
        if n["space"] == "evidence":
            evidence_l.append(json.dumps({
                "evidence_id": nid,
                "summary": props.get("text") or props.get("definition") or props.get("label"),
                "conditions": props.get("conditions"), "limitations": props.get("limitations"),
                "evidence_grade": props.get("evidence_grade"),
                "source_id": props.get("source_id"), "source_locator": props.get("source_locator"),
            }, ensure_ascii=False))
    edges_l = [json.dumps({"source": e["from_id"], "target": e["to_id"],
                            "relation": e["relation"], "properties": e.get("properties", {})},
                           ensure_ascii=False) for e in buf["edges"]]
    pack_yaml = (f"schema: yucrates.ontology.v1.2\npack_id: {_safe_filename(pack)}-{today}\n"
                 f"title: \"{pack}\"\nversion: 1.0.0\ncreated: {today}\n"
                 f"manifest_version: \"{MANIFEST['version']}\"\n"
                 f"schema_packs: {json.dumps(buf['schema_packs'], ensure_ascii=False)}\n"
                 f"custom_node_types: {json.dumps(buf.get('custom_types', {}), ensure_ascii=False)}\n"
                 f"custom_relations: {json.dumps(buf.get('custom_relations', []), ensure_ascii=False)}\n"
                 f"storage: \"local zip only, production DB 미접촉\"\n")

    with _tf.TemporaryDirectory() as td:
        src = os.path.join(td, "canonical.zip")
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("pack.yaml", pack_yaml)
            z.writestr("nodes.jsonl", "\n".join(nodes_l))
            z.writestr("edges.jsonl", "\n".join(edges_l))
            z.writestr("evidence.jsonl", "\n".join(evidence_l))
            z.writestr("reviews.jsonl", "")
        out = os.path.join(td, "pack-queryable.zip")
        # 2) 같은 표준 파이프라인으로 질의 가능 zip 생성
        r = local_pack.build_queryable(src, out, include_embeddings)
        if "error" in r:
            return r
        # 3) notes/ md 부속 + opencrab-pack-v1 계약 아티팩트 추가
        qa = _qa_scan(pack)
        contract = _contract_files(pack, buf, today, qa)
        with zipfile.ZipFile(out, "a", zipfile.ZIP_DEFLATED) as z:
            for name, content in contract.items():
                z.writestr(name, content)
            used_names: set[str] = set()
            for nid, n in buf["nodes"].items():
                props = n.get("properties", {})
                label = props.get("label", nid)
                base = _safe_filename(label)
                fname = base
                while fname in used_names:  # 동일 라벨 노드 md 덮어쓰기 방지
                    fname = f"{base}-{hashlib.sha256(nid.encode()).hexdigest()[:6]}"
                used_names.add(fname)
                z.writestr(f"notes/{fname}.md",
                           f"---\nid: {nid}\nspace: {n['space']}\ntype: {n['node_type']}\n---\n"
                           f"# {label}\n\n{props.get('definition', props.get('text', ''))}\n")
        data = open(out, "rb").read()

    result = {"structure": r["files"] if isinstance(r.get("files"), list) else None,
              "counts": r["counts"], "embeddings": r["embeddings"],
              "contract_files": list(contract.keys()),
              "qa_status": qa["status"], "qa_issues": qa["counts"]["issues"]}
    emb = r.get("embeddings") or {}
    if not (isinstance(emb, dict) and emb.get("count")):
        result["embedding_warning"] = ("임베딩 0개로 저장됐습니다. 한국어 등 의미(벡터) 질의가 "
                                        "약해집니다. include_embeddings=true와 OPENAI_API_KEY "
                                        "설정을 확인한 뒤 다시 저장하는 것을 권장합니다.")
    if qa["status"] != "pass":
        result["qa_warning"] = ("팩에 그래머 위반이 있습니다. quality/report.json 참조. "
                                "pack_qa로 확인 후 수정하고 다시 저장하는 것을 권장합니다.")
    is_server = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    # 로컬 모드에서 저장 경로 미지정 -> 저장하지 않고 사용자에게 경로를 묻게 한다
    if not save_to and not is_server:
        result["status"] = "needs_save_path"
        result["ask_user"] = "압축파일(팩)을 어느 폴더에 저장할까요? 옵시디언 볼트의 팩 폴더 경로를 알려주세요."
        return result
    if is_server:
        tok = secrets.token_urlsafe(8)
        _BUNDLES[tok] = data
        result["download_url"] = f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}/download/{tok}"
        result["note"] = "서버 모드: 다운로드 후 로컬 팩 폴더로 옮기세요."
        return result
    # 로컬: 사용자가 준 경로 아래에 팩별 폴더를 유팩이 만들어 저장
    base = os.path.expanduser(save_to)
    pack_dir = os.path.join(base, f"{_safe_filename(pack)}-{today}")
    os.makedirs(pack_dir, exist_ok=True)
    dest = os.path.join(pack_dir, "pack.zip")
    with open(dest, "wb") as fh:
        fh.write(data)
    result["saved_to"] = dest
    result["pack_folder"] = pack_dir
    result["note"] = f"'{pack}' 팩을 저장했습니다: {dest}"
    result["next_question"] = ("이 팩을 기본 팩으로 등록할까요? 등록하면 Codex를 켤 때마다 "
                               "자동으로 열려 pack_open_local 없이 바로 질문할 수 있습니다. "
                               f"등록하려면 pack_set_default(\"{dest}\")를 호출하세요.")
    return result


from . import governance as _governance


@mcp.tool()
def workflow_create_run(pack_handle: str, kind: str) -> dict:
    """authoring 상태의 로컬 workflow run을 생성한다."""
    return _governance.workflow_create_run(pack_handle, kind)


@mcp.tool()
def workflow_advance(pack_handle: str, run_id: str) -> dict:
    """workflow를 authoring, validation, independent_review, promotion 순서로 한 단계 전진시킨다."""
    return _governance.workflow_advance(pack_handle, run_id)


@mcp.tool()
def approval_request(pack_handle: str, target: str, reason: str) -> dict:
    """승인 요청을 pending으로 기록하고 자동 승인하지 않는다."""
    return _governance.approval_request(pack_handle, target, reason)


@mcp.tool()
def harness_promotion_apply(pack_handle: str, candidate_id: str) -> dict:
    """promoted 상태의 후보만 로컬 overlay에 적용 표시한다."""
    return _governance.harness_promotion_apply(pack_handle, candidate_id)


@mcp.tool()
def identity_add_alias(pack_handle: str, alias: str, canonical: str) -> dict:
    """identity alias를 canonical id에 연결한다."""
    return _governance.identity_add_alias(pack_handle, alias, canonical)


@mcp.tool()
def identity_resolve_canonical(pack_handle: str, identity: str) -> dict:
    """alias 체인을 따라 identity의 canonical id를 반환한다."""
    return _governance.identity_resolve_canonical(pack_handle, identity)


@mcp.tool()
def identity_propose_duplicate(pack_handle: str, a: str, b: str) -> dict:
    """두 identity를 pending duplicate 후보로 기록한다."""
    return _governance.identity_propose_duplicate(pack_handle, a, b)


@mcp.tool()
def identity_resolve_duplicate(pack_handle: str, a: str, b: str,
                               decision: str) -> dict:
    """duplicate 후보를 merge 또는 distinct로 확정한다."""
    return _governance.identity_resolve_duplicate(pack_handle, a, b, decision)


@mcp.tool()
def identity_list_pending_duplicates(pack_handle: str) -> dict:
    """pending duplicate 후보를 나열한다."""
    return _governance.identity_list_pending_duplicates(pack_handle)


@mcp.tool()
def canonicalize_merge_nodes(pack_handle: str, keep_id: str, drop_id: str) -> dict:
    """원본을 수정하지 않고 두 노드의 merge overlay와 alias를 기록한다."""
    return _governance.canonicalize_merge_nodes(pack_handle, keep_id, drop_id)


@mcp.tool()
def canonicalize_find_and_propose(pack_handle: str, threshold: float = 0.9) -> dict:
    """label이 정확히 같은 노드쌍을 pending duplicate 후보로 제안한다."""
    return _governance.canonicalize_find_and_propose(pack_handle, threshold)


@mcp.tool()
def promotion_register_candidate(pack_handle: str, node_id: str) -> dict:
    """노드를 독립 검수 전 promotion 후보로 등록한다."""
    return _governance.promotion_register_candidate(pack_handle, node_id)


@mcp.tool()
def promotion_validate_candidate(pack_handle: str, candidate_id: str) -> dict:
    """promotion 후보의 로컬 evidence_refs를 검사하고 validation만 기록한다."""
    return _governance.promotion_validate_candidate(pack_handle, candidate_id)


@mcp.tool()
def promotion_promote(pack_handle: str, candidate_id: str,
                      independent_review: bool = False) -> dict:
    """독립 검수를 명시한 promotion 후보만 promoted로 바꾼다."""
    return _governance.promotion_promote(pack_handle, candidate_id, independent_review)


@mcp.tool()
def promotion_reject(pack_handle: str, candidate_id: str) -> dict:
    """promotion 후보를 rejected 상태로 기록한다."""
    return _governance.promotion_reject(pack_handle, candidate_id)


@mcp.tool()
def billing_get_usage(pack_handle: str) -> dict:
    """로컬 zip, 캐시, 노드, 벡터, audit event 사용량을 반환한다."""
    return _governance.billing_get_usage(pack_handle)


@mcp.tool()
def billing_list_events(pack_handle: str, limit: int = 50) -> dict:
    """로컬 audit event를 최신순으로 반환한다."""
    return _governance.billing_list_events(pack_handle, limit)


@mcp.tool()
def ontology_rebac_check(pack_handle: str, actor: str, action: str) -> dict:
    """runtime/policy.yaml의 rebac 규칙으로 action 허용 여부를 확인한다."""
    return _governance.ontology_rebac_check(pack_handle, actor, action)


def build_app():
    # /mcp = streamable HTTP (신형 클라이언트), /sse+/messages = legacy SSE
    app = mcp.streamable_http_app()
    app.router.routes.extend(mcp.sse_app().router.routes)
    try:
        from starlette.routing import Route
        from starlette.responses import Response

        async def download(request):
            data = _BUNDLES.get(request.path_params["token"])
            if not data:
                return Response("링크가 만료되었습니다. pack_save를 다시 호출하세요.", status_code=404)
            return Response(data, media_type="application/zip",
                            headers={"Content-Disposition": 'attachment; filename="yupack.zip"'})
        app.router.routes.append(Route("/download/{token}", download))

        async def upload(request):
            data = await request.body()
            if not data:
                return Response("빈 본문", status_code=400)
            tok = secrets.token_urlsafe(8)
            _UPLOADS[tok] = data
            if len(_UPLOADS) > 20:
                for k in list(_UPLOADS)[:10]:
                    _UPLOADS.pop(k, None)
            return Response(json.dumps({"token": tok, "bytes": len(data)}),
                            media_type="application/json")
        app.router.routes.append(Route("/upload", upload, methods=["POST"]))
    except Exception:
        pass
    return app


def main():
    # 서버(Railway) 모드에서만 starlette/uvicorn 필요. 로컬 stdio는 이 경로를 안 탄다.
    import uvicorn
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


# ASGI 서버(uvicorn yupack_mcp.server:app)용. starlette 없으면 None (로컬 stdio 무관).
try:
    app = build_app()
except Exception:
    app = None


def local_main():
    """로컬 stdio MCP (Codex/Claude 데스크톱용): codex mcp add yupack -- python3 -m yupack_mcp.local"""
    mcp.run()


if __name__ == "__main__":
    main()
