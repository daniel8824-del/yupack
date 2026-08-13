"""yupack: 수업용 팩 공방 MCP 서버.

핵심 원칙: 모든 쓰기는 팩 버퍼(PACKS) + 로컬 SQLite에만 간다. 외부 프로덕션 DB(Neo4j)에는
절대 쓰지 않는다 (읽기 전용 Cypher만). pack_save는 옵시디언 노트팩 폴더를 만든다.
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

from . import local_authoring
from . import yucrates_contract

# 클라이언트 모델이 "로컬 파일을 다뤄도 되나"를 추측하지 않도록 서버 성격을
# initialize 단계에서 사실대로 알려준다. 없으면 모르는 쪽으로 기울어 오거절한다.
_INSTRUCTIONS = """yupack은 사용자 본인의 컴퓨터에서 도는 완전 로컬 MCP 서버입니다.
노드·엣지·근거를 옵시디언 노트팩으로 펼치는 개인용 공방입니다.

데이터 취급:
- 쓰기는 로컬 팩 버퍼와 로컬 SQLite에만 갑니다. 외부 프로덕션 DB에는 쓰지 않습니다(읽기 전용 조회만).
- API 키가 없는 기본 모드에서는 노트 본문을 외부 API로 보내지 않습니다. 사용자가 OPENAI_API_KEY를
  명시적으로 제공한 선택 모드에서만 임베딩·자동추출 요청이 OpenAI로 전송될 수 있습니다.

온톨로지 그래머(중요 — 위반은 입력 시점에 거부됩니다):
- 노드 타입과 관계는 ontology_manifest의 그래머로 강제됩니다. 선언되지 않은 노드 타입,
  공간이 틀린 타입(예: evidence 공간의 Claim), 선언되지 않은 공간 쌍의 관계는 거부됩니다.
- 도메인 전용 명사(예: 손실회피, 넛지)나 새 관계가 필요하면 **schema_declare로 먼저 선언**한
  뒤 사용하세요. 미리 정의된 묶음은 schema_pack_install(saas/biomedical/legal/finance)로 설치합니다.
- ontology_ingest의 source_id는 출처 표시입니다. 텍스트 노드 ID로 쓰이지 않으며(자동 text: ID 발급),
  출처 노드가 있으면 contains 엣지가 자동 연결됩니다.
- 저장 전 pack_qa로 팩 전체를 검사하세요. pack_save는 노트팩 폴더(노트+MOC+quality/ledger.json+
  quality/pack-contract.json)를 만듭니다.
- 저장된 팩을 이어서 편집하려면 경로(zip 또는 note-pack 폴더)를
  pack_open_local(path, mode="authoring")으로 여세요.
  반환된 authoring_pack 이름으로 add_node/pack_qa/pack_save가 이어집니다.
  (읽기 핸들 pack_xxxx는 질의 전용이며 저작 버퍼가 아닙니다.)
- 말뭉치가 외국어(영어 원문 등)인 팩은 핵심 노드 properties에 label_ko(한국어 이름)와
  aliases_ko(별칭 목록)를 달아 두세요. 검색 인덱스에 포함되어 한국어 질의가 직접 걸립니다.
  저장 후에는 embeddings count가 0이 아닌지 확인하세요 (0이면 의미 질의 비활성).
- 도구 구분: 열린 팩 질의는 pack_ask_local(3중 검색 + graph_path), ontology_query는
  저작 버퍼 전용입니다. read_only 핸들에 ontology_query를 쓰면 그래프가 비어 나옵니다.
  다른 MCP 서버(유크라테스 엔진)의 query_bm25 등은 팩과 무관하니 팩 질문에 쓰지 마세요.
- 팩 경로를 모르거나 열기에 실패하면 pack_list_local()로 팩 서랍(홈에서 자동 탐색)을
  스캔하세요. verified_final_packs만 정본 후보이며, improved/request/draft ZIP은 자동 선택하지 않습니다.
- 환경변수 YUPACK_AUTO_OPEN에 팩 경로(zip/노트팩 폴더) 또는 "library"가 있으면 서버가 시작할 때 엽니다.
  library에 정본이 여러 개면 작품을 추측해 열지 않고 선택 후보를 돌려줍니다.
- 팩 서랍의 PACK-CHARTER.md(팩 생산 헌장)는 저작 전에 한 번 읽으세요.
- 임베딩·색인은 qmd가 볼트 차원에서 자동 수행합니다(유팩은 qmd를 조작하지 않음).
  Yupack의 기본 질의 백엔드는 로컬 OMLX bge-m3(1024차원)이며 API 키가 필요 없습니다.
  OMLX가 꺼져 있으면 어휘+그래프로만 동작한다고 명시합니다.
- 무키 저작: 원문을 읽고 구조화하는 저작자는 이 도구를 호출한 ChatGPT/Claude다. 유팩은
  원문 Evidence를 보존하고, 호스트가 추가한 Claim·Kinetic·관계를 그래머와 근거 연결로
  검증한다. `pack_authoring_complete`가 통과하기 전에는 노트팩을 저장하지 않는다. OMLX가
  원문을 대신 읽어 경량 노드를 자동 생성하지 않는다.
- 답변 규율: 팩 근거로 말하는 문장에는 근거 id를 표기하고, 팩에 없는 배경지식으로 보충할 때는
  그 부분이 '일반 지식'임을 구분해 밝히세요. 팩 근거와 일반 지식을 한 문장에 섞지 마세요.
  팩이 no_local_evidence를 반환하면 "팩에는 근거가 없다"부터 말한 뒤에만 일반 지식으로 답하세요.
- 보고 축 생략: 근거 등급·조건·한계·검수 상태 같은 축은 팩에 값이 있을 때만 표기하세요.
  비어 있으면 '미기재/없음' 꼬리표를 붙이지 말고 그 축 자체를 생략합니다 (연구 팩 양식을
  문학·독서 팩에 강요하지 마세요).
- 대화 이어가기: 답변 끝에 응답의 graph_path·인접 노드에서 자연스럽게 이어갈 질문을
  1~2개 제안하세요 (예: "침대의 비밀을 아는 사람이 한 명 더 있는데, 누구인지 물어볼까요?").
  단, 사용자가 짧은 사실 확인만 원할 때는 생략합니다.
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
# 팩을 만들기 전에 사용자가 명시한 저장 서랍. 경로는 이 컴퓨터의 선택값이지
# 플러그인 정의나 소스 코드의 하드코딩 값이 아니다.
PACK_DESTINATIONS: dict[str, str] = {}


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


def _begin_host_authoring(buf: dict, source_id: str) -> dict:
    """원문 묶음은 호스트 저작 완료 전 저장할 수 없도록 상태를 기록한다."""
    state = buf.setdefault("authoring", {"required": True, "complete": False, "source_ids": []})
    state["required"] = True
    state["complete"] = False
    if source_id not in state.setdefault("source_ids", []):
        state["source_ids"].append(source_id)
    return state


_load_packs()


def _compute_source_revision() -> str:
    """소스 지문. import 시점에 한 번만 계산한다 — 낡은 프로세스가 디스크의 새 소스를
    읽고 최신인 척하는 것을 막는다 (KINGCRAB protocol.py 리비전 지문 패턴 이식)."""
    root = Path(__file__).resolve().parent
    parts = []
    for path in sorted(root.glob("*.py")):
        try:
            src = path.read_bytes()
        except OSError:
            continue
        parts.append(path.name.encode() + b":" + hashlib.sha256(src).digest())
    return hashlib.sha256(b"|".join(parts)).hexdigest()[:16]


SOURCE_REVISION = _compute_source_revision()


def _read_only_block() -> dict | None:
    """YUPACK_READ_ONLY=1 이면 저작·변경 도구를 잠근다 (전용 질의 플러그인 배포용)."""
    if os.environ.get("YUPACK_READ_ONLY") == "1":
        return {"error": "이 플러그인은 읽기 전용(질의 전용)입니다. 저작은 일반 yupack 플러그인에서 하세요."}
    return None


# 그래머 판정의 정본은 yucrates_contract(유크라테스 이식본) 하나다. 아래는 그 얇은 호출부.
def _space_of(node_type: str) -> str | None:
    return yucrates_contract.space_for_node_type(node_type)


def _allowed_types_for(buf: dict, space: str) -> list[str]:
    """공간별 허용 노드 타입 = 기본 그래머 + 설치 스키마팩(공간 자유) + 팩 커스텀 선언."""
    return yucrates_contract.allowed_node_types(space, local_authoring._extra_types(buf))


def _edge_relations(buf: dict, from_space: str, to_space: str) -> list[str] | None:
    """공간 쌍의 허용 관계(기본 meta_edges + 팩 커스텀 선언). 쌍 자체가 미선언이면 None."""
    return yucrates_contract.get_allowed_relations(from_space, to_space,
                                                   local_authoring._extra_relations(buf))


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
    _ro = _read_only_block()
    if _ro:
        return _ro
    if name not in MANIFEST["schema_packs"]:
        return {"error": f"알 수 없는 스키마팩: {name}. 사용 가능: {list(MANIFEST['schema_packs'])}"}
    buf = _get_pack(pack)
    if name not in buf["schema_packs"]:
        buf["schema_packs"].append(name)
    return {"installed": name, "node_types": MANIFEST["schema_packs"][name]}


