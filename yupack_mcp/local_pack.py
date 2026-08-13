"""yupack 로컬 팩 엔진: self-contained ontology pack의 빌드·오픈·질의·거버넌스.

설계 계약 (2026-07-23 Daniel 확정):
- pack = zip 하나로 완결 (데이터 + 검수 + indexes/ + runtime/ + reports/)
- 원본은 read_only, 수정은 overlay + immutable audit event
- cloud fallback 없음. 근거 없으면 no_local_evidence
- 벡터 임베딩은 로컬 OMLX bge-m3(127.0.0.1:8000). 미가동 시 lexical+graph로 동작
"""
from __future__ import annotations
import datetime
import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import tempfile
import urllib.request
import zipfile

EMBED_URL = os.environ.get("YUPACK_EMBED_URL", "http://127.0.0.1:8000/v1/embeddings")
BGE_CACHE_DIR = os.path.expanduser(
    os.environ.get("YUPACK_BGE_CACHE_DIR", "~/.cache/yupack/bge-m3"))

# 기본 임베딩 계약은 로컬 OMLX bge-m3(1024d)다. API 키는 필요 없다.
# QMD는 볼트 검색 도구이며, Yupack에서는 사용자가 고른 호환 모드(YUPACK_EMBED_MODEL=qmd
# 또는 settings.json embed_model=qmd)로만 쓴다. 백엔드 자동 전환은 팩의 저장 벡터 계약을
# 조용히 바꾸므로 금지한다. 백엔드가 죽으면 서버의 설정 게이트가 사용자에게 다시 묻는다.
def _qmd_available() -> bool:
    import shutil
    return shutil.which("qmd") is not None


def settings_file() -> str:
    raw = os.environ.get("YUPACK_SETTINGS")
    return os.path.expanduser(raw) if raw else os.path.expanduser("~/.yupack/settings.json")


def _settings_embed_model() -> str | None:
    try:
        with open(settings_file(), encoding="utf-8") as f:
            v = json.load(f).get("embed_model")
        return v if isinstance(v, str) and v else None
    except (OSError, ValueError):
        return None


def _pick_model() -> tuple[str, int]:
    forced = os.environ.get("YUPACK_EMBED_MODEL") or _settings_embed_model()
    if forced == "bge-m3":
        return "bge-m3", 1024
    if forced == "qmd":
        return "qmd", 768
    if forced == "none":  # 벡터 없이 어휘+그래프만 (설정 인터뷰에서 사용자가 고른 값)
        return "none", 0
    if forced:
        return forced, {"text-embedding-3-large": 3072, "text-embedding-3-small": 1536}.get(forced, 1536)
    return "bge-m3", 1024


EMBED_MODEL, EMBED_DIM = _pick_model()


def refresh_embed_model() -> tuple[str, int]:
    """설정 변경을 프로세스 재시작 없이 반영한다 (pack_configure·팩 열기 시점에 호출)."""
    global EMBED_MODEL, EMBED_DIM
    EMBED_MODEL, EMBED_DIM = _pick_model()
    return EMBED_MODEL, EMBED_DIM


def omlx_alive(timeout: float = 0.6) -> bool:
    """로컬 OMLX 임베딩 서버 생존 확인 (설정 게이트가 선택지·재질문 판정에 쓴다)."""
    base = EMBED_URL.rsplit("/embeddings", 1)[0]
    try:
        urllib.request.urlopen(f"{base}/models", timeout=timeout)
        return True
    except Exception:
        return False

# 인과 축 (2026-08-12): "왜" 계열 질문은 방향을 보존한 인과 확장 + Claim 투영이 필요하다.
# 배관 관계(contains/contains_segment)는 원문 분할 구조라 인과 확장에서 제외한다.
CAUSAL_RELS = {"triggers", "causes", "prevents", "enables", "results_in",
               "precondition_of", "leads_to", "part_of_process", "realizes"}
PROJECT_RELS = {"records", "supports", "characterizes", "about"}
PLUMBING_RELS = {"contains", "contains_segment"}
_CAUSAL_Q = re.compile(r"왜|어째서|어찌|무슨 이유|원인|이유|때문|대가|탓|초래|결과적으로"
                       r"|why|cause|because", re.IGNORECASE)


def _is_causal_question(q: str) -> bool:
    return bool(_CAUSAL_Q.search(q))


# 근거 게이트 판정에서 제외할 메타데이터 키 (경로·식별자·순서 정보는 근거 본문이 아니다)
_GATE_META_KEYS = {"source_path", "source_id", "source_locator", "locator", "path", "url",
                   "node_id", "graph_id", "evidence_refs", "source_order", "story_order",
                   "id", "evidence_id", "kind", "assertion_mode", "extraction_note",
                   "source_map_scope", "evidence_role"}

# 근거 게이트 본문으로 인정하는 "서술 텍스트" 필드 (화이트리스트)
_GATE_TEXT_KEYS = {"label", "label_ko", "aliases_ko", "text", "summary", "definition",
                   "statement", "description", "mechanism", "applies_when", "tradeoffs",
                   "eli5", "note", "body"}

_EN_STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "be", "been", "of", "in",
                 "on", "at", "to", "for", "and", "or", "but", "with", "from", "by", "as",
                 "what", "who", "whom", "when", "where", "why", "how", "which", "that",
                 "this", "these", "those", "do", "does", "did", "it", "its", "his", "her",
                 "their", "there", "have", "has", "had", "not", "than", "then", "so"}


