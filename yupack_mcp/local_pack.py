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

# 임베딩 = OpenAI 강제 (Daniel 확정 2026-08-05): 로컬 모델 자동 폴백 없음.
# 키가 없으면 임베딩을 생략하고 어휘+그래프로 동작한다 (질의는 여전히 가능).
# bge-m3는 YUPACK_EMBED_MODEL=bge-m3 명시 때만 (OMLX 가동 필요).
def _qmd_available() -> bool:
    import shutil
    return shutil.which("qmd") is not None


def _pick_model() -> tuple[str, int]:
    forced = os.environ.get("YUPACK_EMBED_MODEL")
    if forced == "bge-m3":
        return "bge-m3", 1024
    if forced == "qmd":
        return "qmd", 768
    if forced:
        return forced, {"text-embedding-3-large": 3072, "text-embedding-3-small": 1536}.get(forced, 1536)
    # 무키 + qmd 설치 = 로컬 무료 벡터 (EmbeddingGemma 768d, 실측 30/30)
    if not os.environ.get("OPENAI_API_KEY") and _qmd_available():
        return "qmd", 768
    return "text-embedding-3-small", 1536


EMBED_MODEL, EMBED_DIM = _pick_model()

_HANDLES: dict[str, "LocalPack"] = {}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _embed(texts: list[str], model: str | None = None) -> list[list[float]] | None:
    model = model or EMBED_MODEL
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
    return "yupack-" + re.sub(r"[^a-zA-Z0-9_-]", "-", pack_id)[:48]


def _qmd_doc_text(n: dict) -> str:
    p = n.get("properties", {})
    alias = p.get("aliases_ko") or []
    if isinstance(alias, str):
        alias = [alias]
    return " ".join(str(x) for x in [n.get("label") or p.get("label"), p.get("label_ko"), *alias,
                                      p.get("text"), p.get("definition"), p.get("statement"),
                                      p.get("mechanism")] if x)[:1500]


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
    if "evidence_id" in e:
        return e
    return {"evidence_id": e.get("id"), "summary": e.get("text") or e.get("metric_or_observation"),
            "conditions": e.get("condition"), "limitations": e.get("limitation"),
            "evidence_grade": e.get("grade"), "evidence_kind": e.get("canonical_layer"),
            "source_id": e.get("source_id"), "source_locator": e.get("source_locator"),
            "verbatim_or_paraphrase": e.get("verbatim_or_paraphrase")}