@mcp.tool()
def schema_pack_uninstall(name: str, pack: str = DEFAULT_PACK) -> dict:
    """현재 팩 버퍼에서 스키마팩을 제거한다 (노드는 남고, 타입 확장만 해제)."""
    _ro = _read_only_block()
    if _ro:
        return _ro
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
    _ro = _read_only_block()
    if _ro:
        return _ro
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
    엣지 공간 불일치, 비선언 관계, 고아 노드. pack_save 결과에는 qa_status·qa_issues
    요약이 실리고 위반 시 qa_warning이 붙는다.
    """
    return _qa_scan(pack)


@mcp.tool()
def ontology_extract(text: str, pack: str = DEFAULT_PACK, max_nodes: int = 8,
                     source_id: str | None = None) -> dict:
    """호스트 저작자에게 원문 Evidence를 돌려준다. 자동 추출·외부 API 호출은 하지 않는다.

    ChatGPT/Claude가 반환된 원문만 근거로 ontology_add_node/ontology_add_edge를 호출한다.
    source_id를 주면 기존 Evidence를 사용하고, 없으면 새 Evidence를 만든다.
    """
    _ro = _read_only_block()
    if _ro:
        return _ro
    buf = _get_pack(pack)
    if source_id is None:
        source_id = f"src:extract-{secrets.token_hex(4)}"
        buf["nodes"][source_id] = {
            "space": "evidence", "node_type": "TextUnit",
            "properties": {"label": source_id, "text": text[:12000], "source_id": source_id},
        }
    elif source_id not in buf["nodes"]:
        return {"error": f"source_id 근거 노드가 팩에 없습니다: {source_id}"}
    _begin_host_authoring(buf, source_id)
    return {
        "status": "needs_host_extraction", "pack": pack, "source_id": source_id,
        "source_text": text[:6000], "max_nodes": max_nodes,
        "agent_instruction": (
            "당신이 이 원문을 읽는 저작자다. 원문에 있는 내용만 Claim·Concept·Kinetic으로 "
            "구조화하라. Claim/Kinetic에는 evidence_refs=[source_id]를 넣고, Evidence→Claim은 "
            "supports, Evidence→Kinetic은 records로 연결하라. 인과는 원문이 명시할 때만 넣는다. "
            "모든 원문을 처리한 뒤 pack_authoring_complete를 호출해야만 저장할 수 있다."
        ),
        "stores": {"buffer": "ok", "sqlite": _persist(pack)},
    }


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
    _ro = _read_only_block()
    if _ro:
        return _ro
    try:
        return local_authoring.builder_for(pack).add_node(space, node_type, node_id, properties)
    except local_authoring.AuthoringError as exc:
        return {"error": str(exc), **exc.details}


@mcp.tool()
def ontology_add_edge(from_space: str, from_id: str, relation: str, to_space: str,
                       to_id: str, properties: dict | None = None,
                       pack: str = DEFAULT_PACK) -> dict:
    """엣지 하나를 팩 버퍼에 추가한다. 그래머(끝점 실존·실제 공간·허용 관계)를 강제한다.

    끝점 노드가 팩에 먼저 있어야 하고, 선언된 공간 쌍·관계만 허용된다.
    같은 (from, relation, to)를 다시 넣으면 쌓지 않고 속성만 갱신한다(created=false).
    새 관계가 필요하면 schema_declare(relations=[...])로 먼저 선언한다.
    """
    _ro = _read_only_block()
    if _ro:
        return _ro
    try:
        return local_authoring.builder_for(pack).add_edge(
            from_space, from_id, relation, to_space, to_id, properties)
    except local_authoring.AuthoringError as exc:
        return {"error": str(exc), **exc.details}


@mcp.tool()
def authoring_register_candidate(space: str, node_type: str, node_id: str,
                                 properties: dict | None = None,
                                 confidence: float | None = None,
                                 source_id: str | None = None,
                                 pack: str = DEFAULT_PACK) -> dict:
    """저작 버퍼에 승격 후보(status=candidate)를 등록한다.

    후보는 승격 전까지 완성 노트팩에 들어가지 않는다 (pack_save가 needs_promotion으로 막는다).
    열린 완성 노트팩의 overlay 거버넌스(promotion_register_candidate)와 다른 경로다:
    이 도구는 저작 중인 draft 버퍼만 건드린다.
    """
    _ro = _read_only_block()
    if _ro:
        return _ro
    try:
        return local_authoring.engine_for(pack).register_candidate(
            space, node_type, node_id, properties, confidence, source_id)
    except local_authoring.AuthoringError as exc:
        return {"error": str(exc), **exc.details}


@mcp.tool()
def authoring_validate_candidate(node_id: str, pack: str = DEFAULT_PACK,
                                 validator_id: str | None = None,
                                 note: str | None = None) -> dict:
    """후보를 검수 완료(status=validated)로 표시한다. 아직 저장 가능은 아니다."""
    _ro = _read_only_block()
    if _ro:
        return _ro
    try:
        return local_authoring.engine_for(pack).validate_candidate(node_id, validator_id, note)
    except local_authoring.AuthoringError as exc:
        return {"error": str(exc), **exc.details}


@mcp.tool()
def authoring_promote(node_id: str, pack: str = DEFAULT_PACK,
                      promoted_by: str | None = None,
                      evidence_ids: list[str] | None = None) -> dict:
    """후보를 승격(status=promoted)하고 Evidence를 근거 관계로 투영한다.

    evidence_ids는 evidence_refs에 기록되고, Claim에는 supports, Kinetic에는 records
    엣지로 연결된다. 승격된 노드만 완성 노트팩에 들어간다.
    """
    _ro = _read_only_block()
    if _ro:
        return _ro
    try:
        return local_authoring.engine_for(pack).promote(node_id, promoted_by, evidence_ids)
    except local_authoring.AuthoringError as exc:
        return {"error": str(exc), **exc.details}


@mcp.tool()
def authoring_reject(node_id: str, pack: str = DEFAULT_PACK,
                     rejected_by: str | None = None, reason: str | None = None) -> dict:
    """후보를 거부한다. 노드와 그 엣지는 버퍼에서 빠지고 draft 감사 기록에만 남는다."""
    _ro = _read_only_block()
    if _ro:
        return _ro
    try:
        return local_authoring.engine_for(pack).reject(node_id, rejected_by, reason)
    except local_authoring.AuthoringError as exc:
        return {"error": str(exc), **exc.details}


@mcp.tool()
def ontology_ingest(text: str, source_id: str | None = None, pack: str = DEFAULT_PACK) -> dict:
    """텍스트를 evidence 공간의 TextUnit 노드로 버퍼에 저장한다 (DB 쓰기 없음).

    source_id는 출처 표시(provenance)다: TextUnit의 ID로 쓰이지 않고 속성에 기록되며,
    해당 출처 노드가 resource 공간에 있으면 contains 엣지가 자동 연결된다.
    (과거에는 source_id를 노드 ID로 써서 원본 Source 노드를 덮어쓰는 버그가 있었다.)
    """
    _ro = _read_only_block()
    if _ro:
        return _ro
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
    _ro = _read_only_block()
    if _ro:
        return _ro
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


# pack_build_queryable MCP 도구는 제거됨 (오너 결정 2026-08-13): 입력을 만들던
# 구 pack_save가 사라져 회색지대였다. 엔진 함수 local_pack.build_queryable은
# T4 봉인 게이트 내장·테스트 픽스처용으로 내부 유지 — 필요 시 git 이력에서 복원.


def _pack_contract(nodes: dict, edges: list[dict]) -> dict:
    """노트팩에 기록할 공간-타입·공간-관계-공간 계약을 만든다."""
    node_types: dict[str, set[str]] = {}
    for node in nodes.values():
        space, node_type = node.get("space"), node.get("node_type")
        if space and node_type:
            node_types.setdefault(space, set()).add(node_type)
    triples: dict[tuple[str, str, str], int] = {}
    for edge in edges:
        from_id = edge.get("from_id") or edge.get("source")
        to_id = edge.get("to_id") or edge.get("target")
        from_space = edge.get("from_space") or (nodes.get(from_id) or {}).get("space")
        to_space = edge.get("to_space") or (nodes.get(to_id) or {}).get("space")
        relation = edge.get("relation")
        if from_space and relation and to_space:
            key = (from_space, relation, to_space)
            triples[key] = triples.get(key, 0) + 1
    return {
        "format": "yupack-pack-contract-v1",
        "generated": datetime.date.today().isoformat(),
        "node_types": {space: sorted(types) for space, types in sorted(node_types.items())},
        "edge_triples": [[fs, rel, ts, count]
                         for (fs, rel, ts), count in sorted(triples.items())],
    }


def _hydrate_from_zip(zip_path: str, pack: str | None = None) -> dict:
    """저장된 zip 또는 note-pack 폴더를 정본(nodes/edges + 커스텀 그래머)에서 저작 버퍼로 복원한다."""
    path = os.path.expanduser(zip_path)
    if os.path.isdir(path):
        from . import local_pack
        lp = local_pack.LocalPack(path)
        base = os.path.basename(os.path.normpath(path))
        parent = os.path.basename(os.path.dirname(os.path.normpath(path)))
        pack = pack or (parent if base == "note-pack" and parent else lp.pack_id)
        buf = _get_pack(pack)
        if buf["nodes"]:
            return {"error": f"저작 버퍼 '{pack}'에 이미 노드 {len(buf['nodes'])}개가 있습니다. "
                            "덮어쓰지 않습니다. pack 파라미터로 다른 이름을 지정하세요."}
        for nid, node in lp.nodes.items():
            buf["nodes"][nid] = {
                "space": node.get("space"), "node_type": node.get("node_type"),
                "properties": dict(node.get("properties") or {}),
            }
        for source, relations in lp.adj.items():
            from_space = (lp.nodes.get(source) or {}).get("space")
            for relation, target in relations:
                if str(relation).startswith("~"):
                    continue
                to_space = (lp.nodes.get(target) or {}).get("space")
                buf["edges"].append({"from_space": from_space, "from_id": source,
                                     "relation": relation, "to_space": to_space,
                                     "to_id": target, "properties": {}})
        _persist(pack)
        return {"authoring_pack": pack,
                "counts": {"nodes": len(buf["nodes"]), "edges": len(buf["edges"])},
                "custom_grammar": {"node_types": buf.get("custom_types", {}),
                                    "relations": buf.get("custom_relations", [])},
                "schema_packs": buf["schema_packs"]}

    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    meta: dict[str, str] = {}
    if "pack.yaml" in names:
        for ln in z.read("pack.yaml").decode("utf-8", errors="ignore").splitlines():
            if ": " in ln:
                k, v = ln.split(": ", 1)
                meta[k.strip()] = v.strip()
    pack = pack or meta.get("title", "").strip('"') or \
        os.path.splitext(os.path.basename(path))[0]
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


_AUTO_OPENED: dict = {}


_NON_FINAL_PACK_MARKERS = ("archive", "improved", "request", "draft", "test", "retrofit")


def _settings_path() -> Path:
    """사용자가 고른 기본 팩만 로컬 설정에 보관한다.

    플러그인/공용 config.toml을 수정하지 않는다. 환경변수는 테스트 또는 이식 환경에서
    별도 설정 파일을 지정할 때만 쓴다.
    """
    raw = os.environ.get("YUPACK_SETTINGS")
    return Path(os.path.expanduser(raw)) if raw else Path.home() / ".yupack" / "settings.json"


def _load_settings() -> dict:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _update_settings(**updates) -> dict:
    """부분 갱신 (기존 키 보존). 한 키 저장이 다른 키를 지우면 안 된다."""
    data = {**_load_settings(), **updates}
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def _default_pack_path() -> str | None:
    try:
        path = _load_settings().get("default_pack")
        # zip 파일(이행기 하위호환) 또는 노트팩 폴더 둘 다 기본 팩이 될 수 있다
        if isinstance(path, str) and os.path.exists(os.path.expanduser(path)):
            return os.path.expanduser(path)
    except TypeError:
        pass
    return None


def _save_default_pack_path(zip_path: str) -> None:
    _update_settings(default_pack=zip_path)


def _embed_backend_options() -> list[dict]:
    """이 컴퓨터에서 지금 실제로 응답하는 임베딩 백엔드만 선택지로 만든다.

    죽어 있는 백엔드는 나열하지 않는다 (OMLX 설치 권유도 하지 않는다 —
    "서버 없는 로컬" 배포 약속과 충돌한다). 어휘+그래프는 항상 마지막 선택지다.
    """
    from . import local_pack
    opts = []
    if local_pack.omlx_alive():
        opts.append({"embed_model": "bge-m3", "dimension": 1024,
                     "note": "로컬 OMLX 서버가 지금 응답함 (제작·고급 환경 권장)"})
    if local_pack._qmd_available():
        opts.append({"embed_model": "qmd", "dimension": 768,
                     "note": "qmd 설치됨 — 키·비용 없는 로컬 임베딩 (수강 환경 권장)"})
    opts.append({"embed_model": "none", "dimension": 0,
                 "note": "벡터 없이 어휘+그래프 검색만 (추가 설치 0)"})
    return opts


def _setup_gate() -> dict | None:
    """작업 진입 게이트: 미설정 → 인터뷰, 저장 백엔드 사망 → 재질문, 오늘 첫 작업 → 확인.

    조용한 백엔드 전환은 하지 않는다 — 어긋나면 반드시 사용자에게 묻는다.
    통과하면 None. 안정화 기간에는 하루 첫 작업마다 확인한다 (confirm_interval=never로 해제).
    """
    from . import local_pack
    s = _load_settings()
    env_model = os.environ.get("YUPACK_EMBED_MODEL")
    env_dir = os.environ.get("YUPACK_PACK_DIR")
    model = env_model or s.get("embed_model")
    pack_dir = s.get("pack_dir") or env_dir

    if not model or not pack_dir:
        return {
            "status": "needs_setup",
            "ask_user": ("유팩 첫 설정이 필요합니다. 아래 두 가지를 사용자에게 그대로 물어보세요. "
                         "① 팩(note-pack)을 보관할 서가 폴더 (folder_candidates 중 선택 또는 직접 입력) "
                         "② 임베딩 방식 (embed_options 중 선택 — 이 컴퓨터에서 지금 되는 것만 나열함)"),
            "folder_candidates": _discover_pack_dirs(),
            "embed_options": _embed_backend_options(),
            "next": "답을 받으면 pack_configure(pack_dir=..., embed_model=...)로 저장하세요. "
                    "값을 추측하거나 질문을 건너뛰지 마세요.",
        }

    if model == "bge-m3" and not local_pack.omlx_alive():
        return {
            "status": "saved_backend_unreachable", "saved_model": "bge-m3",
            "ask_user": ("저장된 임베딩 백엔드(bge-m3, 로컬 OMLX)가 지금 응답하지 않습니다. "
                         "아래 embed_options 중 어떤 방식으로 진행할지 사용자에게 물어보세요."),
            "embed_options": _embed_backend_options(),
            "next": "조용히 다른 백엔드로 바꾸지 않습니다. 사용자가 고른 값을 "
                    "pack_configure(embed_model=...)로 저장하세요. OMLX를 다시 켰다면 그대로 재시도하면 됩니다.",
        }
    if model == "qmd" and not local_pack._qmd_available():
        return {
            "status": "saved_backend_unreachable", "saved_model": "qmd",
            "ask_user": "저장된 임베딩 백엔드(qmd)가 이 컴퓨터에 없습니다. 어떤 방식으로 진행할지 물어보세요.",
            "embed_options": _embed_backend_options(),
            "next": "사용자가 고른 값을 pack_configure(embed_model=...)로 저장하세요.",
        }

    if env_model and env_dir and not s.get("embed_model"):
        return None  # env로 완전 고정된 배포·테스트 환경은 일일 확인 대상이 아니다

    today = datetime.date.today().isoformat()
    if s.get("confirm_interval", "daily") == "daily" and s.get("last_confirmed") != today:
        dim = {"bge-m3": 1024, "qmd": 768, "none": 0}.get(model)
        return {
            "status": "needs_daily_confirm",
            "current": {"pack_dir": pack_dir, "embed_model": model, "dimension": dim},
            "ask_user": (f"오늘 첫 유팩 작업입니다. 현재 설정 — 팩 폴더: {pack_dir} · "
                         f"임베딩: {model}. 이대로 진행할까요? (안정화 기간 동안 하루 한 번 확인합니다)"),
            "next": "사용자가 예라고 하면 pack_configure(confirm=True), 바꾸겠다고 하면 바꿀 값만 담아 "
                    "pack_configure(...)를 호출하세요. 사용자에게 묻지 않고 확인 처리하지 마세요.",
        }
    return None


def _is_final_pack_path(path: str) -> bool:
    """정본 후보 이름만 통과시킨다. 수정시각은 정본성의 증거가 아니다."""
    name = Path(path).stem.lower()
    return "final" in name and not any(marker in name for marker in _NON_FINAL_PACK_MARKERS)


def _zip_integrity_ok(path: str) -> bool:
    """ZIP 내부 integrity.json의 파일 해시가 모두 맞는 정본만 자동 선택한다."""
    try:
        with zipfile.ZipFile(path) as z:
            names = {n for n in z.namelist() if not n.startswith("__MACOSX/") and "/._" not in n}
            integrity_paths = [n for n in names if n.endswith("integrity.json")]
            for integrity_path in integrity_paths:
                root = integrity_path[: -len("integrity.json")]
                integrity = json.loads(z.read(integrity_path))
                hashes = integrity.get("sha256") or {}
                if not hashes:
                    continue
                if all(
                    rel == "runtime/ontology.sqlite" or
                    (root + rel in names and hashlib.sha256(z.read(root + rel)).hexdigest() == want)
                    for rel, want in hashes.items()
                ):
                    return True
    except (OSError, zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError):
        return False
    return False


def _verified_final_pack_paths(directory: str) -> list[str]:
    """검증된 final ZIP과 MOC가 있는 note-pack 폴더를 수정시각 내림차순으로 돌려준다."""
    import glob as _glob
    paths = [
        path for path in _glob.glob(os.path.join(directory, "**", "*.zip"), recursive=True)
        if "_archive" not in path and os.path.isfile(path)
        and _is_final_pack_path(path) and _zip_integrity_ok(path)
    ]
    paths += [
        path for path in _glob.glob(os.path.join(directory, "**", "note-pack"), recursive=True)
        if os.path.isdir(path)
        and _glob.glob(os.path.join(path, "*-MOC.md"))
    ]
    return sorted(set(paths), key=lambda path: (os.path.getmtime(path), path), reverse=True)


def _open_verified_final_library() -> dict:
    """정본 하나면 열고, 여러 작품이면 잘못 고르지 않고 선택 정보를 돌려준다."""
    global _AUTO_OPENED
    configured = _load_settings().get("pack_dir") or os.environ.get("YUPACK_PACK_DIR")
    if configured and os.path.isdir(os.path.expanduser(configured)):
        candidates = [os.path.expanduser(configured)]
    else:
        candidates = _discover_pack_dirs()
    if not candidates:
        _AUTO_OPENED = {"error": "정본 팩 서랍을 자동 탐색하지 못했습니다."}
        return _AUTO_OPENED
    if len(candidates) > 1:
        _AUTO_OPENED = {"error": "정본 팩 서랍 후보가 여러 개입니다.", "candidates": candidates}
        return _AUTO_OPENED
    paths = _verified_final_pack_paths(candidates[0])
    if not paths:
        _AUTO_OPENED = {"error": "검증된 final 팩이 없습니다.", "directory": candidates[0]}
        return _AUTO_OPENED
    if len(paths) > 1:
        choices = [{"title": os.path.basename(os.path.dirname(path)) or os.path.basename(path),
                    "path": path} for path in paths]
        _AUTO_OPENED = {
            "status": "pack_selection_needed",
            "directory": candidates[0],
            "verified_final_packs": paths,
            "choices": choices,
            "ask_user": "정본 팩이 여러 개입니다. 어느 작품 팩을 열까요? 한 번 고르면 이 컴퓨터의 기본 팩으로 등록할 수 있습니다.",
            "hint": "선택한 path를 pack_open_local로 열고 다시 질문하세요. 추측으로 다른 작품 팩을 열지 않습니다.",
        }
        return _AUTO_OPENED
    from . import local_pack
    result = local_pack.open_local(paths[0], "read_only")
    _AUTO_OPENED = {"mode": "verified_final", "directory": candidates[0],
                    "pack_handle": result["pack_handle"], "path": paths[0]}
    return _AUTO_OPENED


def _auto_open_on_start():
    """명시 환경변수 또는 사용자가 고른 로컬 기본 팩을 자동으로 연다."""
    global _AUTO_OPENED
    target = os.environ.get("YUPACK_AUTO_OPEN") or _default_pack_path()
    if not target or _AUTO_OPENED:
        return
    try:
        if target in {"latest", "library"}:
            _open_verified_final_library()
            return
        from . import local_pack
        r = local_pack.open_local(os.path.expanduser(target), "read_only")
        _AUTO_OPENED = {"pack_handle": r["pack_handle"], "path": target}
    except Exception as e:
        _AUTO_OPENED = {"error": f"자동 오픈 실패: {e}"}


@mcp.tool()
def auto_open_status() -> dict:
    """자동 오픈된 정본 팩 라이브러리의 핸들·경로를 반환한다."""
    _auto_open_on_start()
    return _AUTO_OPENED or {"status": "아직 열린 팩이 없습니다. 첫 질의 때 정본 라이브러리를 자동으로 엽니다."}


def _discover_pack_dirs() -> list[str]:
    """팩 서랍 후보를 사용자 홈에서 자동 탐색한다.

    경로는 사람마다 다르다. 특정인의 볼트 경로를 코드에 박지 않고,
    흔한 위치에서 팩이 실제로 들어 있는 디렉토리만 골라 돌려준다.
    """
    import glob as _glob
    home = os.path.expanduser("~")
    seeds = [
        os.path.join(home, "*", "70_Ontology"),
        os.path.join(home, "*", "*", "70_Ontology"),
        os.path.join(home, "Documents", "*", "70_Ontology"),
        os.path.join(home, "Desktop", "*", "70_Ontology"),
        # iCloud Obsidian
        os.path.join(home, "Library", "Mobile Documents", "iCloud~md~obsidian",
                     "Documents", "*", "70_Ontology"),
        # iCloud Drive(CloudDocs) 아래 볼트 (실사용 보고: Learning Core/70_Ontology)
        os.path.join(home, "Library", "Mobile Documents", "com~apple~CloudDocs",
                     "*", "70_Ontology"),
        # 볼트 이름을 안 쓰는 사람들: 홈 아래 yupack 전용 서랍
        os.path.join(home, "yupack-packs"),
        os.path.join(home, ".local", "share", "yupack", "packs"),
    ]
    found = []
    for pat in seeds:
        for d in _glob.glob(pat):
            if os.path.isdir(d) and d not in found:
                if (_glob.glob(os.path.join(d, "**", "*.zip"), recursive=True)
                        or _glob.glob(os.path.join(d, "**", "note-pack", "*-MOC.md"), recursive=True)):
                    found.append(d)
    return found


@mcp.tool()
def pack_list_local(directory: str = "") -> dict:
    """팩 서랍을 스캔해 사용 가능한 zip·노트팩 목록을 반환한다 (최신순).

    directory 생략 시 YUPACK_PACK_DIR, 그것도 없으면 홈에서 자동 탐색한다.
    설정이 없어도 동작하며, 못 찾으면 사용자에게 물을 문구를 돌려준다.
    경로를 모를 때는 이 도구부터 호출하면 된다.
    """
    gate = _setup_gate()
    if gate:
        return gate
    import glob as _glob
    d = directory or _load_settings().get("pack_dir") or os.environ.get("YUPACK_PACK_DIR") or ""
    if d:
        d = os.path.expanduser(d)
    else:
        cands = _discover_pack_dirs()
        if not cands:
            return {"packs": [], "searched": "홈 디렉토리 자동 탐색",
                    "ask_user": "팩이 들어 있는 폴더 경로를 알려주세요. "
                                "(예: 옵시디언 볼트의 70_Ontology 폴더)",
                    "hint": "경로를 받으면 pack_list_local(directory=\"<경로>\")로 다시 부르세요."}
        if len(cands) > 1:
            return {"candidates": cands, "packs": [],
                    "ask_user": "팩 서랍 후보가 여러 개입니다. 어느 것을 쓸까요?",
                    "hint": "고른 경로로 pack_list_local(directory=\"<경로>\")를 부르세요."}
        d = cands[0]
    if not os.path.isdir(d):
        return {"error": f"디렉토리가 없습니다: {d}", "packs": [],
                "ask_user": "팩이 들어 있는 폴더 경로를 알려주세요."}
    packs = []
    for f in _glob.glob(os.path.join(d, "**", "*.zip"), recursive=True):
        if "_archive" in f or not os.path.isfile(f):
            continue
        st = os.stat(f)
        packs.append({"path": f, "type": "zip", "size_mb": round(st.st_size / 1e6, 1),
                      "modified": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    for f in _glob.glob(os.path.join(d, "**", "note-pack"), recursive=True):
        if not os.path.isdir(f) or not _glob.glob(os.path.join(f, "*-MOC.md")):
            continue
        st = os.stat(f)
        packs.append({"path": f, "type": "notepack",
                      "modified": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    packs.sort(key=lambda x: x["modified"], reverse=True)
    return {"directory": d, "count": len(packs), "packs": packs,
            "verified_final_packs": _verified_final_pack_paths(d),
            "hint": "정본은 verified_final_packs에만 들어갑니다(zip·note-pack). 첫 pack_ask_local 질의는 이 라이브러리를 자동으로 엽니다."}


@mcp.tool()
def pack_open_local(zip_path: str, mode: str = "read_only") -> dict:
    """로컬 zip 또는 note-pack 폴더를 열고 pack_handle을 반환한다.

    mode="read_only"(기본): 질의 전용 핸들만 연다.
    mode="authoring": 핸들과 함께 팩의 정본을 저작 버퍼로 복원한다. 이후
      ontology_add_node/pack_qa/pack_save를 반환된 authoring_pack 이름으로 호출하면
      이어서 편집·검사·재저장할 수 있다. (핸들 id는 저작 버퍼가 아니다 - 혼용 금지)
    """
    gate = _setup_gate()
    if gate:
        return gate
    from . import local_pack
    try:
        r = local_pack.open_local(zip_path, "read_only")
    except Exception as e:
        return {"error": f"열기 실패: {e}",
                "hint": "경로가 바뀌었거나 zip·note-pack 폴더가 아닐 수 있습니다. pack_list_local()로 현재 팩 목록을 확인하세요."}
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
    if not pk:
        return {"error": f"핸들 없음: {pack_handle}"}
    return {**pk.status(), "source_revision": SOURCE_REVISION}


@mcp.tool()
def pack_verify_baseline(pack_handle: str, source_path: str = "") -> dict:
    """팩 품질층(quality/*.json)의 자기 신고를 nodes/edges/인덱스에서 재계산해 대조한다.

    신고를 그대로 믿지 않는다: 어긋나면 FAIL(declared_mismatch, 조작 탐지),
    핵심 기준 위반이면 FAIL(below_baseline), 품질층이 없으면 absent(구세대 팩, 오류 아님).
    source_path(원문 텍스트 경로)를 주면 게이트9 quote-in-span(앵커 실재)을 직접
    재계산하고, 없으면 팩 내장 quality/anchor-check.json의 green 여부로 대체한다.
    유팩은 읽기 도구다 — 판정만 하고 팩을 수정하지 않는다."""
    from . import local_pack
    pk = local_pack.get(pack_handle)
    if not pk:
        return {"error": f"핸들 없음: {pack_handle}"}
    return {**pk.verify_baseline(source_path or None), "source_revision": SOURCE_REVISION}


@mcp.tool()
def pack_ask_local(question: str, pack_handle: str = "", top_k: int = 6) -> dict:
    """열린 로컬 팩에 질의한다. lexical+vector+graph 3중 검색, 근거 카드와
    retrieval_trace를 반환하며 근거가 없으면 no_local_evidence를 반환한다.
    pack_handle을 생략하면 열린 팩을 쓰며, 정본이 하나뿐이면 자동으로 연다.
    에이전트 개발·MCP·메모리·프롬프트 인젝션·회귀 질문이면 답하기 전에 먼저 호출할 것."""
    gate = _setup_gate()
    if gate:
        return gate
    from . import local_pack
    if pack_handle:
        pk = local_pack.get(pack_handle)
        if not pk:
            return {"error": f"핸들 없음: {pack_handle}"}
        return pk.ask(question, top_k)
    _auto_open_on_start()
    if not local_pack._HANDLES:
        selection = _open_verified_final_library()
        if selection.get("status") == "pack_selection_needed":
            return selection
    return local_pack.ask_all(question, top_k)


@mcp.tool()
def pack_close(pack_handle: str) -> dict:
    """열린 로컬 팩 핸들을 닫는다."""
    from . import local_pack
    return local_pack.close(pack_handle)


@mcp.tool()
def pack_set_default(zip_path: str) -> dict:
    """이 컴퓨터의 기본 팩을 지정한다.

    사용자가 명시적으로 고른 팩 경로(zip 또는 note-pack 폴더)를 ~/.yupack/settings.json에만 저장한다. 플러그인 정의나
    Codex 공용 config.toml은 수정하지 않는다. 이후 새 작업에서 이 팩만 자동으로 열 수 있다.
    경로를 모르면 사용자에게 물어라 (사람마다 다르다).
    """
    _ro = _read_only_block()
    if _ro:
        return _ro
    zip_path = os.path.expanduser(zip_path)
    if not os.path.exists(zip_path):
        return {"error": f"팩 경로가 없습니다: {zip_path}. 저장한 zip 또는 note-pack 경로를 확인하세요."}
    _save_default_pack_path(zip_path)
    return {"ok": True, "default_pack": zip_path,
            "settings": str(_settings_path()),
            "note": "기본 팩 지정 완료. 다음 새 대화에서 이 팩을 자동으로 열 수 있습니다."}


@mcp.tool()
def pack_configure(pack_dir: str = "", embed_model: str = "", confirm: bool = False,
                   confirm_interval: str = "") -> dict:
    """유팩 사용자 설정을 저장·확인한다 (~/.yupack/settings.json).

    needs_setup·needs_daily_confirm·saved_backend_unreachable 응답을 받으면 사용자에게
    그 질문을 그대로 물은 뒤, 받은 답만 이 도구로 저장한다. 값을 추측하지 않는다.
    confirm=True는 "오늘 이 설정 그대로 진행"의 확인 스탬프다.
    confirm_interval: daily(안정화 기간 기본, 하루 한 번 확인) 또는 never(확인 해제).
    인자 없이 부르면 현재 설정·게이트 상태만 돌려준다.
    """
    from . import local_pack
    updates: dict = {}
    if pack_dir:
        d = os.path.expanduser(pack_dir)
        if not os.path.isdir(d):
            return {"error": f"폴더가 없습니다: {d}",
                    "hint": "실제 존재하는 폴더 경로인지 사용자에게 다시 확인하세요."}
        updates["pack_dir"] = d
    if embed_model:
        if embed_model == "bge-m3" and not local_pack.omlx_alive():
            return {"error": "bge-m3(로컬 OMLX)가 지금 응답하지 않습니다.",
                    "embed_options": _embed_backend_options(),
                    "hint": "지금 되는 선택지 중에서 고르거나, OMLX를 켠 뒤 다시 저장하세요."}
        if embed_model == "qmd" and not local_pack._qmd_available():
            return {"error": "qmd가 이 컴퓨터에 설치돼 있지 않습니다.",
                    "embed_options": _embed_backend_options()}
        if embed_model not in {"bge-m3", "qmd", "none"} and not embed_model.startswith("text-embedding"):
            return {"error": f"모르는 임베딩 방식: {embed_model}",
                    "embed_options": _embed_backend_options()}
        updates["embed_model"] = embed_model
    if confirm_interval:
        if confirm_interval not in {"daily", "never"}:
            return {"error": "confirm_interval은 daily 또는 never만 됩니다."}
        updates["confirm_interval"] = confirm_interval
    if not updates and not confirm:
        return {"settings": _load_settings(), "effective_model": local_pack.EMBED_MODEL,
                "dimension": local_pack.EMBED_DIM, "embed_options": _embed_backend_options(),
                "source_revision": SOURCE_REVISION,
                "gate": _setup_gate() or {"status": "ok"}}
    updates["last_confirmed"] = datetime.date.today().isoformat()
    saved = _update_settings(**updates)
    model, dim = local_pack.refresh_embed_model()
    out = {"ok": True,
           "settings": {k: saved.get(k) for k in
                        ("pack_dir", "embed_model", "confirm_interval", "last_confirmed", "default_pack")},
           "effective_model": model, "dimension": dim,
           "source_revision": SOURCE_REVISION}
    env_model = os.environ.get("YUPACK_EMBED_MODEL")
    if env_model and saved.get("embed_model") and env_model != saved.get("embed_model"):
        out["warning"] = (f"환경변수 YUPACK_EMBED_MODEL={env_model}이 설정을 덮고 있습니다. "
                          "플러그인/환경 정의에서 제거해야 저장값이 적용됩니다.")
    return out


@mcp.tool()
def pack_create(pack: str, save_to: str | None = None) -> dict:
    """새 팩 버퍼를 만든다 (이후 add_node/ingest/extract로 채우고 pack_save로 내보낸다).

    새 팩의 저장 서랍은 반드시 사용자가 먼저 정한다. save_to 없이 호출하면 버퍼나 폴더를
    임의로 만들지 않고 물어볼 문구를 반환한다. 같은 이름이 이미 있으면 오류가 아니라
    현황을 돌려준다 (이어서 작업 가능).
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ro = _read_only_block()
    if _ro:
        return _ro
    if not save_to:
        # 설정 인터뷰에서 사용자가 이미 고른 팩 서랍이 있으면 그것이 기본 저장처다
        save_to = _load_settings().get("pack_dir") or ""
    if not save_to:
        return {
            "status": "needs_save_path",
            "ask_user": "새 팩을 어느 폴더에 저장할까요? 본인 옵시디언 볼트의 팩 폴더 경로를 알려주세요.",
        }
    destination = os.path.expanduser(save_to)
    if pack in PACKS:
        buf = PACKS[pack]
        return {"created": False, "exists": pack,
                "nodes": len(buf["nodes"]), "edges": len(buf["edges"]),
                "custom_types": buf.get("custom_types", {}),
                "save_to": PACK_DESTINATIONS.get(pack, destination),
                "hint": "기존 팩입니다. 그대로 add_node/ingest로 이어서 작업하거나, 다른 이름으로 새로 만드세요."}
    _get_pack(pack)
    PACK_DESTINATIONS[pack] = destination
    return {"created": pack, "save_to": destination,
            "stores": {"buffer": "ok", "sqlite": _persist(pack)}}


@mcp.tool()
def pack_ingest_local_zip(zip_path: str, pack: str = DEFAULT_PACK,
                           max_files: int = 0, max_nodes_per_file: int = 6) -> dict:
    """로컬 md 묶음을 Evidence로 보존하고 호스트 저작 단계를 시작한다.

    이 도구는 원문을 자동 요약·추출하지 않는다. 호출한 ChatGPT/Claude가
    pack_authoring_sources로 원문을 읽고 Claim·Concept·Kinetic·관계를 작성한다.
    pack_authoring_complete 전에는 pack_save가 노트팩 생성을 거절한다.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ro = _read_only_block()
    if _ro:
        return _ro
    zip_path = os.path.expanduser(zip_path)
    if not os.path.exists(zip_path):
        return {"error": f"zip이 없습니다: {zip_path}",
                "hint": "경로가 바뀌었을 수 있습니다. pack_list_local()로 현재 팩 목록을 확인하세요."}
    z = zipfile.ZipFile(zip_path)
    mds = [n for n in z.namelist()
           if n.lower().endswith((".md", ".markdown", ".txt"))
           and "__MACOSX" not in n and not n.endswith("/")]
    if max_files > 0:
        mds = mds[:max_files]
    if not mds:
        return {"error": "zip 안에 md/txt 파일이 없습니다."}
    _get_pack(pack)
    done, errors, source_ids = 0, 0, []
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
            "properties": {"label": os.path.basename(name), "text": text[:12000],
                            "source_locator": name, "source_id": src_id}}
        _begin_host_authoring(buf, src_id)
        source_ids.append(src_id)
        done += 1
    return {
        "status": "needs_host_extraction", "pack": pack,
        "ingested_files": done, "total_files": len(mds), "errors": errors,
        "source_ids": source_ids,
        "stores": {"buffer": "ok", "sqlite": _persist(pack)},
        "next": (
            "pack_authoring_sources(pack=..., cursor=0)로 원문을 읽고, 원문마다 "
            "ontology_add_node/ontology_add_edge로 구조화하세요. Claim 또는 Kinetic은 반드시 "
            "Evidence와 연결하세요. 완료 뒤 pack_authoring_complete를 호출해야 노트팩을 저장할 수 있습니다."
        ),
    }


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
def pack_authoring_sources(pack: str = DEFAULT_PACK, cursor: int = 0,
                           limit: int = 1, max_chars: int = 6000) -> dict:
    """무키 저작용 원문 근거를 한 문서씩 반환한다.

    pack_ingest_local_zip이 needs_host_extraction을 반환한 뒤 사용한다. 호출한 대화 모델은
    여기서 받은 text만 근거로 노드·엣지를 저작해야 하며, cursor/next_cursor로 다음 문서를 읽는다.
    """
    buf = _get_pack(pack)
    rows = []
    for nid, node in buf["nodes"].items():
        if node.get("space") != "evidence":
            continue
        props = node.get("properties") or {}
        if not props.get("source_locator"):
            continue
        rows.append({"source_id": nid, "label": props.get("label"),
                     "locator": props.get("source_locator"), "text": props.get("text", "")})
    rows.sort(key=lambda x: (x["locator"] or "", x["source_id"]))
    cursor = max(0, cursor)
    limit = max(1, min(limit, 3))
    items = rows[cursor:cursor + limit]
    for item in items:
        item["text"] = item["text"][:max(500, min(max_chars, 12000))]
    next_cursor = cursor + len(items)
    return {"pack": pack, "total_sources": len(rows), "cursor": cursor,
            "items": items, "next_cursor": next_cursor if next_cursor < len(rows) else None,
            "agent_instruction": (
                "각 text는 팩 안 원문 근거다. 이 텍스트에서만 개념·사건·주장을 추출해 "
                "ontology_add_node/ontology_add_edge로 넣고, 만든 Claim/Kinetic에는 evidence_refs에 source_id를 남기세요."
            )}


def _promotion_gate(buf: dict, pack: str) -> dict | None:
    """유크라테스 계약: 후보·검수 상태 노드는 완성 노트팩으로 나가지 못한다."""
    unpromoted = local_authoring.unpromoted_node_ids(buf)
    if not unpromoted:
        return None
    return {
        "status": "needs_promotion", "pack": pack, "unpromoted_node_ids": unpromoted,
        "next": ("authoring_promote(node_id=..., evidence_ids=[...])로 승격하거나 "
                 "authoring_reject로 정리한 뒤 다시 호출하세요. candidate·validated 상태 노드는 "
                 "완성 노트팩에 포함되지 않습니다."),
    }


def _authoring_coverage(buf: dict) -> tuple[list[str], list[str]]:
    """원문 Evidence가 Claim 또는 Kinetic에 실제로 투영됐는지 확인한다."""
    state = buf.get("authoring") or {}
    source_ids = list(state.get("source_ids") or [])
    covered: set[str] = set()
    for node in buf["nodes"].values():
        if node.get("space") in {"claim", "kinetic"}:
            covered.update(node.get("properties", {}).get("evidence_refs") or [])
    for edge in buf["edges"]:
        if edge.get("from_space") != "evidence" or edge.get("relation") not in {"supports", "records"}:
            continue
        target = buf["nodes"].get(edge.get("to_id")) or {}
        if target.get("space") in {"claim", "kinetic"}:
            covered.add(edge.get("from_id"))
    return source_ids, sorted(set(source_ids) & covered)


@mcp.tool()
def pack_authoring_complete(pack: str = DEFAULT_PACK) -> dict:
    """호스트 저작의 근거 연결·그래머를 검증하고, 통과한 팩만 저장 가능으로 표시한다."""
    _ro = _read_only_block()
    if _ro:
        return _ro
    buf = _get_pack(pack)
    state = buf.get("authoring")
    if not state or not state.get("required"):
        return {"status": "not_required", "pack": pack,
                "note": "원문 ZIP ingest로 시작한 팩이 아닙니다. pack_quality와 pack_qa를 확인하세요."}
    gate = _promotion_gate(buf, pack)
    if gate:
        state["complete"] = False
        return gate
    quality = _authoring_quality(buf)
    source_ids, covered = _authoring_coverage(buf)
    missing = sorted(set(source_ids) - set(covered))
    qa = _qa_scan(pack)
    counts = quality["counts"]
    if quality["status"] != "pass" or missing or qa["status"] != "pass":
        state["complete"] = False
        return {
            "status": "needs_authoring", "pack": pack, "counts": counts,
            "uncovered_source_ids": missing,
            "qa_status": qa["status"], "qa_issues": qa["counts"]["issues"],
            "next": (
                "각 원문 Evidence를 Claim 또는 Kinetic에 evidence_refs로 연결하고, "
                "Evidence→Claim supports 또는 Evidence→Kinetic records 엣지를 추가하세요. "
                "Kinetic은 원문에 사건·인과가 있을 때만 필요하며, 논증형 팩은 Claim만으로 통과합니다."
            ),
        }
    state["complete"] = True
    return {"status": "ready_to_save", "pack": pack, "counts": counts,
            "covered_source_ids": covered,
            "qa_status": qa["status"],
            "stores": {"buffer": "ok", "sqlite": _persist(pack)}}


def _authoring_quality(buf: dict) -> dict:
    """Evidence만 든 경량 팩을 정본 저장 전에 확실히 막는다.

    Kinetic은 작품 유형에 따라 없을 수 있다(논증서 등). 따라서 kinetic 부재를 답변 거절의
    조건으로 삼지 않는다. 대신 원문 Evidence 외에 하나의 구조 노드도 없는 경우만 실패다.
    """
    counts: dict[str, int] = {space: 0 for space in MANIFEST["spaces"]}
    for node in buf["nodes"].values():
        space = node.get("space", "unknown")
        counts[space] = counts.get(space, 0) + 1
    evidence = counts.get("evidence", 0)
    structured = sum(count for space, count in counts.items() if space != "evidence")
    answer_bearing = counts.get("claim", 0) + counts.get("kinetic", 0)
    return {"status": "pass" if not evidence or answer_bearing else "needs_authoring",
            "counts": counts, "structured_nodes": structured, "answer_bearing_nodes": answer_bearing,
            "note": ("Kinetic은 서사·사건형 작품에서만 품질 지표입니다. 논증형 팩은 Claim과 Evidence만으로도 통과합니다."
                     if answer_bearing else "근거 Evidence만 있거나 Concept 이름만 있습니다. Claim 또는 Kinetic이 없는 이 상태는 경량 초안이며 완성 팩으로 저장할 수 없습니다.")}


# ── 품질 게이트 (2026-08-13 오너 승인 · design-2026-08-13-user-pack-quality-gate) ──
# 11개 정본 팩 전수 실측에서 나온 불변식만 저장을 차단한다. 기준 프로파일은
# 오디세이(서사형)·역사란 무엇인가(논증형) 실측치. 오디세이급 점수는 차단이 아니라
# quality/ledger.json에 기록되는 관측값이다 — 필수만 막고 나머지는 보이는 공백으로.
QUALITY_FLOOR = {"claim": 5, "theme": 3, "evidence": 20, "checks": 5}
NARRATIVE_PROFILE = {"evidence": 239, "claim": 14, "theme": 46, "kinetic": 223,
                     "causal_edges": 21, "aliases_ratio": 0.53}
ARGUMENT_PROFILE = {"evidence": 48, "claim": 96, "theme": 16, "aliases_ratio": 0.21}
DEFAULT_OFF_TOPIC = "비트코인 반감기는 언제이고 가격에 어떤 영향을 주는가?"
_THEME_TYPES = {"Theme", "Topic"}  # Topic이 그래머 정본, Theme은 정본 팩들의 커스텀 선언


def _quality_measure(buf: dict) -> dict:
    from .local_pack import CAUSAL_RELS
    nodes, edges = buf["nodes"], buf["edges"]
    sup_to = {e.get("to_id") for e in edges if e.get("relation") == "supports"}
    claims = [nid for nid, n in nodes.items() if n.get("space") == "claim"]
    claims_linked = [nid for nid in claims
                     if (nodes[nid].get("properties") or {}).get("evidence_refs") or nid in sup_to]
    themes = [nid for nid, n in nodes.items() if n.get("node_type") in _THEME_TYPES]
    themes_linked = [nid for nid in themes
                     if (nodes[nid].get("properties") or {}).get("evidence_refs") or nid in sup_to]
    evid = [nid for nid, n in nodes.items() if n.get("space") == "evidence"]
    loc_missing, loc_invalid = [], []
    for nid in evid:
        p = nodes[nid].get("properties") or {}
        loc = p.get("source_locator") or p.get("locator")
        if not loc:
            loc_missing.append(nid)
        elif isinstance(loc, dict):
            a, b = loc.get("start_line"), loc.get("end_line")
            if a is not None and b is not None and \
                    (not isinstance(a, int) or not isinstance(b, int) or a < 1 or b < a):
                loc_invalid.append(nid)
    concepts = [n for n in nodes.values() if n.get("space") == "concept"]
    alias = sum(1 for n in concepts if (n.get("properties") or {}).get("aliases_ko"))
    return {"claims": len(claims), "claims_linked": len(claims_linked),
            "claims_unlinked": sorted(set(claims) - set(claims_linked)),
            "themes": len(themes), "themes_linked": len(themes_linked),
            "evidence": len(evid),
            "locator_missing_count": len(loc_missing), "locator_missing": loc_missing[:8],
            "locator_invalid": loc_invalid[:8],
            "concepts": len(concepts),
            "aliases_ratio": round(alias / len(concepts), 2) if concepts else 0.0,
            "kinetic": sum(1 for n in nodes.values() if n.get("space") == "kinetic"),
            "causal_edges": sum(1 for e in edges if e.get("relation") in CAUSAL_RELS),
            "edges": len(edges)}


def _work_type(buf: dict, m: dict) -> str:
    declared = (buf.get("quality_checks") or {}).get("work_type")
    if declared:
        return declared
    return "argument" if m["kinetic"] == 0 else "narrative"


def _quality_gate(buf: dict, pack: str) -> dict | None:
    """저장 차단 게이트 G1·G2·G4·G5 + G6 사전조건(검수 질문 등록). 통과 시 None."""
    m = _quality_measure(buf)
    gates = []
    if m["claims"] < QUALITY_FLOOR["claim"] or m["claims_unlinked"]:
        gates.append({"gate": "G1", "need": f"Claim ≥ {QUALITY_FLOOR['claim']} + 전부 근거 연결",
                      "now": f"claims {m['claims']}, 미연결 {len(m['claims_unlinked'])}"})
    if m["locator_missing_count"] or m["locator_invalid"]:
        gates.append({"gate": "G2", "need": "모든 Evidence에 locator + 범위 정합",
                      "now": f"누락 {m['locator_missing_count']}, 불량 {len(m['locator_invalid'])}",
                      "sample": (m["locator_missing"] + m["locator_invalid"])[:4]})
    if m["themes"] < QUALITY_FLOOR["theme"] or m["themes_linked"] < m["themes"]:
        gates.append({"gate": "G4", "need": f"주제(Topic/Theme) ≥ {QUALITY_FLOOR['theme']} + 근거 연결",
                      "now": f"themes {m['themes']}, 연결 {m['themes_linked']}"})
    if m["evidence"] < QUALITY_FLOOR["evidence"]:
        gates.append({"gate": "G5", "need": f"Evidence ≥ {QUALITY_FLOOR['evidence']} (원문 조각화)",
                      "now": f"evidence {m['evidence']}"})
    checks = (buf.get("quality_checks") or {}).get("questions") or []
    if len(checks) < QUALITY_FLOOR["checks"]:
        gates.append({"gate": "G6", "need": f"검수 질문 ≥ {QUALITY_FLOOR['checks']} 등록",
                      "now": f"{len(checks)}개",
                      "hint": "pack_register_checks(questions=[...])로 원문·저작 내용 기반 질문을 등록하세요."})
    if not gates:
        return None
    return {"status": "needs_quality", "pack": pack, "gates": gates, "measure": m,
            "next": ("11개 정본 팩 전수 실측 불변식입니다. 부족한 축을 저작으로 채운 뒤 "
                     "다시 pack_save를 호출하세요. 이 게이트 밑으로는 완성 팩을 저장하지 않습니다.")}


def _heldout_verify(zip_path: str, buf: dict) -> dict:
    """G6 본검사: 만든 노트팩을 다시 열어 검수 질문을 실측한다 ('만든 뒤 열어서 확인')."""
    from . import local_pack
    checks = buf.get("quality_checks") or {}
    results, failures = [], []
    lp = local_pack.LocalPack(zip_path)
    # 저장 게이트는 qmd를 관리하지 않는다. 질의 시점의 qmd 사용은 유지하되,
    # 저장 중 held-out 검사는 lexical+graph만으로 수행한다.
    lp.vector = lambda _question, _k=8: []
    for q in checks.get("questions") or []:
        st = lp.ask(q, 4).get("status")
        results.append({"question": q, "status": st})
        if st != "grounded":
            failures.append(q)
    off = checks.get("off_topic") or DEFAULT_OFF_TOPIC
    off_st = lp.ask(off, 4).get("status")
    results.append({"question": off, "status": off_st, "expect": "no_local_evidence"})
    if off_st != "no_local_evidence":
        failures.append(f"[팩 외부 질문이 거절되지 않음] {off}")
    return {"results": results, "failures": failures}


def _quality_ledger(m: dict, work_type: str, heldout: dict) -> dict:
    profile = ARGUMENT_PROFILE if work_type == "argument" else NARRATIVE_PROFILE
    mkey = {"evidence": "evidence", "claim": "claims", "theme": "themes",
            "kinetic": "kinetic", "causal_edges": "causal_edges", "aliases_ratio": "aliases_ratio"}
    axes = {k: round(min(1.0, (m[mkey[k]] / ref) if ref else 1.0), 2)
            for k, ref in profile.items()}
    score = round(100 * sum(axes.values()) / len(axes))
    gaps = sorted(k for k, v in axes.items() if v < 0.5)
    return {"schema": "yupack.quality-ledger/v1",
            "reference_profile": "what-is-history-carr" if work_type == "argument" else "odyssey-butler",
            "work_type": work_type, "measure": m, "axes": axes,
            "odyssey_class_score": score, "gaps": gaps,
            # 정본 서가 등재·수업 배포 기준 (오너 재조정 가능): 총점 80 + 50% 미만 축 없음
            "canonical_grade": bool(score >= 80 and not gaps),
            "heldout": heldout, "source_revision": SOURCE_REVISION}


@mcp.tool()
def pack_quality(pack: str = DEFAULT_PACK) -> dict:
    """저장 전 저작 품질을 확인한다. Evidence-only 경량 팩은 needs_authoring으로 표시하고,
    저장 게이트(G1~G6) 판정과 오디세이 프로파일 실측치를 함께 돌려준다."""
    buf = _get_pack(pack)
    base = _authoring_quality(buf)
    m = _quality_measure(buf)
    return {**base, "quality_measure": m, "work_type": _work_type(buf, m),
            "save_gate": _quality_gate(buf, pack) or {"status": "pass"}}


@mcp.tool()
def pack_register_checks(questions: list[str], pack: str = DEFAULT_PACK,
                         off_topic: str = "", work_type: str = "") -> dict:
    """저장 게이트 G6용 검수 질문을 등록한다 (5개 이상).

    pack_save가 완성 노트팩을 직접 다시 열어 이 질문들이 전부 grounded인지, 팩 외부
    대조 질문이 no_local_evidence로 거절되는지 실측한 뒤에만 저장한다.
    질문은 임의로 짓지 말고 원문·저작 내용에서 뽑아 사용자와 정한다.
    work_type: narrative(서사형) 또는 argument(논증형) — 품질 원장의 기준 프로파일 분기.
    """
    _ro = _read_only_block()
    if _ro:
        return _ro
    qs = [q.strip() for q in (questions or []) if isinstance(q, str) and q.strip()]
    if len(qs) < QUALITY_FLOOR["checks"]:
        return {"error": f"검수 질문이 부족합니다: {len(qs)}개 (최소 {QUALITY_FLOOR['checks']}개)"}
    if work_type and work_type not in {"narrative", "argument"}:
        return {"error": "work_type은 narrative 또는 argument만 됩니다."}
    buf = _get_pack(pack)
    buf["quality_checks"] = {"questions": qs[:12],
                            "off_topic": off_topic.strip() or DEFAULT_OFF_TOPIC,
                            "work_type": work_type or None}
    return {"ok": True, "pack": pack, "registered": len(qs[:12]),
            "off_topic": buf["quality_checks"]["off_topic"],
            "stores": {"buffer": "ok", "sqlite": _persist(pack)}}


# 본문 섹션으로 렌더하는 필드 — frontmatter 스칼라 승격에서 제외
_NOTE_TEXTY = {"label", "label_ko", "aliases_ko", "text", "statement", "definition",
               "eli5", "mechanism", "interpretive_basis", "svo", "source_quote",
               "extraction_note", "evidence_refs", "locator", "source_path",
               "source_locator", "source_id"}
_NOTE_FOLDER = {"Person": "person", "Deity": "deity", "Collective": "collective",
                "Place": "place", "Object": "object", "Event": "event",
                "Action": "action", "State": "state", "Process": "process",
                "Claim": "claim", "Theme": "theme", "Topic": "theme",
                "TextUnit": "evidence", "Evidence": "evidence", "Source": "source"}


def _write_notepack(buf: dict, pack: str, note_dir: str, today: str) -> dict:
    """버퍼를 옵시디언 노트팩(전 필드 템플릿)으로 펼친다 — 노트가 정본이다."""
    from .local_pack import CAUSAL_RELS
    nodes, edges = buf["nodes"], buf["edges"]

    def label(nid):
        p = nodes[nid].get("properties") or {}
        return str(p.get("label_ko") or p.get("label") or nid).strip()

    def fname(nid):
        s = re.sub(r'[\\/:*?"<>|#^\[\]{}]', "-", label(nid))
        return re.sub(r"\s+", " ", s).strip()[:80] or _safe_filename(nid)

    fmap, seen = {}, {}
    for nid in nodes:
        f = fname(nid)
        seen[f] = seen.get(f, 0) + 1
        fmap[nid] = f if seen[f] == 1 else f"{f}-{seen[f]}"
    out_edges: dict[str, list] = {}
    for e in edges:
        s, t = e.get("from_id"), e.get("to_id")
        if s in nodes and t in nodes:
            out_edges.setdefault(s, []).append((e.get("relation"), t))
    count = 0
    for nid, n in nodes.items():
        p = n.get("properties") or {}
        d = os.path.join(note_dir, _NOTE_FOLDER.get(n.get("node_type"), n.get("space") or "etc"))
        os.makedirs(d, exist_ok=True)
        aliases = p.get("aliases_ko") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        L = ["---", f'title: "{label(nid)}"', "tags:", f"  - 독서팩/{pack}",
             f"  - 프로젝트/{_safe_filename(pack)}-note-pack",
             f"date: {today}", f"type: {n.get('node_type')}", f"space: {n.get('space')}",
             f"node_id: {nid}"]
        if aliases:
            L.append("aliases:")
            L += [f"  - {a}" for a in aliases[:8]]
        for k, v in p.items():
            if k in _NOTE_TEXTY or isinstance(v, (dict, list)) or v in (None, ""):
                continue
            sv = str(v)
            L.append(f"{k}: " + (f'"{sv}"' if re.search(r'[:#\[\]{}"]', sv) else sv))
        L += ["---", f"## {label(nid)}"]
        if p.get("label") and p.get("label_ko") and p["label"] != p["label_ko"]:
            L.append(f"*{p['label']}*")
        L.append("")
        body = p.get("text") or p.get("statement") or ""
        if n.get("space") == "evidence":
            if body:
                L += ["> " + str(body).replace("\n", "\n> "), ""]
            loc = p.get("locator")
            if isinstance(loc, dict) and (loc.get("start_line") or loc.get("line_start")):
                a = loc.get("start_line") or loc.get("line_start")
                b = loc.get("end_line") or loc.get("line_end")
                L.append(f"행:: {a}~{b}")
            elif p.get("source_locator"):
                L.append(f"위치: {p['source_locator']}")
            if p.get("source_path"):
                orig = os.path.splitext(os.path.basename(str(p["source_path"])))[0]
                L.append(f"원문:: [[{orig}]]")
            if p.get("extraction_note"):
                L += ["", f"발췌 노트: {p['extraction_note']}"]
            L.append("")
        else:
            if p.get("definition"):
                L += [str(p["definition"]), ""]
            if body and body != p.get("definition"):
                L += [str(body), ""]
            if p.get("eli5"):
                L += [f"**쉽게 말하면:** {p['eli5']}", ""]
            if p.get("interpretive_basis"):
                L += [f"**해석 근거:** {p['interpretive_basis']}", ""]
            if p.get("svo"):
                L += [f"**구조(SVO):** {p['svo']}", ""]
            if p.get("source_quote"):
                L += ["> " + str(p["source_quote"]).replace("\n", "\n> "), ""]
        refs = p.get("evidence_refs") or []
        if refs:
            L.append("### 근거")
            L += [f"- 근거: [[{fmap[r]}]]" if r in fmap else f"- 근거: {r}" for r in refs[:10]]
            L.append("")
        rels = out_edges.get(nid) or []
        if rels:
            L.append("### 관계")
            L += [f"{rel}:: [[{fmap[t]}]]" for rel, t in rels]
            L.append("")
        open(os.path.join(d, fmap[nid] + ".md"), "w", encoding="utf-8").write("\n".join(L))
        count += 1
    causal = [(e.get("from_id"), e.get("relation"), e.get("to_id")) for e in edges
              if e.get("relation") in CAUSAL_RELS
              and e.get("from_id") in nodes and e.get("to_id") in nodes]
    used = sorted({e.get("relation") for e in edges if e.get("relation")})
    claims = [nid for nid, n in nodes.items() if n.get("space") == "claim"]
    themes = [nid for nid, n in nodes.items() if n.get("node_type") in ("Theme", "Topic")]
    tcount: dict = {}
    for n in nodes.values():
        tcount[n.get("space")] = tcount.get(n.get("space"), 0) + 1
    moc = ["---", f'title: "{pack} 노트팩 MOC"', "tags:", f"  - 독서팩/{pack}",
           f"date: {today}", "type: MOC", "---", f"## {pack} — 노트팩", "",
           f"노드 {len(nodes)} · 관계 {len(edges)} · 저작일 {today}", "",
           "| 층 | 규모 |", "|---|---|",
           f"| 개념(스키마) | {tcount.get('concept', 0)} |",
           f"| 사건·행위(키네틱) | {tcount.get('kinetic', 0)} · 인과 화살표 {len(causal)} |",
           f"| 주장·주제 | 주장 {len(claims)} · 주제 {len(themes)} |",
           f"| 근거 | {tcount.get('evidence', 0)} |", ""]
    if causal:
        moc += ["## 키네틱 도미노", ""] + \
               [f"- [[{fmap[s]}]] —{r}→ [[{fmap[t]}]]" for s, r, t in causal] + [""]
    if claims:
        moc += ["## 주장", ""] + [f"- [[{fmap[c]}]]" for c in claims] + [""]
    moc += ["## 동사 범례", ""] + [f"- `{r}`" for r in used] + ["",
            "## 걷는 법", "",
            "\"왜?\"는 사건 노트의 `triggers::`/`results_in::` 링크를 따라간다.",
            "역방향은 백링크 패널. 근거는 주장·사건 노트의 근거 링크와 백링크에서 연다."]
    moc_name = f"{_safe_filename(pack)}-MOC.md"
    open(os.path.join(note_dir, moc_name), "w", encoding="utf-8").write("\n".join(moc))
    return {"count": count, "moc": moc_name}


def _save_notepack(buf: dict, pack: str, authoring_quality: dict,
                   save_to: str | None, overwrite: bool) -> dict:
    """노트팩 저장: 게이트 통과한 버퍼 → 노트 폴더 + MOC + quality/ledger.

    G6는 만든 폴더를 노트팩 로더로 직접 다시 열어 실측한다 ('구운 뒤 열어서 확인').
    실패하면 방금 만든 산출물을 제거한다 — 반쪽 팩을 볼트에 남기지 않는다.
    """
    import shutil as _sh
    today = datetime.date.today().isoformat()
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        return {"error": "노트팩 생성은 로컬 전용입니다 (zip 생산 폐기 — 서버 역할은 오너 결정 대기)."}
    destination = save_to or PACK_DESTINATIONS.get(pack) or _load_settings().get("pack_dir")
    if not destination:
        return {"status": "needs_save_path",
                "ask_user": "노트팩을 어느 폴더에 만들까요? 옵시디언 볼트의 팩 서가 경로를 알려주세요."}
    base = os.path.expanduser(destination)
    pack_root = os.path.join(base, _safe_filename(pack))
    note_dir = os.path.join(pack_root, "note-pack")
    if os.path.exists(note_dir) and not overwrite:
        return {"status": "needs_overwrite_confirm", "note_dir": note_dir,
                "ask_user": ("이미 같은 이름의 노트팩이 있습니다. 노트가 정본입니다 — 버퍼로 "
                             "덮어쓰면 노트에서 고친 내용이 사라집니다. 덮어쓸까요?"),
                "hint": "사용자가 승인하면 pack_save(..., overwrite=True)로 재호출하세요."}
    if os.path.exists(note_dir):
        _sh.rmtree(note_dir)
    qa = _qa_scan(pack)
    written = _write_notepack(buf, pack, note_dir, today)
    heldout = _heldout_verify(note_dir, buf)
    if heldout["failures"]:
        _sh.rmtree(note_dir, ignore_errors=True)
        return {"status": "needs_quality", "gate": "G6", "pack": pack, "heldout": heldout,
                "next": ("실패한 질문이 grounded가 되도록 근거·주장을 보강하거나, 검수 질문을 "
                         "실제 저작 내용에 맞게 pack_register_checks로 다시 등록한 뒤 재시도하세요. "
                         "저장되지 않았습니다.")}
    m = _quality_measure(buf)
    ledger = _quality_ledger(m, _work_type(buf, m), heldout)
    qdir = os.path.join(note_dir, "quality")
    os.makedirs(qdir, exist_ok=True)
    open(os.path.join(qdir, "ledger.json"), "w", encoding="utf-8").write(
        json.dumps(ledger, ensure_ascii=False, indent=2))
    open(os.path.join(qdir, "pack-contract.json"), "w", encoding="utf-8").write(
        json.dumps(_pack_contract(buf["nodes"], buf["edges"]),
                   ensure_ascii=False, indent=2))
    result = {"format": "notepack", "saved_to": note_dir, "pack_folder": pack_root,
              "counts": {"nodes": len(buf["nodes"]), "edges": len(buf["edges"]),
                          "notes": written["count"]},
              "moc": written["moc"], "authoring_quality": authoring_quality,
              "qa_status": qa["status"], "qa_issues": qa["counts"]["issues"],
              "quality": {"odyssey_class_score": ledger["odyssey_class_score"],
                          "gaps": ledger["gaps"],
                          "canonical_grade": ledger["canonical_grade"],
                          "work_type": ledger["work_type"],
                          "heldout": f"{len(ledger['heldout']['results'])}문항 실측"},
              "note": f"'{pack}' 노트팩을 만들었습니다: {note_dir} — 옵시디언에서 {written['moc']}를 여세요.",
              "next_question": ("이 노트팩을 기본 팩으로 등록할까요? "
                                 f"등록: pack_set_default(\"{note_dir}\")")}
    if qa["status"] != "pass":
        result["qa_warning"] = "그래머 위반이 있습니다. pack_qa로 확인 후 수정을 권장합니다."
    return result


@mcp.tool()
def pack_migrate_to_notes(zip_path: str, dest_dir: str = "", overwrite: bool = False) -> dict:
    """기존 zip 정본을 옵시디언 노트팩으로 이행한다. zip은 보존한다 (이행기 하위호환 D1).

    게이트 없이 충실 복제한다: 노드·엣지·근거·품질 문서를 그대로 노트로 펼친다
    (이행은 기존 정본의 형식 전환이지 신규 저작이 아니다). dest_dir 생략 시
    zip이 있는 폴더(책 폴더) 아래 note-pack/을 만든다. 이행 후 폴더를 다시 열어
    노드 수 일치를 실측한다.
    """
    import shutil as _sh
    from . import local_pack
    zp = os.path.expanduser(zip_path)
    if not os.path.isfile(zp):
        return {"error": f"zip이 없습니다: {zp}"}
    lp = local_pack.LocalPack(zp)
    edges = []
    for src, lst in lp.adj.items():
        for rel, dst in lst:
            if not str(rel).startswith("~"):
                edges.append({"from_id": src, "relation": rel, "to_id": dst})
    buf_like = {"nodes": lp.nodes, "edges": edges}
    book_dir = os.path.expanduser(dest_dir) if dest_dir else os.path.dirname(zp)
    note_dir = os.path.join(book_dir, "note-pack")
    if os.path.exists(note_dir) and not overwrite:
        return {"status": "needs_overwrite_confirm", "note_dir": note_dir,
                "hint": "이미 노트팩이 있습니다. 노트가 정본이면 이행이 필요 없습니다. "
                        "정말 다시 펼치려면 overwrite=True로 재호출하세요."}
    if os.path.exists(note_dir):
        _sh.rmtree(note_dir)
    pack_name = os.path.basename(book_dir) or lp.pack_id
    today = datetime.date.today().isoformat()
    written = _write_notepack(buf_like, pack_name, note_dir, today)
    qdocs = lp._quality_docs()
    qdir = os.path.join(note_dir, "quality")
    os.makedirs(qdir, exist_ok=True)
    if qdocs:
        for rel, doc in qdocs.items():
            open(os.path.join(qdir, os.path.basename(rel)), "w", encoding="utf-8").write(
                json.dumps(doc, ensure_ascii=False, indent=2))
    open(os.path.join(qdir, "pack-contract.json"), "w", encoding="utf-8").write(
        json.dumps(_pack_contract(buf_like["nodes"], buf_like["edges"]),
                   ensure_ascii=False, indent=2))
    reopened = local_pack.LocalPack(note_dir)
    return {"format": "notepack", "migrated_from": zp, "saved_to": note_dir,
            "counts": {"zip_nodes": len(lp.nodes), "note_nodes": len(reopened.nodes),
                        "notes": written["count"], "quality_docs": len(qdocs)},
            "faithful": len(reopened.nodes) == len(lp.nodes),
            "moc": written["moc"],
            "note": "zip은 보존됩니다 (읽기 하위호환). 이행 후 정본은 노트팩입니다."}


@mcp.tool()
def pack_save(pack: str = DEFAULT_PACK, include_embeddings: bool = True,
              save_to: str | None = None, overwrite: bool = False) -> dict:
    """현재 팩을 옵시디언 노트팩으로 저장한다 (zip 생산은 폐기됨 — 2026-08-13 오너 결정).

    산출: {서가}/{팩이름}/note-pack/ 폴더 — 타입별 노트(frontmatter 전 필드 +
    `동사:: [[대상]]` 관계) + {팩이름}-MOC.md 허브 + quality/ledger.json.
    저장 전 게이트(승격·커버리지·G1~G6)를 통과해야 하며, G6은 만든 폴더를 직접
    다시 열어 검수 질문을 실측한다. 임베딩·색인은 qmd가 볼트 차원에서 자동 수행하며
    유팩은 qmd를 조작하지 않는다 —
    include_embeddings 인자는 하위호환용으로 무시된다.

    save_to가 비어 있으면 설정 인터뷰의 팩 서가(pack_dir)를 쓰고, 그것도 없으면
    needs_save_path로 사용자에게 묻는다. 같은 노트팩이 이미 있으면 덮어쓰지 않고
    needs_overwrite_confirm을 반환한다 — 노트가 정본이다.
    """
    _ro = _read_only_block()
    if _ro:
        return _ro
    import tempfile as _tf
    from . import local_pack
    buf = _get_pack(pack)
    if not buf["nodes"]:
        return {"error": f"팩이 비어 있습니다: {pack}"}
    gate = _promotion_gate(buf, pack)
    if gate:
        return gate
    authoring = buf.get("authoring") or {}
    if authoring.get("required") and not authoring.get("complete"):
        source_ids, covered = _authoring_coverage(buf)
        return {
            "status": "needs_authoring_completion", "pack": pack,
            "source_ids": source_ids,
            "uncovered_source_ids": sorted(set(source_ids) - set(covered)),
            "next": "호스트 저작을 마친 뒤 pack_authoring_complete(pack=...)를 호출하세요. 통과 전에는 노트팩을 저장하지 않습니다.",
        }
    authoring_quality = _authoring_quality(buf)
    if authoring_quality["status"] != "pass":
        return {"status": "needs_authoring", "quality": authoring_quality,
                "next": "원문을 구조화해 Claim 또는 Kinetic을 근거와 연결한 뒤 다시 pack_save를 호출하세요. Evidence-only 팩은 저장하지 않습니다."}
    save_gate = _quality_gate(buf, pack)
    if save_gate:
        return save_gate
    # zip 생산 폐기(2026-08-13 오너 결정) — 저장은 노트팩 폴더 생성이다.
    return _save_notepack(buf, pack, authoring_quality, save_to, overwrite)


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