_HANDLES: dict[str, "LocalPack"] = {}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _embed(texts: list[str], model: str | None = None) -> list[list[float]] | None:
    model = model or EMBED_MODEL
    if model == "none":
        return None
    try:
        if model.startswith("text-embedding"):
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                return None
            req = urllib.request.Request(
                "https://api.openai.com/v1/embeddings",
                json.dumps({"model": model, "input": texts}).encode(),
                {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        else:
            req = urllib.request.Request(
                EMBED_URL, json.dumps({"model": model, "input": texts}).encode(),
                {"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=600))
        return [r["embedding"] for r in d["data"]]
    except Exception:
        return None


QMD_DIR = os.path.expanduser(os.environ.get("YUPACK_QMD_DIR", "~/.cache/yupack/qmd"))


def _qmd_collection_name(pack_id: str) -> str:
    # QMD 컬렉션명은 ASCII만 안정적으로 다룬다. 한글 제목을 '-'로만 바꾸면
    # 같은 날 만든 여러 팩이 전부 같은 이름이 되어 벡터 결과가 섞일 수 있다.
    # 읽을 수 있는 ASCII 부분 + 원본 pack_id 해시를 함께 써서 충돌을 막는다.
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", pack_id).strip("-_") or "pack"
    digest = _sha256(pack_id.encode())[:12]
    return f"yupack-{slug[:36]}-{digest}"


def _qmd_doc_text(n: dict) -> str:
    p = n.get("properties", {})
    alias = p.get("aliases_ko") or []
    if isinstance(alias, str):
        alias = [alias]
    return " ".join(str(x) for x in [n.get("label") or p.get("label"), p.get("label_ko"), *alias,
                                      p.get("text"), p.get("definition"), p.get("statement"),
                                      p.get("mechanism")] if x)[:1500]


def _node_embedding_text(n: dict) -> str:
    """팩·로컬 sidecar가 같은 bge-m3 입력을 쓰도록 한 곳에 고정한다."""
    p = n.get("properties", {})
    aliases = p.get("aliases_ko") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    return " ".join(str(x) for x in [
        n.get("label"), p.get("label"), p.get("label_ko"), *aliases,
        p.get("text"), p.get("summary"), p.get("definition"), p.get("statement"),
        p.get("mechanism"), p.get("description"),
    ] if x)[:1500]


def _qmd_ensure_collection(pack_id: str, nodes: dict) -> str | None:
    """팩 노드들을 qmd 컬렉션으로 보장한다 (멱등). 성공 시 컬렉션명, 실패 시 None."""
    import subprocess
    if not _qmd_available():
        return None
    col = _qmd_collection_name(pack_id)
    d = os.path.join(QMD_DIR, col)
    os.makedirs(d, exist_ok=True)
    wrote = 0
    for nid, n in nodes.items():
        if n.get("space") == "resource":
            continue
        text = _qmd_doc_text(n)
        if not text.strip():
            continue
        fp = os.path.join(d, nid.replace(":", "__") + ".md")
        p = n.get("properties", {})
        body = f"# {p.get('label_ko') or p.get('label') or nid}\n\n{text}\n"
        try:
            if not os.path.exists(fp) or open(fp, encoding="utf-8").read() != body:
                open(fp, "w", encoding="utf-8").write(body)
                wrote += 1
        except Exception:
            continue
    try:
        r = subprocess.run(["qmd", "collection", "show", col], capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or col not in (r.stdout + r.stderr):
            subprocess.run(["qmd", "collection", "add", d, "--name", col],
                            capture_output=True, text=True, timeout=60)
        if wrote:
            subprocess.run(["qmd", "update"], capture_output=True, text=True, timeout=300)
        subprocess.run(["qmd", "embed", "-c", col], capture_output=True, text=True, timeout=600)
        return col
    except Exception:
        return None


def _qmd_vector_search(col: str, q: str, k: int) -> list[dict]:
    import subprocess
    if not q:
        return []
    try:
        out = subprocess.run(["qmd", "query", "vec: " + q, "-c", col, "-n", str(k)],
                              capture_output=True, text=True, timeout=90).stdout
    except Exception:
        return []
    hits, cur = [], None
    for ln in out.splitlines():
        m = re.match(rf"qmd://{re.escape(col)}/(.+?)\.md", ln.strip())
        if m:
            cur = m.group(1).replace("__", ":")
            continue
        m2 = re.search(r"Score:\s+(\d+)%", ln)
        if m2 and cur:
            hits.append({"id": cur, "cosine": int(m2.group(1)) / 100.0, "text": ""})
            cur = None
    return hits[:k]


def _jsonl(text: str) -> list[dict]:
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def _norm_evidence(e: dict) -> dict:
    """evidence.jsonl 행을 카드 계약으로 정규화한다.

    스키마가 팩 세대마다 다르다: 구형 flat(text/condition/grade), 정본 11권형
    (최상위 id/summary + 중첩 locator{} + properties{text|summary|short_quote}).
    키를 못 찾으면 카드 summary가 조용히 null이 되어 저자 대화에서 근거 요약문이
    빠진다 (북팩 레인 인계 findings 2026-08-13) — 양쪽 키를 전부 수용한다.
    """
    if "evidence_id" in e:
        return e
    props = e.get("properties") or {}
    return {"evidence_id": e.get("id"),
            "summary": (e.get("summary") or e.get("text") or props.get("summary")
                        or props.get("text") or props.get("short_quote")
                        or e.get("metric_or_observation")),
            "conditions": e.get("condition") or e.get("conditions") or props.get("conditions"),
            "limitations": e.get("limitation") or e.get("limitations") or props.get("limitations"),
            "evidence_grade": e.get("grade") or e.get("evidence_grade") or props.get("evidence_grade"),
            "evidence_kind": e.get("canonical_layer") or props.get("kind"),
            "source_id": e.get("source_id") or props.get("source_id"),
            "source_locator": e.get("source_locator") or e.get("locator"),
            "verbatim_or_paraphrase": e.get("verbatim_or_paraphrase") or props.get("verbatim_or_paraphrase")}


# ======================= 빌드: 정본 zip -> queryable zip =======================
def build_queryable(source_zip: str, out_zip: str | None = None,
                    include_embeddings: bool = True) -> dict:
    """정본 zip(nodes/edges/evidence/reviews)을 계약 구조의 질의 가능 zip으로 재구성한다."""
    refresh_embed_model()
    source_zip = os.path.expanduser(source_zip)
    z = zipfile.ZipFile(source_zip)
    names = [n for n in z.namelist() if "__MACOSX" not in n and "__pycache__" not in n
             and not n.endswith((".pyc", ".DS_Store"))]

    def find(suffix):
        c = [n for n in names if n.endswith(suffix)]
        return min(c, key=len) if c else None

    def read(suffix, default=""):
        f = find(suffix)
        return z.read(f).decode("utf-8") if f else default

    nodes_t = read("nodes.jsonl")
    if not nodes_t:
        return {"error": "source zip에 nodes.jsonl이 없습니다."}
    edges_t = read("edges.jsonl")
    evidence_t = read("evidence.jsonl")
    reviews_t = read("reviews.jsonl")
    pack_yaml = read("pack.yaml", "schema: yucrates.ontology.v1.2\n")
    sources_t = read("sources.json", "") or read("discovery_sources.json", "{}")

    nodes = _jsonl(nodes_t)
    edges = _jsonl(edges_t)
    evidence = [_norm_evidence(e) for e in _jsonl(evidence_t)]
    reviews = _jsonl(reviews_t)

    files: dict[str, bytes] = {
        "pack.yaml": pack_yaml.encode(),
        "nodes.jsonl": nodes_t.encode(),
        "edges.jsonl": edges_t.encode(),
        "evidence.jsonl": evidence_t.encode(),
        "reviews.jsonl": reviews_t.encode(),
        "sources.json": sources_t.encode(),
    }
    for rep in ("nine-space-contract.md", "nine-stage-verification-contract.md"):
        t = read(rep)
        if t:
            files[f"reports/{rep}"] = t.encode()

    # 팩 생산 헌장 자동 동봉 (헌장 9조): YUPACK_CHARTER > 팩 서랍의 PACK-CHARTER.md
    # 서랍 경로는 사람마다 다르다. 설정이 없으면 동봉을 건너뛴다(특정인 경로를 박지 않는다).
    _dir = os.environ.get("YUPACK_PACK_DIR")
    for cand in (os.environ.get("YUPACK_CHARTER"),
                 os.path.join(os.path.expanduser(_dir), "PACK-CHARTER.md") if _dir else None):
        if cand and os.path.isfile(os.path.expanduser(cand)):
            files["reports/pack-production-charter.md"] = open(
                os.path.expanduser(cand), "rb").read()
            break

    # graph adjacency
    adj: dict[str, list] = {}
    for e in edges:
        s = e.get("source") or e.get("from_id")
        t = e.get("target") or e.get("to_id")
        rel = e.get("relation", "related_to")
        adj.setdefault(s, []).append([rel, t])
        adj.setdefault(t, []).append([f"~{rel}", s])
    files["graph-index/adjacency.jsonl"] = "\n".join(
        json.dumps({"id": k, "edges": v}, ensure_ascii=False) for k, v in adj.items()).encode()

    # lexical FTS5
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.execute("CREATE VIRTUAL TABLE docs USING fts5(id UNINDEXED, kind UNINDEXED, body)")
    rows = []
    for n in nodes:
        p = n.get("properties", {})
        alias = p.get("aliases_ko") or []
        if isinstance(alias, str):
            alias = [alias]
        body = " ".join(str(x) for x in [
            n.get("label"), p.get("label"), p.get("label_ko"), *alias,
            p.get("definition"), p.get("statement"),
            p.get("mechanism"), p.get("applies_when")] if x)
        rows.append((n["id"], f"node:{n.get('space','')}", body))
    for ev in evidence:
        body = " ".join(str(x) for x in [ev.get("summary"), ev.get("conditions"),
                                          ev.get("limitations")] if x)
        rows.append((ev["evidence_id"], "evidence", body))
    con.executemany("INSERT INTO docs VALUES(?,?,?)", rows)
    con.commit(); con.close()
    files["lexical-index/fts.sqlite"] = open(tmp.name, "rb").read()
    os.unlink(tmp.name)

    # vectors (로컬 bge-m3)
    emb_meta, emb_status = [], "skipped"
    qmd_backend = (EMBED_MODEL == "qmd")
    if include_embeddings and qmd_backend:
        # 무키 로컬 벡터: 노드를 qmd 컬렉션으로 (EmbeddingGemma 768d, 실측 30/30)
        nd = {n["id"]: n for n in nodes}
        _pid = os.path.splitext(os.path.basename(out_zip or source_zip))[0]
        col = _qmd_ensure_collection(_pid, nd)
        if col:
            emb_status = f"qmd({col}, embeddinggemma-768d, 로컬 무료)"
        else:
            emb_status = "unavailable(qmd 실행 실패): lexical+graph로 동작"
    if include_embeddings and not qmd_backend:
        targets = []
        for n in nodes:
            if n.get("space") == "resource":
                continue
            text = _node_embedding_text(n)
            if text.strip():
                targets.append((n["id"], text))
        vec_buf = io.BytesIO()
        ok = True
        for i in range(0, len(targets), 64):
            batch = targets[i:i + 64]
            vs = _embed([t for _, t in batch])
            if vs is None:
                ok = False
                break
            for (nid, text), v in zip(batch, vs):
                emb_meta.append({"id": nid, "row": len(emb_meta),
                                  "text_sha256": _sha256(text.encode())})
                vec_buf.write(struct.pack(f"{EMBED_DIM}f", *v))
        if ok and emb_meta:
            files["vector-index/vectors.bin"] = vec_buf.getvalue()
            files["vector-index/vector-metadata.jsonl"] = "\n".join(
                json.dumps(m) for m in emb_meta).encode()
            emb_status = f"included({len(emb_meta)} x {EMBED_DIM}, {EMBED_MODEL}, normalized)"
        else:
            emb_status = ("unavailable(로컬 OMLX bge-m3에 연결할 수 없음): "
                          "lexical+graph로 동작")

    # query-docs/: 레버·결정경로·정책 근거 카드 (조건·한계·검수 상태 원문 그대로)
    review_by_target = {}
    for r in reviews:
        review_by_target.setdefault(r.get("target_id"), []).append(
            {"status": r.get("review_status"), "reviewer_role": r.get("reviewer_role")})
    ev_by_id = {e["evidence_id"]: e for e in evidence}
    # 근거가 supports로 연결된 노드도 카드 대상 (일반 사용자 팩: 개념+근거 구조)
    supported = set()
    for e in edges:
        if e.get("relation") in ("supports", "describes", "mentions", "exemplifies"):
            supported.add(e.get("target") or e.get("to_id"))
    n_cards = 0
    for n in nodes:
        is_core = n.get("space") in ("lever", "outcome", "policy") or str(n.get("id","")).startswith("path:")
        if not is_core and n.get("id") not in supported:
            continue
        p = n.get("properties", {})
        refs = list(p.get("evidence_refs") or [])
        for e in edges:
            if e.get("relation") in ("supports", "describes", "mentions", "exemplifies") \
                    and (e.get("target") or e.get("to_id")) == n["id"]:
                src_ev = e.get("source") or e.get("from_id")
                if src_ev in ev_by_id and src_ev not in refs:
                    refs.append(src_ev)
        card = {"id": n["id"], "space": n.get("space"),
                "label": n.get("label") or p.get("label"), "properties": p,
                "review_status": review_by_target.get(n["id"], []),
                "evidence_cards": [
                    {k: ev_by_id[e].get(k) for k in ("evidence_id", "summary", "conditions",
                     "limitations", "evidence_grade", "source_id", "source_locator")}
                    for e in refs if e in ev_by_id],
                "governance": "local_validation_required" if n.get("space") == "lever" else "informational"}
        fn = re.sub(r"[^\w가-힣.-]+", "_", n["id"])
        files[f"query-docs/{fn}.json"] = json.dumps(card, ensure_ascii=False, indent=1).encode()
        n_cards += 1

    # runtime: ontology.sqlite(overlay/audit) + policy.yaml
    tmp2 = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp2.close()
    rcon = sqlite3.connect(tmp2.name)
    for ddl in (
        "CREATE TABLE overlay_nodes (id TEXT PRIMARY KEY, data TEXT, op TEXT, ts TEXT)",
        "CREATE TABLE overlay_edges (rowid_ INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, ts TEXT)",
        "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, detail TEXT)",
        "CREATE TABLE workflow_runs (run_id TEXT PRIMARY KEY, kind TEXT, state TEXT, ts TEXT)",
        "CREATE TABLE approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT, status TEXT, ts TEXT)",
        "CREATE TABLE promotions (candidate_id TEXT PRIMARY KEY, target TEXT, status TEXT, validation TEXT, ts TEXT)",
        "CREATE TABLE identity_aliases (alias TEXT, canonical TEXT, ts TEXT)",
        "CREATE TABLE duplicates (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT, b TEXT, status TEXT, ts TEXT)",
    ):
        rcon.execute(ddl)
    rcon.commit(); rcon.close()
    files["runtime/ontology.sqlite"] = open(tmp2.name, "rb").read()
    os.unlink(tmp2.name)
    files["runtime/policy.yaml"] = (
        "rebac:\n  default: read_only\n  roles: {owner: [read, write, promote], student: [read]}\n"
        "workflow:\n  stages: [authoring, validation, independent_review, promotion]\n"
        "promotion:\n  auto_promote: false\n"
        "  rule: candidate_not_independently_reviewed는 reviewed로 자동 승격하지 않는다\n"
        "answers:\n  no_local_evidence_on_miss: true\n  cloud_fallback: false\n"
        "  lever_without_evidence: never_recommend\n").encode()

    # validation report + manifest.lock + integrity
    counts = {"nodes": len(nodes), "edges": len(edges), "evidence": len(evidence),
              "reviews": len(reviews), "embedded": len(emb_meta),
              "adjacency_entries": len(adj)}
    spaces = {}
    for n in nodes:
        spaces[n.get("space", "?")] = spaces.get(n.get("space", "?"), 0) + 1
    files["reports/validation-report.json"] = json.dumps({
        "built_at": _now(), "source_zip": os.path.basename(source_zip),
        "source_sha256": _sha256(open(source_zip, "rb").read()),
        "counts": counts, "nine_spaces": spaces,
        "embeddings": emb_status,
        "checks": {"nodes_nonempty": len(nodes) > 0,
                    "edge_refs_resolved": "deferred_to_open(무결성은 open 시 재검증)",
                    "reviews_preserved": len(reviews)},
    }, ensure_ascii=False, indent=1).encode()
    # 품질층 보존 (T1): 원본 zip의 quality/ 문서를 재구성 후에도 그대로 담는다.
    # manifest·integrity 해시 계산 전에 넣어, 변조 시 무결성 검증도 함께 깨지게 한다.
    nd_path = find("nodes.jsonl") or ""
    base = nd_path[: -len("nodes.jsonl")] if nd_path else ""
    if base.endswith("graph/"):
        base = base[: -len("graph/")]
    for n in names:
        # 2종 계약: baseline-standard.snapshot.md + baseline-compliance.json — md도 보존
        if "quality/" in n and not n.endswith("/"):
            rel = n[len(base):] if base and n.startswith(base) else "quality/" + n.split("quality/", 1)[1]
            files.setdefault(rel, z.read(n))

    manifest = {"schema": "yucrates.ontology.v1.2", "index_version": 1,
                "created": _now(), "embed_model": EMBED_MODEL if emb_meta else None,
                "embed_dim": EMBED_DIM if emb_meta else None,
                "sha256": {k: _sha256(v) for k, v in files.items()}}
    files["runtime/manifest.lock"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode()

    files["embeddings.json"] = json.dumps({
        "model": EMBED_MODEL if emb_meta else None, "dim": EMBED_DIM if emb_meta else None,
        "normalized": True, "count": len(emb_meta),
        "storage": "vector-index/vectors.bin (float32 row-major)",
        "per_row_text_sha256": "vector-index/vector-metadata.jsonl",
        "corpus_sha256": _sha256(b"".join(m["text_sha256"].encode() for m in emb_meta)) if emb_meta else None,
        "query_runtime": ("정본 벡터 모델과 같은 로컬 OMLX bge-m3 런타임을 사용한다. "
                          "OMLX를 사용할 수 없으면 lexical+graph로 폴백하며, QMD는 "
                          "명시적 호환 모드에서만 사용한다."),
    }, ensure_ascii=False, indent=1).encode()
    files["query-contract.json"] = json.dumps({
        "discovery_layer": "search_only",
        "decision_core": "local_validation_required",
        "no_cloud_fallback": True,
        "no_pack_side_prescription": "팩은 근거 카드만 반환한다. 자연어 답변은 호출측 책임.",
        "no_grounded_match_policy": "근거 없으면 no_local_evidence를 반환하고 팩 밖 지식으로 답하지 않는다.",
        "answer_requirements": ["matched_levers", "claims", "direct_evidence", "sources",
                                  "conditions", "limitations", "review_status",
                                  "retrieval_trace", "local_pack_id", "manifest_hash"],
    }, ensure_ascii=False, indent=1).encode()
    files["integrity.json"] = json.dumps({
        "counts": {"nodes": len(nodes), "edges": len(edges), "evidence": len(evidence),
                    "reviews": len(reviews), "query_docs": n_cards, "embedded": len(emb_meta)},
        "sha256": {k: _sha256(v) for k, v in sorted(files.items())},
    }, ensure_ascii=False, indent=1).encode()

    out_zip = out_zip or os.path.join(
        os.path.dirname(source_zip),
        os.path.basename(source_zip).replace(".zip", "") + "-queryable.zip")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zo:
        for name, data in sorted(files.items()):
            zo.writestr(name, data)
    return {"out_zip": out_zip, "counts": counts, "embeddings": emb_status,
            "files": sorted(files)}


# ======================= 오픈/질의 =======================
class LocalPack:
    def __init__(self, zip_path: str, mode: str = "read_only"):
        self.zip_path = os.path.expanduser(zip_path)
        self.mode = mode
        if os.path.isdir(self.zip_path):
            # 노트팩(옵시디언 노트 폴더) — zip 생산 폐기 전환(2026-08-13 오너 결정)의 소비면
            self._init_notepack(self.zip_path)
            return
        raw = open(self.zip_path, "rb").read()
        self.pack_id = os.path.basename(self.zip_path)
        self.manifest_hash = _sha256(raw)
        self.cache = os.path.join(tempfile.gettempdir(),
                                  "yupack-" + self.manifest_hash[:12])
        if not os.path.exists(os.path.join(self.cache, ".ok")):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                z.extractall(self.cache)
            open(os.path.join(self.cache, ".ok"), "w").write("ok")
        # ZIP은 평면형과 wrapper/graph/형을 모두 허용한다. nodes.jsonl이
        # 있는 디렉터리 하나만 고르면 graph/의 형제 index/evidence를 놓친다.
        # __MACOSX/._ 메타데이터는 후보에서 제외하고 계약 마커가 가장 많은
        # 디렉터리를 팩 루트로 택한다.
        candidates: set[str] = {self.cache}
        for dirpath, dirs, fs in os.walk(self.cache):
            dirs[:] = [d for d in dirs if d != "__MACOSX" and not d.startswith("._")]
            if "nodes.jsonl" in fs:
                candidates.add(dirpath)
                candidates.add(os.path.dirname(dirpath))
            if "manifest.lock" in fs and os.path.basename(dirpath) == "runtime":
                candidates.add(os.path.dirname(dirpath))

        def root_score(path: str) -> int:
            markers = ("graph-index", "lexical-index", "vector-index", "evidence", "runtime", "neo4j")
            files = ("integrity.json", "pack.yaml", "manifest.json", "reviews.jsonl", "embeddings.json")
            score = sum(os.path.isdir(os.path.join(path, marker)) for marker in markers)
            score += sum(os.path.isfile(os.path.join(path, marker)) for marker in files)
            score += 2 * os.path.isfile(os.path.join(path, "nodes.jsonl"))
            score += 2 * os.path.isfile(os.path.join(path, "graph", "nodes.jsonl"))
            return score

        root = max(candidates, key=lambda path: (root_score(path), -len(path)))
        self.root = root
        # 무결성 검증 (manifest.lock 있으면)
        self.integrity = "no_manifest"
        integ = os.path.join(root, "integrity.json")
        if os.path.exists(integ):
            m = json.load(open(integ, encoding="utf-8"))
            bad = [rel for rel, want in m["sha256"].items()
                   if rel != "runtime/ontology.sqlite" and
                   (not os.path.exists(os.path.join(root, rel)) or
                    _sha256(open(os.path.join(root, rel), "rb").read()) != want)]
            self.integrity = "ok" if not bad else f"failed:{bad[:5]}"
        lock = os.path.join(root, "runtime", "manifest.lock")
        if self.integrity != "no_manifest":
            lock = ""  # integrity.json이 권위
        if lock and os.path.exists(lock):
            m = json.load(open(lock, encoding="utf-8"))
            bad = []
            for rel, want in m["sha256"].items():
                if rel == "runtime/manifest.lock":
                    continue
                p = os.path.join(root, rel)
                if not os.path.exists(p) or _sha256(open(p, "rb").read()) != want:
                    bad.append(rel)
            bad = [b for b in bad if b != "runtime/ontology.sqlite"]  # 런타임 상태는 가변
            self.integrity = "ok" if not bad else f"failed:{bad[:5]}"
        # 데이터 로드
        self.nodes = {d["id"]: d for d in _jsonl(self._read("nodes.jsonl"))}
        self.evidence = {d["evidence_id"]: d for d in
                         (_norm_evidence(e) for e in _jsonl(self._read("evidence.jsonl")))}
        self.reviews: dict[str, list] = {}
        for r in _jsonl(self._read("reviews.jsonl")):
            self.reviews.setdefault(r["target_id"], []).append(
                {"status": r.get("review_status"), "reviewer_role": r.get("reviewer_role"),
                 "backend": EMBED_MODEL})
        self.adj: dict[str, list] = {}
        adj_p = os.path.join(root, "graph-index", "adjacency.jsonl")
        if not os.path.exists(adj_p):
            adj_p = os.path.join(root, "indexes", "graph-adjacency.jsonl")
        if os.path.exists(adj_p):
            for l in open(adj_p, encoding="utf-8"):
                d = json.loads(l)
                # 구형 Yupack은 [relation, node] 쌍, v2 final pack은
                # {relation, node_id, direction, edge_id} 객체를 쓴다.
                # 탐색 엔진의 내부 계약은 전자로 통일한다.
                self.adj[d["id"]] = [
                    [edge["relation"] if edge.get("direction") != "in" else "~" + edge["relation"], edge["node_id"]]
                    if isinstance(edge, dict) else edge
                    for edge in d["edges"]
                ]
        self.vec_meta, self.vecs = [], b""
        vdir = "vector-index" if os.path.exists(os.path.join(root, "vector-index")) else "indexes"
        vm = os.path.join(root, vdir, "vector-metadata.jsonl")
        if os.path.exists(vm):
            self.vec_meta = [json.loads(l) for l in open(vm)]
            self.vecs = open(os.path.join(root, vdir, "vectors.bin"), "rb").read()
        # 팩에 기록된 임베딩 모델·차원 (질의 임베딩은 반드시 이걸 따른다)
        self.embed_model, self.dim = EMBED_MODEL, EMBED_DIM
        ej = os.path.join(root, "embeddings.json")
        if os.path.exists(ej):
            em = json.load(open(ej, encoding="utf-8"))
            if em.get("model"):
                self.embed_model, self.dim = em["model"], em.get("dimension") or em.get("dim") or EMBED_DIM
        self._audit("open", f"mode={mode}")

    def _read(self, name: str) -> str:
        paths = [os.path.join(self.root, name), os.path.join(self.root, "graph", name)]
        if name == "evidence.jsonl":
            paths.append(os.path.join(self.root, "evidence", "index.jsonl"))
        for path in paths:
            if os.path.exists(path):
                return open(path, encoding="utf-8").read()
        return ""

    # ── 노트팩 로더: 옵시디언 노트 폴더 → 같은 3축 질의 엔진 ──
    _NOTE_SPACE = {"Event": "kinetic", "Action": "kinetic", "State": "kinetic",
                   "Process": "kinetic", "Claim": "claim", "Covariate": "claim",
                   "TextUnit": "evidence", "Evidence": "evidence",
                   "LogEntry": "evidence", "Study": "evidence", "Source": "resource"}
    _REL_LINE = re.compile(r"^([a-z_]+)::\s*\[\[(.+?)\]\]\s*$")

    def _init_notepack(self, root: str) -> None:
        """frontmatter(type·node_id·aliases) + `동사:: [[대상]]` 노트를 nodes/edges로 파싱한다.

        노트가 정본이다: zip 아티팩트(FTS·벡터·integrity) 없이 폴더만으로
        어휘(인메모리 FTS) + 벡터(qmd) + 그래프 3축 질의·거절·영수증 계약을 그대로 제공한다.
        읽기 전용 — 볼트 폴더 안에 어떤 파일도 만들지 않는다 (audit 기록도 끔).
        """
        base_name = os.path.basename(os.path.normpath(root))
        parent = os.path.basename(os.path.dirname(os.path.normpath(root)))
        # {book}/note-pack 배치에서 pack_id가 전부 'note-pack'으로 충돌하지 않게 부모를 붙인다
        self.pack_id = f"{parent}-{base_name}" if base_name == "note-pack" and parent else base_name
        self.root = root
        self.cache = root
        self._no_audit = True
        notes = []
        h = hashlib.sha256()
        for dirpath, dirs, fs in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in sorted(fs):
                if not f.endswith(".md"):
                    continue
                try:
                    text = open(os.path.join(dirpath, f), encoding="utf-8").read()
                except OSError:
                    continue
                h.update(f.encode())
                h.update(_sha256(text.encode()).encode())
                fm, body = self._parse_frontmatter(text)
                if not fm.get("type") or fm.get("type") == "MOC":
                    continue  # MOC·프론트매터 없는 문서는 노드가 아니다
                nid = fm.get("node_id") or os.path.splitext(f)[0]
                rels, locator, orig, kept = [], None, None, []
                for ln in body.splitlines():
                    m = self._REL_LINE.match(ln.strip())
                    if m:
                        rels.append((m.group(1), m.group(2)))
                        continue
                    m2 = re.match(r"^행::\s*(\d+)\s*~\s*(\d+)", ln.strip())
                    if m2:
                        locator = {"start_line": int(m2.group(1)), "end_line": int(m2.group(2))}
                        continue
                    m3 = re.match(r"^원문::\s*\[\[(.+?)\]\]", ln.strip())
                    if m3:
                        orig = m3.group(1)
                        continue
                    kept.append(ln)
                body_text = re.sub(r"^#+\s.*$", "", "\n".join(kept), flags=re.M).strip()
                extras = {k: v for k, v in fm.items()
                          if k not in ("title", "tags", "date", "type", "node_id", "aliases")
                          and isinstance(v, str) and v}
                notes.append({"id": nid, "label": fm.get("title") or os.path.splitext(f)[0],
                              "type": fm.get("type"), "aliases": fm.get("aliases") or [],
                              "extras": extras,
                              "body": body_text, "rels": rels, "locator": locator,
                              "orig": orig, "stem": os.path.splitext(f)[0]})
        self.manifest_hash = h.hexdigest()
        self.integrity = "notepack"
        stem2id: dict[str, str] = {}
        for n in notes:
            stem2id.setdefault(n["stem"], n["id"])
            stem2id.setdefault(n["label"], n["id"])
        self.nodes, self.evidence, self.adj = {}, {}, {}
        for n in notes:
            space = self._NOTE_SPACE.get(n["type"], "concept")
            props: dict = {"label": n["label"], "label_ko": n["label"]}
            # frontmatter의 나머지 스칼라(assertion_mode·confidence·story_order 등)를
            # 속성으로 왕복시킨다 — 노트가 전 필드 정본이 되는 조건
            props.update(n.get("extras") or {})
            if n["aliases"]:
                props["aliases_ko"] = n["aliases"]
            if n["body"]:
                props["text"] = n["body"][:1500]
            if n["locator"]:
                props["locator"] = n["locator"]
            if n["orig"]:
                props["source_path"] = n["orig"]
            self.nodes[n["id"]] = {"id": n["id"], "space": space, "node_type": n["type"],
                                    "label": n["label"], "properties": props}
            if space == "evidence":
                self.evidence[n["id"]] = _norm_evidence(
                    {"id": n["id"], "summary": n["body"][:500] or n["label"],
                     "locator": n["locator"], "source_id": n["orig"]})
        for n in notes:
            for rel, target in n["rels"]:
                tid = stem2id.get(target)
                if not tid or tid not in self.nodes:
                    continue
                self.adj.setdefault(n["id"], []).append([rel, tid])
                self.adj.setdefault(tid, []).append(["~" + rel, n["id"]])
        self.reviews = {}
        self.vec_meta, self.vecs = [], b""
        self.embed_model, self.dim = EMBED_MODEL, EMBED_DIM
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE docs USING fts5(id, kind, text)")
        for nid, nd in self.nodes.items():
            p = nd["properties"]
            doc = " ".join(str(x) for x in [p.get("label"), *(p.get("aliases_ko") or []),
                                             p.get("text")] if x)
            con.execute("INSERT INTO docs VALUES(?,?,?)", (nid, nd["node_type"], doc))
        con.commit()
        self._fts_conn = con

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        if not text.startswith("---"):
            return {}, text
        end = text.find("\n---", 3)
        if end < 0:
            return {}, text
        head, body = text[3:end], text[end + 4:]
        fm: dict = {}
        key = None
        for ln in head.splitlines():
            m = re.match(r"^(\w+):\s*(.*)$", ln)
            if m:
                key = m.group(1)
                val = m.group(2).strip().strip('"')
                fm[key] = val if val else []
            elif key is not None and re.match(r"^\s+-\s+", ln):
                if not isinstance(fm.get(key), list):
                    fm[key] = []
                fm[key].append(ln.split("-", 1)[1].strip().strip('"'))
        return fm, body

    def _runtime_db(self):
        return sqlite3.connect(os.path.join(self.root, "runtime", "ontology.sqlite"))

    def _audit(self, action: str, detail: str, actor: str = "local"):
        if getattr(self, "_no_audit", False):
            return  # 노트팩은 읽기 전용 — 볼트에 runtime sqlite를 만들지 않는다
        try:
            with self._runtime_db() as c:
                c.execute("INSERT INTO audit_events(ts, actor, action, detail) VALUES(?,?,?,?)",
                          (_now(), actor, action, detail))
        except Exception:
            pass

    def status(self) -> dict:
        return {"pack_id": self.pack_id, "manifest_hash": self.manifest_hash,
                "mode": self.mode, "integrity": self.integrity,
                "baseline_compliance": self._baseline_summary(),
                "counts": {"nodes": len(self.nodes), "evidence": len(self.evidence),
                            "reviews": sum(len(v) for v in self.reviews.values()),
                            "vectors": len(self.vec_meta), "adjacency": len(self.adj)}}

    # --- 품질층 (T2 노출 · T3 재계산 대조) ---
    def _quality_docs(self) -> dict[str, dict]:
        import glob as _glob
        docs: dict[str, dict] = {}
        pats = [os.path.join(self.root, "quality", "*.json"),
                os.path.join(self.root, "*", "quality", "*.json"),
                os.path.join(self.root, "*", "*", "quality", "*.json")]
        for pat in pats:
            for p in _glob.glob(pat):
                if "__MACOSX" in p or os.path.basename(p).startswith("._"):
                    continue
                rel = os.path.relpath(p, self.root)
                try:
                    docs[rel] = json.load(open(p, encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    docs[rel] = {"_unparseable": True}
        return docs

    def _baseline_summary(self) -> dict:
        """status()용 요약. 부재 시 {present: false} — 구세대 팩 하위호환, 오류 금지.

        baseline-compliance.json(§6 스키마)이 있으면 발주 계약 형태
        {present, profile, passed, per_1000:{evidence,kinetic}}로 요약한다.
        """
        docs = self._quality_docs()
        if not docs:
            return {"present": False}
        comp = next((d for rel, d in docs.items()
                     if rel.endswith("baseline-compliance.json") and isinstance(d, dict)), None)
        if comp and "measured" in comp:
            per = (comp.get("measured") or {}).get("per_1000") or {}
            return {"present": True, "profile": comp.get("profile"),
                    "passed": comp.get("passed"),
                    "per_1000": {"evidence": per.get("evidence"), "kinetic": per.get("kinetic")},
                    "files": sorted(docs),
                    "verify": "pack_verify_baseline로 재계산 대조 (자기 신고는 신뢰하지 않음)"}
        checks: dict[str, bool] = {}
        for doc in docs.values():
            checks.update({k: bool(v) for k, v in (doc.get("checks") or {}).items()})
        return {"present": True, "files": sorted(docs),
                "declared_checks": {"true": sum(1 for v in checks.values() if v),
                                     "total": len(checks)},
                "verify": "pack_verify_baseline로 재계산 대조 (자기 신고는 신뢰하지 않음)"}

    _BASELINE_RECOMPUTABLE = ("all_edges_resolve", "unique_nodes", "unique_evidence",
                              "fts_count_matches_nodes", "openai_vector_materialized")

    def verify_baseline(self) -> dict:
        """quality/ 자기 신고를 nodes/edges/인덱스에서 재계산해 대조한다 (T3).

        신고를 그대로 믿지 않는다. 재계산과 어긋나면 FAIL(declared_mismatch, 조작 탐지),
        핵심 기준을 어기면 FAIL(below_baseline), 문서가 없으면 absent (오류 아님).
        neo4j·qmd 컬렉션 같은 환경 주장은 zip만으로 재계산 불가 — unverifiable로 표시만 한다.
        """
        docs = self._quality_docs()
        if not docs:
            return {"status": "absent", "present": False,
                    "note": "quality/ 문서가 없는 팩 (구세대 정본 하위호환) — 오류 아님"}
        nodes_p = _jsonl(self._read("nodes.jsonl"))
        edges_p = _jsonl(self._read("edges.jsonl"))
        node_ids = [n.get("id") for n in nodes_p]
        node_set = set(node_ids)
        ev_ids = [e.get("evidence_id") or e.get("id")
                  for e in _jsonl(self._read("evidence.jsonl"))]
        unresolved, touched = [], set()
        for e in edges_p:
            s, t = e.get("source") or e.get("from_id"), e.get("target") or e.get("to_id")
            touched.add(s)
            touched.add(t)
            if s not in node_set or t not in node_set:
                unresolved.append(f"{s}->{t}")
        fts_docs = None
        for cand in ("lexical-index/fts.sqlite", "indexes/lexical.sqlite"):
            p = os.path.join(self.root, cand)
            if os.path.exists(p):
                try:
                    con = sqlite3.connect(p)
                    fts_docs = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
                    con.close()
                except sqlite3.OperationalError:
                    pass
                break
        recomputed = {
            "count_nodes": len(nodes_p), "count_edges": len(edges_p),
            "all_edges_resolve": not unresolved,
            "unique_nodes": len(node_ids) == len(node_set),
            "unique_evidence": len(ev_ids) == len(set(ev_ids)),
            "fts_docs": fts_docs,
            "fts_count_matches_nodes": (fts_docs == len(nodes_p)) if fts_docs is not None else None,
            "openai_vector_materialized": bool(self.vec_meta) and self.dim == 3072,
        }
        mismatches, unverifiable = [], set()
        for rel, doc in docs.items():
            if doc.get("_unparseable"):
                mismatches.append({"file": rel, "check": "_parse",
                                   "declared": "json", "recomputed": "unparseable"})
                continue
            for k, declared in (doc.get("checks") or {}).items():
                if k in self._BASELINE_RECOMPUTABLE and recomputed.get(k) is not None:
                    if bool(declared) != bool(recomputed[k]):
                        mismatches.append({"file": rel, "check": k,
                                           "declared": bool(declared),
                                           "recomputed": recomputed[k]})
                else:
                    unverifiable.add(k)
            counts = doc.get("counts") or {}
            for k in ("nodes", "edges"):
                want = counts.get(k)
                if isinstance(want, int) and want != recomputed[f"count_{k}"]:
                    mismatches.append({"file": rel, "check": f"counts.{k}",
                                       "declared": want, "recomputed": recomputed[f"count_{k}"]})
        below = [k for k in ("all_edges_resolve", "unique_nodes", "unique_evidence")
                 if recomputed[k] is False]

        # ── §6 baseline-compliance.json 전용 재계산 대조 (발주 T3) ──
        # measured(evidence·kinetic·theme·claim·grammar_kinds·per_1000)를 nodes/edges
        # 재계산값과 대조하고, 기계 판정 게이트(고립 0·records 커버리지·밀도 하한)를 실측한다.
        baseline = None
        comp = next((d for rel, d in docs.items()
                     if rel.endswith("baseline-compliance.json") and isinstance(d, dict)
                     and "measured" in d), None)
        if comp:
            m = comp.get("measured") or {}
            counts = {
                "evidence": sum(1 for n in nodes_p if n.get("space") == "evidence"),
                "kinetic": sum(1 for n in nodes_p if n.get("space") == "kinetic"),
                "theme": sum(1 for n in nodes_p if n.get("node_type") in ("Theme", "Topic")),
                "claim": sum(1 for n in nodes_p if n.get("space") == "claim"),
                "grammar_kinds": len({e.get("relation") for e in edges_p if e.get("relation")}),
            }
            src_lines = comp.get("source_lines")
            per_rc = {}
            if isinstance(src_lines, (int, float)) and src_lines:
                per_rc = {"evidence": round(counts["evidence"] / src_lines * 1000, 1),
                          "kinetic": round(counts["kinetic"] / src_lines * 1000, 1)}
            for k, rc in counts.items():
                want = m.get(k)
                if isinstance(want, (int, float)) and int(want) != rc:
                    mismatches.append({"file": "quality/baseline-compliance.json",
                                       "check": f"measured.{k}", "declared": want, "recomputed": rc})
            decl_per = m.get("per_1000") or {}
            for k in ("evidence", "kinetic"):
                want = decl_per.get(k)
                if isinstance(want, (int, float)) and k in per_rc and abs(want - per_rc[k]) > 0.15:
                    mismatches.append({"file": "quality/baseline-compliance.json",
                                       "check": f"measured.per_1000.{k}",
                                       "declared": want, "recomputed": per_rc[k]})
            isolated = [nid for nid in node_ids if nid not in touched]
            kin_ids = [n.get("id") for n in nodes_p if n.get("space") == "kinetic"]
            ev_space = {n.get("id") for n in nodes_p if n.get("space") == "evidence"}
            rec_to = {e.get("target") or e.get("to_id") for e in edges_p
                      if e.get("relation") == "records"
                      and (e.get("source") or e.get("from_id")) in ev_space}
            uncovered_kin = [k_ for k_ in kin_ids if k_ not in rec_to]
            gate_items = [
                {"item": "isolated_nodes", "recomputed": len(isolated),
                 "verdict": "PASS" if not isolated else "FAIL"},
                {"item": "records_coverage",
                 "recomputed": f"{len(kin_ids) - len(uncovered_kin)}/{len(kin_ids)}",
                 "verdict": "PASS" if not uncovered_kin else "FAIL"},
            ]
            floor_map = {"theme": counts["theme"], "claim": counts["claim"],
                         "grammar_kinds": counts["grammar_kinds"],
                         "evidence": per_rc.get("evidence"),
                         "kinetic": per_rc.get("kinetic"),
                         "evidence_per_1000": per_rc.get("evidence"),
                         "kinetic_per_1000": per_rc.get("kinetic")}
            floor_unverified = []
            for k, want in (comp.get("floors") or {}).items():
                rc = floor_map.get(k)
                if rc is None or not isinstance(want, (int, float)):
                    floor_unverified.append(k)
                    continue
                gate_items.append({"item": f"floor.{k}", "floor": want, "recomputed": rc,
                                   "verdict": "PASS" if rc >= want else "FAIL"})
            # 게이트 7 (§4-7): locator 상한 — 라이선스·부록을 원문으로 서빙하는 것 차단
            unverified_gates = []
            body_end = comp.get("source_body_end_line")
            spans = []  # (id, start, end, chapter)
            for r_ in _jsonl(self._read("evidence.jsonl")):
                loc = r_.get("locator") if isinstance(r_.get("locator"), dict) else {}
                a_ = loc.get("start_line") or loc.get("line_start")
                b_ = loc.get("end_line") or loc.get("line_end")
                if isinstance(a_, int) and isinstance(b_, int):
                    spans.append((r_.get("id") or r_.get("evidence_id"), a_, b_,
                                  loc.get("chapter")))
            locator_over = []
            if isinstance(body_end, (int, float)) and spans:
                locator_over = [(i_, b_) for i_, a_, b_, c_ in spans if b_ > body_end]
                gate_items.append({"item": "locator_ceiling",
                                   "recomputed": f"침범 {len(locator_over)}/{len(spans)}"
                                                 f" (본문 종료 {int(body_end)}행)",
                                   "verdict": "PASS" if not locator_over else "FAIL"})
            elif spans and body_end is None:
                unverified_gates.append("locator_ceiling: source_body_end_line 미신고")
            elif isinstance(body_end, (int, float)) and not spans:
                unverified_gates.append("locator_ceiling: 수치 locator 없음")
            # 게이트 8 (§4-8): 등분 탐지 — 기계 등분(변동계수 비정상 저하)은 suspect.
            # 자동 FAIL이 아니라 정독 검수 필수 대상 표시 (화원 실사례: '장 기억' 요약).
            import statistics as _st
            by_ch: dict = {}
            for i_, a_, b_, c_ in spans:
                by_ch.setdefault(c_, []).append(b_ - a_ + 1)
            slicing_suspects = []
            for c_, sizes in by_ch.items():
                if len(sizes) >= 4:
                    mean = _st.fmean(sizes)
                    cv = (_st.pstdev(sizes) / mean) if mean else 0.0
                    if cv < 0.1:  # 자연 저작은 구간 크기가 크게 출렁인다 (임계 재조정 가능)
                        slicing_suspects.append({"chapter": c_, "n": len(sizes),
                                                 "cv": round(cv, 3)})
            gate_items.append({"item": "uniform_slicing",
                               "recomputed": f"suspect 장 {len(slicing_suspects)}",
                               "verdict": "SUSPECT" if slicing_suspects else "PASS"})
            below.extend(g["item"] for g in gate_items
                         if g["verdict"] == "FAIL" and g["item"] not in below)
            baseline = {"profile": comp.get("profile"), "declared_passed": comp.get("passed"),
                        "source_lines": src_lines,
                        "source_body_end_line": body_end,
                        "counts": counts, "per_1000": per_rc,
                        "gates": gate_items, "floor_unverified": floor_unverified,
                        "unverified_gates": unverified_gates,
                        "isolated_sample": isolated[:4],
                        "uncovered_kinetic_sample": uncovered_kin[:4],
                        "locator_over_sample": locator_over[:4],
                        "uniform_slicing_suspects": slicing_suspects[:6]}

        if mismatches:
            status, reason = "FAIL", "declared_mismatch"
        elif below:
            status, reason = "FAIL", "below_baseline"
        else:
            status, reason = "PASS", None
        return {"status": status, "present": True, "reason": reason,
                "files": sorted(docs), "recomputed": recomputed,
                "baseline": baseline,
                "mismatches": mismatches[:8], "below_baseline": below,
                "unverifiable_checks": sorted(unverifiable),
                "unresolved_edges_sample": unresolved[:4]}

    # --- retrieval 3종 ---
    @staticmethod
    def _fts_terms(toks: list[str]) -> list[str]:
        # 한국어 조사 대응: 각 토큰의 절단형(prefix*)도 함께 질의 ("프랑스혁명이" -> 프랑스혁명*)
        # 1글자 한글(소·개·활)은 정확 일치로만 질의 (prefix는 소음)
        terms = []
        for t in toks[:12]:
            terms.append(f'"{t}"')
            if len(t) == 1:
                continue
            for cut in (t[:-1], t[:-2]):
                if len(cut) >= 2:
                    terms.append(f'"{cut}" *'.replace('" *', '"*'))
                elif len(cut) == 1 and re.fullmatch(r"[가-힣]", cut):
                    terms.append(f'"{cut}"')
        return terms

    def lexical(self, q: str, k: int = 8) -> list[dict]:
        # 축 영수증: 실패를 빈 배열로 숨기지 않고 사유를 남긴다 (관측 계약)
        self._lexical_receipt = {"status": "ok", "hits": 0}
        toks = [t for t in re.findall(r"[\w가-힣]+", q)
                if len(t) >= 2 or re.fullmatch(r"[가-힣]", t)]
        if not toks:
            return []
        mem = getattr(self, "_fts_conn", None)
        if mem is not None:
            # 노트팩: 폴더 파싱 시 만든 인메모리 FTS가 zip의 fts.sqlite를 대체한다
            try:
                rows = mem.execute(
                    "SELECT id, kind, bm25(docs) FROM docs WHERE docs MATCH ? "
                    "ORDER BY bm25(docs) LIMIT ?",
                    (" OR ".join(dict.fromkeys(self._fts_terms(toks))), k)).fetchall()
            except sqlite3.OperationalError:
                rows = []
                self._lexical_receipt = {"status": "error: FTS 질의 실패", "hits": 0}
            if rows:
                self._lexical_receipt = {"status": "ok", "hits": len(rows)}
            return [{"id": r[0], "kind": r[1], "bm25": round(r[2], 3)} for r in rows]
        p = os.path.join(self.root, "lexical-index", "fts.sqlite")
        if not os.path.exists(p):
            p = os.path.join(self.root, "indexes", "lexical.sqlite")
        if not os.path.exists(p):
            self._lexical_receipt = {"status": "unavailable: lexical-index가 팩에 없음", "hits": 0}
            return []
        terms = self._fts_terms(toks)
        con = sqlite3.connect(p)
        # FTS 스키마는 팩 빌더마다 다르다: 구형은 (id, kind, body),
        # 신형은 (id, work_id, space, node_type, text). 없는 컬럼을 고정 참조하면
        # OperationalError가 나고 어휘 축이 통째로 조용히 죽는다 — 컬럼을 보고 고른다.
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(docs)")]
        except sqlite3.OperationalError:
            con.close()
            self._lexical_receipt = {"status": "error: FTS 스키마를 읽지 못함", "hits": 0}
            return []
        kind_col = next((c for c in ("kind", "node_type", "space") if c in cols), None)
        sel = f"id, {kind_col}, bm25(docs)" if kind_col else "id, '', bm25(docs)"
        try:
            rows = con.execute(
                f"SELECT {sel} FROM docs WHERE docs MATCH ? "
                "ORDER BY bm25(docs) LIMIT ?",
                (" OR ".join(dict.fromkeys(terms)), k)).fetchall()
        except sqlite3.OperationalError:
            rows = []
            self._lexical_receipt = {"status": "error: FTS 질의 실패", "hits": 0}
        con.close()
        if rows:
            self._lexical_receipt = {"status": "ok", "hits": len(rows)}
        return [{"id": r[0], "kind": r[1], "bm25": round(r[2], 3)} for r in rows]

    def _qmd_col_lazy(self) -> str | None:
        if getattr(self, "_qmd_col", "unset") == "unset":
            self._qmd_col = _qmd_ensure_collection(self.pack_id, self.nodes) if _qmd_available() else None
            # qmd는 URI에서 파일명의 특수문자를 '-'로 눌러 보여주므로 슬러그->실 id 역매핑 필요
            self._qmd_slug2id = {re.sub(r"[^a-z0-9]+", "-", nid.lower()): nid for nid in self.nodes}
        return self._qmd_col

    def _bge_sidecar(self) -> tuple[list[dict], bytes] | None:
        """구형 3072d 팩에도 사용자 로컬 bge-m3 캐시를 붙인다.

        정본 zip은 절대 수정하지 않는다. 캐시는 manifest hash에 묶이므로 팩이 바뀌면
        자동으로 새로 만든다. 이 경로가 없으면 기존 팩은 lexical+graph로만 답한다.
        """
        if EMBED_MODEL != "bge-m3":
            return None
        if getattr(self, "_bge_sidecar_loaded", False):
            return self._bge_sidecar_data
        self._bge_sidecar_loaded = True
        self._bge_sidecar_data = None
        key = self.manifest_hash or _sha256(self.pack_id.encode())
        os.makedirs(BGE_CACHE_DIR, exist_ok=True)
        meta_path = os.path.join(BGE_CACHE_DIR, f"{key}.json")
        vec_path = os.path.join(BGE_CACHE_DIR, f"{key}.bin")
        try:
            cached = json.load(open(meta_path, encoding="utf-8"))
            if (cached.get("model") == "bge-m3" and cached.get("dimension") == EMBED_DIM
                    and cached.get("manifest_hash") == self.manifest_hash):
                raw = open(vec_path, "rb").read()
                if len(raw) == len(cached["rows"]) * EMBED_DIM * 4:
                    self._bge_sidecar_data = (cached["rows"], raw)
                    return self._bge_sidecar_data
        except Exception:
            pass

        targets = [(nid, _node_embedding_text(n)) for nid, n in self.nodes.items()
                   if n.get("space") != "resource" and _node_embedding_text(n).strip()]
        rows, buf = [], io.BytesIO()
        for i in range(0, len(targets), 64):
            batch = targets[i:i + 64]
            vectors = _embed([text for _, text in batch], model="bge-m3")
            if vectors is None or len(vectors) != len(batch) or any(len(v) != EMBED_DIM for v in vectors):
                return None
            for (nid, text), vector in zip(batch, vectors):
                rows.append({"id": nid, "row": len(rows), "text_sha256": _sha256(text.encode())})
                buf.write(struct.pack(f"{EMBED_DIM}f", *vector))
        raw = buf.getvalue()
        payload = {"schema": 1, "model": "bge-m3", "dimension": EMBED_DIM,
                   "manifest_hash": self.manifest_hash, "rows": rows}
        try:
            tmp_meta = meta_path + ".tmp"
            tmp_vec = vec_path + ".tmp"
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            with open(tmp_vec, "wb") as f:
                f.write(raw)
            os.replace(tmp_meta, meta_path)
            os.replace(tmp_vec, vec_path)
        except Exception:
            return None
        self._bge_sidecar_data = (rows, raw)
        return self._bge_sidecar_data

    def _vector_space(self) -> tuple[list[dict], bytes, int, str] | None:
        if self.vec_meta and self.embed_model == EMBED_MODEL and self.dim == EMBED_DIM:
            return self.vec_meta, self.vecs, self.dim, "pack_vectors"
        sidecar = self._bge_sidecar()
        if sidecar:
            return sidecar[0], sidecar[1], EMBED_DIM, "omlx-bge-m3-sidecar"
        return None

    def vector(self, q: str, k: int = 8) -> list[dict]:
        # QMD는 명시적으로 선택한 호환 모드에서만 쓴다. 저장 벡터가 없다는 이유로
        # QMD로 바꾸면 bge-m3 팩이 경량화된 다른 임베딩 계약으로 조용히 바뀐다.
        if EMBED_MODEL == "qmd":
            col = self._qmd_col_lazy()
            if col:
                hits = _qmd_vector_search(col, q, k)
                s2i = getattr(self, "_qmd_slug2id", {})
                for h in hits:
                    h["id"] = s2i.get(re.sub(r"[^a-z0-9]+", "-", h["id"].lower()), h["id"])
                self._vector_receipt = {"status": "ok", "backend": "qmd", "hits": len(hits)}
                return hits
            self._vector_receipt = {"status": "unavailable: qmd 컬렉션 준비 실패",
                                    "backend": "qmd", "hits": 0}
        space = self._vector_space()
        if not space:
            self._vector_receipt = {
                "status": (f"unavailable: 쓸 수 있는 벡터 없음 (팩 {self.embed_model or '없음'}"
                           f"·{self.dim or 0}d, 런타임 {EMBED_MODEL}·{EMBED_DIM}d)"),
                "backend": None, "hits": 0}
            return []
        meta, vectors, dim, backend = space
        qv = _embed([q], model=EMBED_MODEL)
        if qv is None:
            self._vector_receipt = {"status": f"unavailable: 임베딩 백엔드({EMBED_MODEL}) 미응답",
                                    "backend": backend, "hits": 0}
            return []
        qv = qv[0]
        self._active_vector_backend = backend
        row = dim * 4
        best = []
        for m in meta:
            v = struct.unpack_from(f"{dim}f", vectors, m["row"] * row)
            best.append((sum(a * b for a, b in zip(qv, v)), m["id"]))
        best.sort(reverse=True)
        out = [{"id": nid, "cosine": round(s, 4)} for s, nid in best[:k]]
        self._vector_receipt = {"status": "ok", "backend": backend, "hits": len(out)}
        return out

    def _axis_receipts(self, gtrace=None, ctrace=None) -> dict:
        """축별 관측 영수증. 실패 축은 빈 배열이 아니라 사유로 보고한다.

        '조용히 죽는 고장'(예외를 삼켜 빈 결과가 성공처럼 보이는 것) 방지.
        KINGCRAB truth contract("empty/failed receipt는 성공으로 취급하지 않는다") 이식.
        """
        return {
            "lexical": getattr(self, "_lexical_receipt", {"status": "not_run", "hits": 0}),
            "vector": getattr(self, "_vector_receipt", {"status": "not_run", "hits": 0}),
            "graph": {"status": "ok", "path_nodes": len(gtrace or []),
                      "causal_paths": len(ctrace or [])},
        }

    def expand(self, seeds: list[str], hops: int = 3, cap: int = 40):
        visited, trace, frontier = set(seeds), [], list(seeds)
        for hop in range(hops):
            nxt = []
            for nid in frontier:
                for rel, other in self.adj.get(nid, [])[:20]:
                    if other in visited or len(visited) >= cap:
                        continue
                    visited.add(other); nxt.append(other)
                    trace.append({"hop": hop + 1, "from": nid, "rel": rel, "to": other})
            frontier = nxt
            if not frontier:
                break
        return visited, trace

    def causal_expand(self, seeds: list[str], hops: int = 4, cap: int = 240):
        """인과 축 확장. expand()와 달리 (1) 노드당 엣지를 자르지 않고 (2) 배관 관계를 건너뛰며
        (3) 인과 관계는 방향을 보존해 trace에 남기고 (4) Evidence에서 records/supports로
        Action·Claim을 답변 후보로 투영한다. 허브 노드(per:* 87엣지 등)에서 인과 체인이
        잘려나가던 문제의 해소가 목적이다."""
        visited, trace, frontier, traced = set(seeds), [], list(seeds), set()
        for hop in range(hops):
            nxt = []
            for nid in frontier:
                # 인과 > 투영(Claim·Evidence) > 진입(performs/involves) 순으로 훑는다.
                # 허브의 performs 72개가 먼저 예산을 먹어 Claim이 밀려나던 문제 방지.
                def _prio(e):
                    b = e[0].lstrip("~")
                    return 0 if b in CAUSAL_RELS else (1 if b in PROJECT_RELS else 2)
                for rel, other in sorted(self.adj.get(nid, []), key=_prio):
                    base = rel.lstrip("~")
                    if base in PLUMBING_RELS:
                        continue
                    is_causal = base in CAUSAL_RELS
                    # 인과·투영 관계, 그리고 인물/신 <-> 행위 진입로(performs/involves)만 탄다.
                    if not (is_causal or base in PROJECT_RELS
                            or base in ("performs", "involves")):
                        continue
                    # 두 끝점이 모두 lexical/vector seed여도 인과 화살표는 결과에 남아야 한다.
                    # 이전에는 visited 검사에 먼저 걸려 triggers가 사라지고, "왜" 답변에서
                    # 가장 중요한 kinetic trace가 빈 배열이 됐다.
                    edge_key = (nid, base, rel.startswith("~"), other)
                    if edge_key not in traced:
                        traced.add(edge_key)
                        trace.append({
                            "hop": hop + 1, "from": nid, "rel": base,
                            "direction": "reverse" if rel.startswith("~") else "forward",
                            "to": other,
                            "axis": "causal" if is_causal else "projection",
                            "from_label": (self.nodes.get(nid) or {}).get("label"),
                            "to_label": (self.nodes.get(other) or {}).get("label"),
                        })
                    if other in visited or len(visited) >= cap:
                        continue
                    visited.add(other)
                    nxt.append(other)
            frontier = nxt
            if not frontier:
                break
        return visited, trace

    def card(self, nid: str) -> dict | None:
        if nid in self.evidence:
            ev = self.evidence[nid]
            out = {"kind": "evidence",
                   **{k: ev.get(k) for k in ("evidence_id", "summary", "conditions",
                                               "limitations", "evidence_grade",
                                               "source_id", "source_locator")},
                   "review_status": self.reviews.get(nid, [])}
            # locator/source_path는 nodes.jsonl 쪽에만 있다 (evidence.jsonl은 요약만 보유)
            p = (self.nodes.get(nid) or {}).get("properties", {})
            for k in ("locator", "source_path"):
                if p.get(k) and not out.get(k):
                    out[k] = p[k]
            if not out.get("summary"):
                # 노드 쪽 복구: 구형은 properties.text, 신형 6권은 properties.summary
                out["summary"] = p.get("text") or p.get("summary") or p.get("short_quote")
            return out
        n = self.nodes.get(nid)
        if not n:
            return None
        p = n.get("properties", {})
        out = {"kind": f"node:{n.get('space')}", "id": nid,
               "label": n.get("label") or p.get("label"),
               "review_status": self.reviews.get(nid, [])}
        for key in ("definition", "statement", "text", "label_ko", "mechanism",
                     "applies_when", "tradeoffs",
                     "validation_probe", "evidence_refs", "measurement", "rule"):
            if p.get(key):
                out[key] = p[key]
        refs = list(p.get("evidence_refs") or [])
        # 인접 evidence(describes/mentions/exemplifies/supports 역방향)도 근거로 수집
        for rel, other in self.adj.get(nid, []):
            if rel.lstrip("~") in ("supports", "describes", "mentions", "exemplifies") \
                    and other in self.evidence and other not in refs:
                refs.append(other)
        if n.get("space") == "lever":
            out["governance"] = "local_validation_required"
        if refs:
            out["direct_evidence"] = [self.card(e) for e in refs if e in self.evidence][:4]
        return out

    def _rich_card(self, nid: str, score: float) -> dict:
        """답변 후보를 '이야기 가능한 카드'로 조립: 한국어 라벨·정의·원문 요약·관계 사슬 동봉.

        모델이 이 카드만 보고도 근거 인용 + 관계 서사를 쓸 수 있어야 한다.
        (라벨·id만 주면 모델이 자기 배경지식으로 채운다 - 온톨로지 답변이 아니게 됨)
        """
        c = self.card(nid) or {}
        c["id"] = nid
        c["score"] = round(score, 4)
        n = self.nodes.get(nid)
        if n:
            p = n.get("properties", {})
            if not c.get("label"):
                c["label"] = n.get("label") or p.get("label")
            for key in ("label_ko", "aliases_ko"):
                if p.get(key):
                    c[key] = p[key]
            # 관계 사슬: 이 후보가 그래프 어디에 걸려 있는지 (서사를 엮는 재료)
            rels = []
            for rel, other in self.adj.get(nid, []):
                on = self.nodes.get(other) or {}
                op = on.get("properties", {})
                rels.append({"relation": rel, "id": other,
                             "label": op.get("label_ko") or op.get("label") or on.get("label") or other})
                if len(rels) >= 6:
                    break
            if rels:
                c["relations"] = rels
        if isinstance(c.get("summary"), str):
            c["summary"] = c["summary"][:600]
        return c

    def _cos_threshold(self) -> float:
        # 실측 캘리브레이션: bge-m3는 유근거 0.78+/무근거 0.74-, 3-large는 0.43+/0.23-,
        # qmd(젬마 rerank %)는 유관 0.88+ 실측 - strong 컷 0.55
        if EMBED_MODEL == "qmd" or (not self.vec_meta and getattr(self, "_qmd_col", None)):
            return 0.55
        if EMBED_MODEL == "bge-m3":
            return 0.76
        return 0.35 if str(self.embed_model).startswith("text-embedding") else 0.76

    def _translate_query_en(self, q: str) -> str | None:
        """한글 질의를 영어로 1회 번역 (영문 말뭉치 재현율용). 키 없거나 영문 질의면 None."""
        if not re.search(r"[가-힣]", q):
            return None
        if os.environ.get("YUPACK_QUERY_TRANSLATE", "on") == "off":
            return None
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            return None
        try:
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                json.dumps({"model": os.environ.get("YUPACK_TRANSLATE_MODEL", "gpt-4o-mini"),
                            "temperature": 0,
                            "messages": [{"role": "user", "content":
                                "Translate this Korean search query into a short English search query. "
                                "Reply with the translation only.\n" + q}]}).encode(),
                {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            d = json.load(urllib.request.urlopen(req, timeout=20))
            t = (d["choices"][0]["message"]["content"] or "").strip()
            return t or None
        except Exception:
            return None

    def ask(self, question: str, top_k: int = 6) -> dict:
        thr = self._cos_threshold()
        # 인과 질문은 후보 창을 넓힌다: Claim·State가 evidence에 밀려 잘리던 문제
        causal = _is_causal_question(question)
        k_ret = 20 if causal else 12
        lex = self.lexical(question, k_ret)
        vec = self.vector(question, k_ret)
        # 한·영 이중 질의: 한글 질문은 영어 번역으로도 검색해 RRF에 합류 (영문 말뭉치 재현율)
        q_en = self._translate_query_en(question)
        lex_en = self.lexical(q_en, k_ret) if q_en else []
        vec_en = self.vector(q_en, k_ret) if q_en else []
        # 같은 id가 채널 안에서 두 번(노드행+근거행) 잡히면 첫 순위만 인정 (이중계상 방지)
        def _dedupe(hits):
            seen, out = set(), []
            for h in hits:
                if h["id"] in seen:
                    continue
                seen.add(h["id"])
                out.append(h)
            return out
        lex, vec = _dedupe(lex), _dedupe(vec)
        lex_en, vec_en = _dedupe(lex_en), _dedupe(vec_en)
        # RRF 융합: 한 채널의 절대점수 지배를 막고 여러 채널에 잡힌 후보를 위로
        RRF_K = 6
        score: dict[str, float] = {}
        for hits in (lex, lex_en):
            for i, h in enumerate(hits):
                score[h["id"]] = score.get(h["id"], 0) + 1.0 / (RRF_K + i)
        for hits in (vec, vec_en):
            for i, h in enumerate(hits):
                # 순위 기여는 임계값 없이(교차언어 질의는 절대 코사인이 낮게 나옴),
                # 임계값(thr)은 아래 근거 게이트(vec_strong) 판정에만 쓴다
                score[h["id"]] = score.get(h["id"], 0) + 1.0 / (RRF_K + i) + (0.05 if h["cosine"] >= thr else 0)
        ranked = sorted(score.items(), key=lambda kv: -kv[1])
        # 근거 게이트: 벡터(>=0.76) 또는 강한 어휘 일치(4자+ 토큰 정확 일치 / 2토큰 교집합)
        # 한글 1글자 단어(활·눈·신 등)는 의미어라 게이트 토큰에 포함한다
        def _gtok(s):
            return {t for t in re.findall(r"[\w가-힣]+", s)
                    if len(t) >= 2 or re.fullmatch(r"[가-힣]", t)}
        # 영어 번역 질의의 불용어는 게이트에서 뺀다: "the"/"for"가 3글자라 단독으로
        # 강한 어휘 일치 판정을 통과시키던 문제 (한글 3글자=의미어 규칙의 영어 오적용)
        q_toks = {t for t in (_gtok(question) | (_gtok(q_en) if q_en else set()))
                  if t.lower() not in _EN_STOPWORDS}
        lex_strong = False
        for h in lex + lex_en:
            c = self.card(h["id"]) or {}
            vals = [v for k0, v in c.items()
                    if isinstance(v, str) and k0 in _GATE_TEXT_KEYS]
            # 게이트는 인덱스와 같은 본문으로 판정한다: 노드 원본 속성(label_ko·aliases_ko 등) 포함
            n0 = (self.nodes.get(h["id"]) if isinstance(getattr(self, "nodes", None), dict) else None) or {}
            p0 = n0.get("properties", {}) or {}
            for k0, v in p0.items():
                # 게이트 본문은 "사람이 읽는 서술"만으로 만든다. 경로·식별자·빌드 스탬프
                # (actor-attribution-2026-08-11 등)가 질문 토큰과 겹쳐 게이트를 뚫던 문제를
                # 블랙리스트 대신 화이트리스트로 막는다 — 새 메타 필드가 생겨도 새지 않는다.
                if k0 not in _GATE_TEXT_KEYS:
                    continue
                if isinstance(v, str):
                    vals.append(v)
                elif isinstance(v, list):
                    vals += [x for x in v if isinstance(x, str)]
            body = " ".join(vals)
            d_toks = _gtok(body)
            # 조사 대응: 정확 일치 또는 전방일치(문서 토큰이 질문 토큰의 접두)로 겹침 판정.
            # 단, 1글자 토큰은 정확 일치만 인정한다 ("전"이 "전망은"에 걸리는 오탐 차단).
            def _hit(t: str) -> bool:
                if len(t) < 2:
                    return t in q_toks
                return any(qt == t or qt.startswith(t) or t.startswith(qt) for qt in q_toks)
            inter = {t for t in d_toks if _hit(t)}
            if len(inter) >= 2 or any(len(t) >= 3 for t in inter):
                lex_strong = True
                break
        vec_all = vec + vec_en
        vec_strong = bool(vec_all) and max(h["cosine"] for h in vec_all) >= thr
        if not ranked or not (vec_strong or lex_strong):
            self._audit("ask", f"no_local_evidence: {question[:80]}")
            return {"status": "no_local_evidence", "answer_guide": "팩에 근거가 없습니다. 답변은 \"팩에는 근거가 없다\"부터 밝힌 뒤에만 일반 지식으로 하세요.", "local_pack_id": self.pack_id,
                    "manifest_hash": self.manifest_hash,
                    "axis_receipts": self._axis_receipts(),
                    "message": "이 팩에는 질문을 뒷받침할 로컬 근거가 없습니다. "
                               "일반 지식/클라우드로 대체하지 마세요."}
        # 인과 질문은 시드를 넓게 잡는다. 어휘가 8건 쏟아지면 벡터 상위가 시드에서 밀려
        # 인과 경로 진입점을 놓친다 — 두 채널 상위가 모두 시드에 들어오게 한다.
        seeds = [nid for nid, _ in ranked[:(12 if causal else 4)]]
        visited, gtrace = self.expand(seeds, 3)
        ctrace: list[dict] = []
        if causal:
            cvisited, ctrace = self.causal_expand(seeds)
            visited |= cvisited
            # 그래프를 제3의 RRF 축으로 합류시킨다: 인과 답변의 보완축이지 장식이 아니다.
            corder, seen_c = [], set()
            for t in ctrace:
                if t["to"] not in seen_c:
                    seen_c.add(t["to"])
                    corder.append(t["to"])
            for i, nid in enumerate(corder):
                score[nid] = score.get(nid, 0.0) + 1.0 / (RRF_K + i + 1)
            ranked = sorted(score.items(), key=lambda kv: -kv[1])
        # 발견 순서(시드 -> 가까운 홉부터)로 카드화: 무작위 set 순서로 레버가 잘리는 문제 방지
        # 인과 경로 노드를 배관 evidence보다 앞세운다 (top_k를 evidence가 전부 점유하던 문제)
        # 인과 경로 위의 노드와 그 위에 걸린 Claim·Evidence를 최우선으로 카드화한다.
        # 인과 사슬 위의 노드, 그리고 "그 노드에 직접 걸린" Claim·Evidence만 앞세운다.
        # 투영 대상을 전부 끌어오면 팩 전체의 주제 Claim이 밀려들어 직답 Claim을 덮는다.
        chain_nodes = {t["from"] for t in ctrace if t["axis"] == "causal"} | \
                      {t["to"] for t in ctrace if t["axis"] == "causal"}
        prio = [t["to"] for t in ctrace if t["axis"] == "causal"]
        prio += [t["to"] for t in ctrace
                 if t["axis"] == "projection" and t["from"] in chain_nodes
                 and (self.nodes.get(t["to"], {}) or {}).get("space") in ("claim", "evidence")]
        # 인과 체인이 적은 팩에서는 어떤 "왜" 질문이든 같은 체인으로 걸어 들어가
        # 늘 같은 Claim이 1순위가 된다. 질문과의 어휘·벡터 근접도로 다시 세워
        # 체인 위에 있다는 이유만으로 무관한 Claim이 앞서지 않게 한다.
        prio.sort(key=lambda nid: -score.get(nid, 0.0))
        # 인과 질문에서는 인과 경로와 그 위의 Claim·Evidence가 시드보다 앞선다.
        # 어휘가 끌어온 주제 Claim이 top_k를 선점해 질문에 직답하는 Claim이 밀리던 문제.
        # 질문이 직접 끌어온 후보(seeds)가 먼저, 인과 체인에서 투영된 것(prio)이 다음.
        # prio를 앞세우면 체인이 3개뿐인 팩에서 어떤 "왜" 질문이든 같은 Claim이 1순위가 된다.
        head = list(seeds) + prio
        discovery = head + [t["to"] for t in ctrace] + [t["to"] for t in gtrace]
        seen_o = set()
        ordered = [n for n in discovery if not (n in seen_o or seen_o.add(n))]
        # 레버는 잘리지 않게 전부 뒤에 보강 (거리순 유지)
        ordered += [n for n in ordered if False]  # noop, 가독성
        lever_tail = [n for n in ordered if self.nodes.get(n, {}).get("space") == "lever"]
        window = top_k * 5 + (60 if causal else 0)
        cards = [c for c in (self.card(n) for n in (ordered[:window] + lever_tail)) if c]
        dedup = set()
        cards = [c for c in cards if not (c.get("id") in dedup or dedup.add(c.get("id")))]
        levers = [c for c in cards if c.get("kind") == "node:lever"]
        claims = [c for c in cards if c.get("kind") == "node:claim"]
        evs = [c for c in cards if c.get("kind") == "evidence"]
        # 그래프 인덱스가 없거나 관계가 비어 있어도, lexical/vector가 Concept·Claim을
        # 직접 찾고 그 노드가 evidence_refs를 갖고 있으면 grounded 답변은 가능하다.
        # 이 경우 근거가 후보 카드 안에만 숨으면 호출자가 locator를 놓칠 수 있으므로
        # 최상위 direct_evidence에도 같은 카드를 올린다. 그래프 부재는 거절 사유가 아니다.
        if not evs:
            seen_evidence = set()
            for c in cards:
                for ev in c.get("direct_evidence", []):
                    eid = ev.get("evidence_id")
                    if eid and eid not in seen_evidence:
                        seen_evidence.add(eid)
                        evs.append(ev)
        # 인과 질문에서는 Claim에 딸린 직접 근거를 최상위에도 앞세운다.
        # 그래야 호출자는 "왜"에 대한 증거(예: prayer)를 후보 Claim 안까지
        # 다시 파고들지 않고 확인할 수 있다. 그래프가 없어도 Claim의 evidence_refs
        # 투영은 그대로 작동한다.
        if causal and claims:
            claim_evidence, seen_evidence = [], set()
            for c in claims:
                for ev in c.get("direct_evidence", []):
                    eid = ev.get("evidence_id")
                    if eid and eid not in seen_evidence:
                        seen_evidence.add(eid)
                        claim_evidence.append(ev)
            evs = claim_evidence + [ev for ev in evs
                                    if ev.get("evidence_id") not in seen_evidence]
        # source/evidence 없는 lever는 추천으로 반환하지 않는다 (정책)
        levers = [l for l in levers if l.get("direct_evidence") or l.get("evidence_refs")]
        # 레버 보증: grounded인데 3홉 안에 레버가 없으면, 매칭 노드에서 BFS로 가장 가까운 레버를 찾는다
        if not levers:
            lever_ids = {nid for nid, n in self.nodes.items() if n.get("space") == "lever"}
            found, frontier, seen = [], list(seeds), set(seeds)
            for hop in range(6):
                nxt = []
                for nid in frontier:
                    for rel, other in self.adj.get(nid, []):
                        if other in seen:
                            continue
                        seen.add(other)
                        if other in lever_ids:
                            found.append(other)
                            gtrace.append({"hop": 4 + hop, "from": nid, "rel": rel,
                                            "to": other, "note": "lever_reach"})
                        nxt.append(other)
                if found or not nxt:
                    break
                frontier = nxt
            if not found and lever_ids:
                # 그래프로도 못 닿으면 레버 자체를 질문과 직접 대조 (어휘+벡터 순위)
                lv_rank = [h["id"] for h in self.lexical(question, 20) if h["id"] in lever_ids]
                if not lv_rank:
                    lv_rank = [h["id"] for h in self.vector(question, 20) if h["id"] in lever_ids]
                found = lv_rank[:2]
            levers = [c for c in (self.card(n) for n in found[:6]) if c]
            levers = [l for l in levers if l.get("direct_evidence") or l.get("evidence_refs")]
        # 질문-레버 전수 직접 대조 (레버는 수십 개뿐): 허브 편향 보정
        lever_ids_all = [nid for nid, n in self.nodes.items() if n.get("space") == "lever"]
        if lever_ids_all:
            q_toks_l = {t for t in re.findall(r"[\w가-힣]+", question) if len(t) >= 2}
            space = self._vector_space()
            qv = _embed([question], model=EMBED_MODEL) if space else None
            qv = qv[0] if qv else None
            meta, raw, dim, _ = space if space else ([], b"", EMBED_DIM, "none")
            row_of = {m["id"]: m["row"] for m in meta}
            scored_lv = []
            for nid in lever_ids_all:
                n = self.nodes[nid]
                p2 = n.get("properties", {})
                body = " ".join(str(x) for x in [n.get("label"), p2.get("label"),
                                                  p2.get("label_ko"),
                                                  p2.get("mechanism"), p2.get("applies_when")] if x)
                toks = {t for t in re.findall(r"[\w가-힣]+", body) if len(t) >= 2}
                inter = sum(1 for t in toks if any(
                    qt == t or qt.startswith(t) or t.startswith(qt) for qt in q_toks_l))
                sc = inter * 3.0
                if qv is not None and nid in row_of:
                    v = struct.unpack_from(f"{dim}f", raw, row_of[nid] * dim * 4)
                    sc += sum(a * b for a, b in zip(qv, v)) * 5.0
                scored_lv.append((sc, nid))
            scored_lv.sort(reverse=True)
            have = {l.get("id") for l in levers}
            for sc, nid in scored_lv[:3]:
                if nid not in have:
                    c = self.card(nid)
                    if c and (c.get("direct_evidence") or c.get("evidence_refs")):
                        levers.append(c)
        # 레버 재정렬: 그래프 거리보다 질문과의 의미 근접(토큰 겹침 + 벡터)이 앞선다
        if len(levers) > 1:
            q_toks2 = {t for t in re.findall(r"[\w가-힣]+", question) if len(t) >= 2}
            vec_rank = {h["id"]: i for i, h in enumerate(self.vector(question, 30))}
            def _lv_score(l):
                body = " ".join(str(l.get(k, "")) for k in ("label", "mechanism", "applies_when"))
                toks = {t for t in re.findall(r"[\w가-힣]+", body) if len(t) >= 2}
                inter = sum(1 for t in toks if any(
                    qt == t or qt.startswith(t) or t.startswith(qt) for qt in q_toks2))
                return (inter * 3) - vec_rank.get(l.get("id"), 99) * 0.1
            levers = sorted(levers, key=_lv_score, reverse=True)
        # source_id가 비어 있는 팩(원문 단일 소스)에서는 evidence 노드의 source_path로 대체
        src = {e.get("source_id") for e in evs if e.get("source_id")}
        if not src:
            src = {e.get("source_path") for e in evs if e.get("source_path")}
        sources = sorted(s for s in src if s)[:top_k]
        # 양 끝점이 seed면 같은 화살표가 정방향·역방향으로 모두 탐색될 수 있다.
        # 결과에는 하나만, 가능하면 원래 정방향을 보인다.
        causal_by_edge: dict[tuple[str, str, str], dict] = {}
        for t in ctrace:
            if t["axis"] != "causal":
                continue
            key = tuple(sorted((t["from"], t["to"]))) + (t["rel"],)
            item = {"from": t["from"], "from_label": t.get("from_label"),
                    "rel": t["rel"], "direction": t["direction"],
                    "to": t["to"], "to_label": t.get("to_label")}
            old = causal_by_edge.get(key)
            if old is None or (old["direction"] == "reverse" and item["direction"] == "forward"):
                causal_by_edge[key] = item
        causal_chains = list(causal_by_edge.values())
        causal_assessment = None
        if causal and not causal_chains:
            causal_assessment = {
                "status": "not_proven_from_local_graph",
                "message": "관련 로컬 근거는 있으나, 이 팩의 그래프에는 원인-결과 사슬이 확인되지 않습니다. 사실·해석만 답하고 인과를 단정하지 마세요.",
            }
        self._audit("ask", f"grounded: {question[:80]}")
        vec_best = max((h["cosine"] for h in vec_all), default=0.0)
        # strong은 어휘·의미 이중 확인: 일반 낱말 전방일치만으로 무관 질문이 strong이 되는 오발 방지
        strength = "strong" if lex_strong and vec_best >= thr else "weak"
        guide = ("답변 작성 규칙: 후보 카드의 summary를 인용하고 relations의 관계 사슬을 "
                 "따라 서사형으로 답하세요(단답 금지). 근거로 쓴 문장에는 근거 id를 표기하고, "
                 "팩 밖 배경지식은 '일반 지식'으로 구분하세요. 값이 빈 보고 축(등급·검수 등)은 "
                 "표기하지 말고 생략하세요. 답 끝에 relations의 이웃에서 이어갈 질문 1~2개를 "
                 "제안하세요.")
        if strength == "weak":
            guide = ("주의: 이 질문과 팩 근거의 결합이 약합니다(어휘 또는 의미 유사도 부족). "
                     "\"팩에 이 질문의 직접 근거는 뚜렷하지 않다\"를 먼저 밝히고, 후보 중 실제로 "
                     "관련 있는 것만 골라 답하거나 일반 지식으로 구분해 답하세요. ") + guide
        if causal_assessment:
            guide = causal_assessment["message"] + " " + guide
        return {
            "status": "grounded",
            "grounding": {"strength": strength, "lexical_match": bool(lex_strong),
                           "max_cosine": round(vec_best, 3)},
            "answer_guide": guide,
            "answer_candidates": [self._rich_card(nid, sc) for nid, sc in ranked[:top_k]],
            "graph_path": gtrace[:30],
            "matched_levers": levers[:top_k],
            "claims": claims[:top_k],
            "causal_chains": causal_chains,
            "causal_assessment": causal_assessment,
            "direct_evidence": evs[:top_k],
            "sources": sources,
            "conditions": [e.get("conditions") for e in evs if e.get("conditions")][:top_k],
            "limitations": [e.get("limitations") for e in evs if e.get("limitations")][:top_k],
            "review_status": {c["id"]: c["review_status"] for c in cards
                               if c.get("id") and c.get("review_status")},
            "retrieval_trace": {"lexical_hits": lex[:8], "vector_hits": vec[:5],
                                 # 정직한 폴백 표기 (발주 T6): 조용한 성능 저하 금지
                                 "mode": "hybrid" if vec else "lexical-only",
                                 "axis_receipts": self._axis_receipts(gtrace, ctrace),
                                 "graph_path": gtrace[:30],
                                 "causal_path": ctrace[:40],
                                 "question_intent": "causal" if causal else "factual",
                                 "rerank": f"RRF(k={RRF_K}) over lexical+vector"
                                           + ("+causal-graph" if causal else "")
                                           + f", cosine>={thr} ({self.embed_model} 실측 캘리브레이션)",
                                 "embedding": {
                                     "runtime_model": EMBED_MODEL,
                                     "runtime_dimension": EMBED_DIM,
                                     "pack_model": self.embed_model,
                                     "pack_dimension": self.dim,
                                     "backend": getattr(self, "_active_vector_backend",
                                                        "qmd" if EMBED_MODEL == "qmd" else "omlx-bge-m3"),
                                 }},
            "local_pack_id": self.pack_id,
            "manifest_hash": self.manifest_hash,
            "governance": {"decision_core": "local_validation_required",
                            "discovery": "search_only"},
        }


# ======================= 핸들 관리 =======================
def open_local(zip_path: str, mode: str = "read_only") -> dict:
    refresh_embed_model()
    pk = LocalPack(zip_path, mode)
    handle = f"pack_{pk.manifest_hash[:8]}"
    _HANDLES[handle] = pk
    return {"pack_handle": handle, **pk.status()}


def get(handle: str) -> LocalPack | None:
    return _HANDLES.get(handle)


def default_pack() -> "LocalPack | None":
    """마지막으로 연 팩. 없으면 YUPACK_AUTO_OPEN에서 연다."""
    if _HANDLES:
        return next(reversed(_HANDLES.values()))
    auto = os.environ.get("YUPACK_AUTO_OPEN")
    if auto and os.path.exists(os.path.expanduser(auto)):
        return _HANDLES[open_local(auto)["pack_handle"]]
    return None


def ask_all(question: str, top_k: int = 6) -> dict:
    """현재 열린 모든 팩에 질의해 가장 근거가 강한 팩의 답을 반환한다."""
    if not _HANDLES:
        auto = os.environ.get("YUPACK_AUTO_OPEN")
        if auto and os.path.exists(os.path.expanduser(auto)):
            open_local(auto)
    if not _HANDLES:
        return {"error": "열린 팩이 없습니다. pack_open_local(zip경로)로 팩을 먼저 여세요."}
    best = None
    for pk in _HANDLES.values():
        a = pk.ask(question, top_k)
        if a.get("status") != "grounded":
            continue
        top = a["answer_candidates"][0]["score"] if a.get("answer_candidates") else 0
        if best is None or top > best[0]:
            best = (top, a)
    if best:
        return best[1]
    return {"status": "no_local_evidence",
            "searched_packs": [pk.pack_id for pk in _HANDLES.values()],
            "message": "서재의 어느 팩에도 이 질문을 뒷받침할 근거가 없습니다. 팩 밖 지식으로 답하지 마세요."}


def close(handle: str) -> dict:
    pk = _HANDLES.pop(handle, None)
    if pk:
        pk._audit("close", "")
        return {"closed": handle}
    return {"error": f"핸들 없음: {handle}"}