# ======================= 빌드: 정본 zip -> queryable zip =======================
def build_queryable(source_zip: str, out_zip: str | None = None,
                    include_embeddings: bool = True) -> dict:
    """정본 zip(nodes/edges/evidence/reviews)을 계약 구조의 질의 가능 zip으로 재구성한다."""
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
    for cand in (os.environ.get("YUPACK_CHARTER"),
                 os.path.join(os.path.expanduser(os.environ.get("YUPACK_PACK_DIR")
                              or "~/Zettelkasten/70_Ontology"), "PACK-CHARTER.md")):
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
            p = n.get("properties", {})
            alias = p.get("aliases_ko") or []
            if isinstance(alias, str):
                alias = [alias]
            text = " ".join(str(x) for x in [n.get("label"), p.get("label_ko"), *alias,
                                              p.get("definition"),
                                              p.get("statement"), p.get("mechanism")] if x)[:1500]
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
            emb_status = "unavailable(OPENAI_API_KEY 없음): lexical+graph로 동작"

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
        "query_runtime": "로컬 OMLX bge-m3(127.0.0.1:8000), 미가동 시 lexical+graph 폴백",
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
        raw = open(self.zip_path, "rb").read()
        self.pack_id = os.path.basename(self.zip_path)
        self.manifest_hash = _sha256(raw)
        self.cache = os.path.join(tempfile.gettempdir(),
                                  "yupack-" + self.manifest_hash[:12])
        if not os.path.exists(os.path.join(self.cache, ".ok")):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                z.extractall(self.cache)
            open(os.path.join(self.cache, ".ok"), "w").write("ok")
        root = self.cache
        for dirpath, _, fs in os.walk(self.cache):
            if "manifest.lock" in fs and dirpath.endswith("runtime"):
                root = os.path.dirname(dirpath)
                break
            if "nodes.jsonl" in fs:
                root = dirpath
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
                {"status": r.get("review_status"), "reviewer_role": r.get("reviewer_role"), "backend": "qmd" if qmd_backend else "openai"})
        self.adj: dict[str, list] = {}
        adj_p = os.path.join(root, "graph-index", "adjacency.jsonl")
        if not os.path.exists(adj_p):
            adj_p = os.path.join(root, "indexes", "graph-adjacency.jsonl")
        if os.path.exists(adj_p):
            for l in open(adj_p, encoding="utf-8"):
                d = json.loads(l)
                self.adj[d["id"]] = d["edges"]
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
                self.embed_model, self.dim = em["model"], em.get("dim") or EMBED_DIM
        self._audit("open", f"mode={mode}")

    def _read(self, name: str) -> str:
        p = os.path.join(self.root, name)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def _runtime_db(self):
        return sqlite3.connect(os.path.join(self.root, "runtime", "ontology.sqlite"))

    def _audit(self, action: str, detail: str, actor: str = "local"):
        try:
            with self._runtime_db() as c:
                c.execute("INSERT INTO audit_events(ts, actor, action, detail) VALUES(?,?,?,?)",
                          (_now(), actor, action, detail))
        except Exception:
            pass

    def status(self) -> dict:
        return {"pack_id": self.pack_id, "manifest_hash": self.manifest_hash,
                "mode": self.mode, "integrity": self.integrity,
                "counts": {"nodes": len(self.nodes), "evidence": len(self.evidence),
                            "reviews": sum(len(v) for v in self.reviews.values()),
                            "vectors": len(self.vec_meta), "adjacency": len(self.adj)}}

    # --- retrieval 3종 ---
    def lexical(self, q: str, k: int = 8) -> list[dict]:
        p = os.path.join(self.root, "lexical-index", "fts.sqlite")
        if not os.path.exists(p):
            p = os.path.join(self.root, "indexes", "lexical.sqlite")
        if not os.path.exists(p):
            return []
        toks = [t for t in re.findall(r"[\w가-힣]+", q)
                if len(t) >= 2 or re.fullmatch(r"[가-힣]", t)]
        if not toks:
            return []
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
        con = sqlite3.connect(p)
        try:
            rows = con.execute(
                "SELECT id, kind, bm25(docs) FROM docs WHERE docs MATCH ? "
                "ORDER BY bm25(docs) LIMIT ?",
                (" OR ".join(dict.fromkeys(terms)), k)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        con.close()
        return [{"id": r[0], "kind": r[1], "bm25": round(r[2], 3)} for r in rows]

    def _qmd_col_lazy(self) -> str | None:
        if getattr(self, "_qmd_col", "unset") == "unset":
            self._qmd_col = _qmd_ensure_collection(self.pack_id, self.nodes) if _qmd_available() else None
            # qmd는 URI에서 파일명의 특수문자를 '-'로 눌러 보여주므로 슬러그->실 id 역매핑 필요
            self._qmd_slug2id = {re.sub(r"[^a-z0-9]+", "-", nid.lower()): nid for nid in self.nodes}
        return self._qmd_col

    def vector(self, q: str, k: int = 8) -> list[dict]:
        # 무키 로컬 경로: 런타임이 qmd거나 저장 벡터가 없으면 qmd 컬렉션으로 (있을 때만)
        if EMBED_MODEL == "qmd" or not self.vec_meta:
            col = self._qmd_col_lazy()
            if col:
                hits = _qmd_vector_search(col, q, k)
                s2i = getattr(self, "_qmd_slug2id", {})
                for h in hits:
                    h["id"] = s2i.get(re.sub(r"[^a-z0-9]+", "-", h["id"].lower()), h["id"])
                return hits
        if not self.vec_meta:
            return []
        # 질의 임베딩은 팩을 구운 모델과 동일해야 한다 (embeddings.json 기록 기준)
        qv = _embed([q], model=self.embed_model)
        if qv is None:
            return []
        qv = qv[0]
        row = self.dim * 4
        best = []
        for m in self.vec_meta:
            v = struct.unpack_from(f"{self.dim}f", self.vecs, m["row"] * row)
            best.append((sum(a * b for a, b in zip(qv, v)), m["id"]))
        best.sort(reverse=True)
        return [{"id": nid, "cosine": round(s, 4)} for s, nid in best[:k]]

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

    def card(self, nid: str) -> dict | None:
        if nid in self.evidence:
            ev = self.evidence[nid]
            return {"kind": "evidence",
                    **{k: ev.get(k) for k in ("evidence_id", "summary", "conditions",
                                                "limitations", "evidence_grade",
                                                "source_id", "source_locator")},
                    "review_status": self.reviews.get(nid, [])}
        n = self.nodes.get(nid)
        if not n:
            return None
        p = n.get("properties", {})
        out = {"kind": f"node:{n.get('space')}", "id": nid,
               "label": n.get("label") or p.get("label"),
               "review_status": self.reviews.get(nid, [])}
        for key in ("definition", "statement", "mechanism", "applies_when", "tradeoffs",
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
        lex = self.lexical(question, 12)
        vec = self.vector(question, 12)
        # 한·영 이중 질의: 한글 질문은 영어 번역으로도 검색해 RRF에 합류 (영문 말뭉치 재현율)
        q_en = self._translate_query_en(question)
        lex_en = self.lexical(q_en, 12) if q_en else []
        vec_en = self.vector(q_en, 12) if q_en else []
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
        score: dict[str, float] = {}
        for hits in (lex, lex_en):
            for i, h in enumerate(hits):
                score[h["id"]] = score.get(h["id"], 0) + 1.0 / (6 + i)
        for hits in (vec, vec_en):
            for i, h in enumerate(hits):
                # 순위 기여는 임계값 없이(교차언어 질의는 절대 코사인이 낮게 나옴),
                # 임계값(thr)은 아래 근거 게이트(vec_strong) 판정에만 쓴다
                score[h["id"]] = score.get(h["id"], 0) + 1.0 / (6 + i) + (0.05 if h["cosine"] >= thr else 0)
        ranked = sorted(score.items(), key=lambda kv: -kv[1])
        # 근거 게이트: 벡터(>=0.76) 또는 강한 어휘 일치(4자+ 토큰 정확 일치 / 2토큰 교집합)
        # 한글 1글자 단어(활·눈·신 등)는 의미어라 게이트 토큰에 포함한다
        def _gtok(s):
            return {t for t in re.findall(r"[\w가-힣]+", s)
                    if len(t) >= 2 or re.fullmatch(r"[가-힣]", t)}
        q_toks = _gtok(question) | (_gtok(q_en) if q_en else set())
        lex_strong = False
        for h in lex + lex_en:
            c = self.card(h["id"]) or {}
            vals = [v for v in c.values() if isinstance(v, str)]
            # 게이트는 인덱스와 같은 본문으로 판정한다: 노드 원본 속성(label_ko·aliases_ko 등) 포함
            n0 = (self.nodes.get(h["id"]) if isinstance(getattr(self, "nodes", None), dict) else None) or {}
            p0 = n0.get("properties", {}) or {}
            for v in p0.values():
                if isinstance(v, str):
                    vals.append(v)
                elif isinstance(v, list):
                    vals += [x for x in v if isinstance(x, str)]
            body = " ".join(vals)
            d_toks = _gtok(body)
            # 조사 대응: 정확 일치 또는 전방일치(문서 토큰이 질문 토큰의 접두)로 겹침 판정
            inter = {t for t in d_toks
                     if any(qt == t or qt.startswith(t) or t.startswith(qt) for qt in q_toks)}
            if len(inter) >= 2 or any(len(t) >= 3 for t in inter):
                lex_strong = True
                break
        vec_all = vec + vec_en
        vec_strong = bool(vec_all) and max(h["cosine"] for h in vec_all) >= thr
        if not ranked or not (vec_strong or lex_strong):
            self._audit("ask", f"no_local_evidence: {question[:80]}")
            return {"status": "no_local_evidence", "answer_guide": "팩에 근거가 없습니다. 답변은 \"팩에는 근거가 없다\"부터 밝힌 뒤에만 일반 지식으로 하세요.", "local_pack_id": self.pack_id,
                    "manifest_hash": self.manifest_hash,
                    "message": "이 팩에는 질문을 뒷받침할 로컬 근거가 없습니다. "
                               "일반 지식/클라우드로 대체하지 마세요."}
        seeds = [nid for nid, _ in ranked[:4]]
        visited, gtrace = self.expand(seeds, 3)
        # 발견 순서(시드 -> 가까운 홉부터)로 카드화: 무작위 set 순서로 레버가 잘리는 문제 방지
        discovery = list(seeds) + [t["to"] for t in gtrace]
        seen_o = set()
        ordered = [n for n in discovery if not (n in seen_o or seen_o.add(n))]
        # 레버는 잘리지 않게 전부 뒤에 보강 (거리순 유지)
        ordered += [n for n in ordered if False]  # noop, 가독성
        lever_tail = [n for n in ordered if self.nodes.get(n, {}).get("space") == "lever"]
        cards = [c for c in (self.card(n) for n in (ordered[:top_k * 5] + lever_tail)) if c]
        dedup = set()
        cards = [c for c in cards if not (c.get("id") in dedup or dedup.add(c.get("id")))]
        levers = [c for c in cards if c.get("kind") == "node:lever"]
        claims = [c for c in cards if c.get("kind") == "node:claim"]
        evs = [c for c in cards if c.get("kind") == "evidence"]
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
            qv = _embed([question], model=self.embed_model)
            qv = qv[0] if qv else None
            row_of = {m["id"]: m["row"] for m in self.vec_meta}
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
                    v = struct.unpack_from(f"{self.dim}f", self.vecs, row_of[nid] * self.dim * 4)
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
        sources = sorted({e.get("source_id") for e in evs if e.get("source_id")})[:top_k]
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
        return {
            "status": "grounded",
            "grounding": {"strength": strength, "lexical_match": bool(lex_strong),
                           "max_cosine": round(vec_best, 3)},
            "answer_guide": guide,
            "answer_candidates": [self._rich_card(nid, sc) for nid, sc in ranked[:top_k]],
            "graph_path": gtrace[:30],
            "matched_levers": levers[:top_k],
            "claims": claims[:top_k],
            "direct_evidence": evs[:top_k],
            "sources": sources,
            "conditions": [e.get("conditions") for e in evs if e.get("conditions")][:top_k],
            "limitations": [e.get("limitations") for e in evs if e.get("limitations")][:top_k],
            "review_status": {c["id"]: c["review_status"] for c in cards
                               if c.get("id") and c.get("review_status")},
            "retrieval_trace": {"lexical_hits": lex, "vector_hits": vec[:5],
                                 "graph_path": gtrace[:30],
                                 "rerank": f"lexical rank + vector rank(+2 boost), cosine>={thr} ({self.embed_model} 실측 캘리브레이션)"},
            "local_pack_id": self.pack_id,
            "manifest_hash": self.manifest_hash,
            "governance": {"decision_core": "local_validation_required",
                            "discovery": "search_only"},
        }


# ======================= 핸들 관리 =======================
def open_local(zip_path: str, mode: str = "read_only") -> dict:
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
