"""yupack: 수업용 팩 공방 MCP 서버.

핵심 원칙: 모든 쓰기는 팩 버퍼(PACKS) + 로컬 SQLite에만 간다. 외부 프로덕션 DB(Neo4j)에는
절대 쓰지 않는다 (읽기 전용 Cypher만). pack_save는 정본 임포터 계약 zip(nodes.jsonl +
edges.jsonl + pack.yaml)을 꺼내 준다.
"""
from __future__ import annotations
import datetime
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

mcp = FastMCP("yupack", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

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
_DB_PATH = os.environ.get("YUPACK_DB") or (
    "/data/yupack.db" if os.path.isdir("/data") else str(Path(__file__).parent / "yupack.db"))


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


def _store_zip(files: dict[str, str]) -> str:
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
def ontology_extract(text: str, pack: str = DEFAULT_PACK, max_nodes: int = 8) -> dict:
    """텍스트에서 개념 노드와 관계를 LLM으로 추출해 팩 버퍼에 넣는다 (DB 쓰기 없음)."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return {"error": "OPENAI_API_KEY 없음: extract는 LLM이 필요합니다."}
    from openai import OpenAI
    client = OpenAI(api_key=key)
    model = os.environ.get("OPENAI_EXTRACT_MODEL", "gpt-4o-mini")
    prompt = (f"다음 텍스트에서 핵심 개념 최대 {max_nodes}개와 개념 사이 관계를 추출해 JSON으로만 답하라. "
              '형식: {"nodes":[{"id":"c:슬러그","label":"...","definition":"..."}],'
              '"edges":[{"from":"c:슬러그","relation":"prerequisite_of|related_to|part_of","to":"c:슬러그"}]}'
              f"\n\n텍스트:\n{text[:6000]}")
    resp = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}],
                                          response_format={"type": "json_object"})
    data = json.loads(resp.choices[0].message.content)
    buf = _get_pack(pack)
    added_n, added_e = 0, 0
    for n in data.get("nodes", [])[:max_nodes]:
        nid = n.get("id") or f"c:{secrets.token_hex(3)}"
        buf["nodes"][nid] = {"space": "concept", "node_type": "Concept",
                             "properties": {"label": n.get("label", nid),
                                             "definition": n.get("definition", "")}}
        added_n += 1
    for e in data.get("edges", []):
        if e.get("from") in buf["nodes"] and e.get("to") in buf["nodes"]:
            buf["edges"].append({"from_space": "concept", "from_id": e["from"],
                                  "relation": e.get("relation", "related_to"),
                                  "to_space": "concept", "to_id": e["to"], "properties": {}})
            added_e += 1
    return {"added": {"nodes": added_n, "edges": added_e},
            "stores": {"buffer": "ok", "sqlite": _persist(pack)}}


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
    """노드 하나를 현재 팩의 인메모리 버퍼에만 추가한다 (외부 DB 쓰기 없음)."""
    if space not in MANIFEST["spaces"]:
        return {"error": f"알 수 없는 space: {space}. 사용 가능: {list(MANIFEST['spaces'])}"}
    node_data = {"space": space, "node_type": node_type, "properties": properties or {}}
    _get_pack(pack)["nodes"][node_id] = node_data
    return {"stores": {"buffer": "ok", "sqlite": _persist(pack)}, "node_data": {node_id: node_data}}


@mcp.tool()
def ontology_add_edge(from_space: str, from_id: str, relation: str, to_space: str,
                       to_id: str, properties: dict | None = None,
                       pack: str = DEFAULT_PACK) -> dict:
    """엣지 하나를 현재 팩의 인메모리 버퍼에만 추가한다. 정의된 관계인지 그래머로 검증한다."""
    allowed = _allowed_relations(from_space, to_space)
    if allowed is not None and relation not in allowed:
        return {"error": f"'{from_space}'→'{to_space}' 간 허용되지 않은 관계: {relation}. "
                          f"허용된 관계: {allowed}"}
    edge = {"from_space": from_space, "from_id": from_id, "relation": relation,
            "to_space": to_space, "to_id": to_id, "properties": properties or {}}
    _get_pack(pack)["edges"].append(edge)
    return {"stores": {"buffer": "ok", "sqlite": _persist(pack)}, "edge": edge}


@mcp.tool()
def ontology_ingest(text: str, source_id: str | None = None, pack: str = DEFAULT_PACK) -> dict:
    """텍스트를 evidence 공간의 TextUnit 노드 하나로 버퍼에 저장한다 (단순 분할, DB 쓰기 없음)."""
    node_id = source_id or f"text:{secrets.token_hex(4)}"
    node_data = {"space": "evidence", "node_type": "TextUnit",
                 "properties": {"text": text, "label": node_id}}
    _get_pack(pack)["nodes"][node_id] = node_data
    return {"stores": {"buffer": "ok", "sqlite": _persist(pack)}, "node_data": {node_id: node_data}}


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
    if not nodes_f:
        return {"error": f"zip 안에 nodes.jsonl이 없습니다. 파일 목록: {names[:10]}"}
    buf = {"nodes": {}, "edges": [], "schema_packs": []}
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
        return _import_pack_zip(data, pack)
    except Exception as e:
        return {"error": f"가져오기 실패: {e}"}


@mcp.tool()
def pack_list() -> dict:
    """현재 세션의 팩 이름과 노드/엣지 개수를 나열한다."""
    return {
        name: {"nodes": len(p["nodes"]), "edges": len(p["edges"]),
               "schema_packs": p["schema_packs"]}
        for name, p in PACKS.items()
    }


@mcp.tool()
def pack_save(pack: str = DEFAULT_PACK, include_embeddings: bool = True) -> dict:
    """현재 팩을 정본 임포터 계약 zip(nodes.jsonl + edges.jsonl + pack.yaml)으로 만들어 다운로드 링크를 준다.

    노드별 md(notes/)와 embeddings.json은 부속으로 동봉한다.
    """
    buf = _get_pack(pack)
    files: dict[str, str] = {}

    # 정본 계약: 임포터가 먹는 jsonl
    files["nodes.jsonl"] = "\n".join(
        json.dumps({"id": nid, "space": n["space"], "node_type": n["node_type"],
                    "properties": n.get("properties", {})}, ensure_ascii=False)
        for nid, n in buf["nodes"].items())
    files["edges.jsonl"] = "\n".join(
        json.dumps(e, ensure_ascii=False) for e in buf["edges"])
    today = datetime.date.today().isoformat()
    files["pack.yaml"] = (
        f"pack_id: {_safe_filename(pack)}-{today}\n"
        f"title: \"{pack}\"\n"
        f"version: 1.0.0\n"
        f"created: {today}\n"
        f"manifest_version: \"{MANIFEST['version']}\"\n"
        f"schema_packs: {json.dumps(buf['schema_packs'], ensure_ascii=False)}\n"
        f"counts:\n  nodes: {len(buf['nodes'])}\n  edges: {len(buf['edges'])}\n"
        f"storage: \"yupack local (sqlite/buffer), production DB 미접촉\"\n")

    for node_id, node in buf["nodes"].items():
        props = node.get("properties", {})
        label = props.get("label", node_id)
        definition = props.get("definition", props.get("text", ""))
        md = (f"---\nid: {node_id}\nspace: {node['space']}\ntype: {node['node_type']}\n---\n"
              f"# {label}\n\n{definition}\n")
        files[f"notes/{_safe_filename(label)}.md"] = md

    embeddings_status = "skipped(no key)"
    if include_embeddings:
        labels = [n.get("properties", {}).get("label", nid) + " " +
                  n.get("properties", {}).get("definition", "")
                  for nid, n in buf["nodes"].items()]
        node_ids = list(buf["nodes"].keys())
        if labels:
            vectors = _embed_texts(labels)
            if vectors is not None:
                files["embeddings.json"] = json.dumps(
                    dict(zip(node_ids, vectors)), ensure_ascii=False
                )
                embeddings_status = "included"
    else:
        embeddings_status = "skipped(disabled)"

    token = _store_zip(files)
    base = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    public_base = f"https://{base}" if base else "http://localhost:8000"
    return {
        "download_url": f"{public_base}/download/{token}",
        "counts": {"nodes": len(buf["nodes"]), "edges": len(buf["edges"])},
        "embeddings": embeddings_status,
    }


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


app = build_app()


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


if __name__ == "__main__":
    main()
