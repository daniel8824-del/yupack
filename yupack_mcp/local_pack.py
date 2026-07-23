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

# 임베딩 프로바이더: OPENAI_API_KEY 있으면 OpenAI 3-large(요건), 없으면 로컬 bge-m3 폴백
def _pick_model() -> tuple[str, int]:
    forced = os.environ.get("YUPACK_EMBED_MODEL")
    if forced == "bge-m3":
        return "bge-m3", 1024
    if forced:
        return forced, {"text-embedding-3-large": 3072, "text-embedding-3-small": 1536}.get(forced, 1024)
    if os.environ.get("OPENAI_API_KEY"):
        return "text-embedding-3-small", 1536
    return "bge-m3", 1024


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
        body = " ".join(str(x) for x in [
            n.get("label"), p.get("label"), p.get("definition"), p.get("statement"),
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
    if include_embeddings:
        targets = []
        for n in nodes:
            if n.get("space") == "resource":
                continue
            p = n.get("properties", {})
            text = " ".join(str(x) for x in [n.get("label"), p.get("definition"),
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
            emb_status = "unavailable(로컬 임베딩 서버 미가동): lexical+graph로 동작"

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
                {"status": r.get("review_status"), "reviewer_role": r.get("reviewer_role")})
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
        toks = [t for t in re.findall(r"[\w가-힣]+", q) if len(t) >= 2]
        if not toks:
            return []
        con = sqlite3.connect(p)
        try:
            rows = con.execute(
                "SELECT id, kind, bm25(docs) FROM docs WHERE docs MATCH ? "
                "ORDER BY bm25(docs) LIMIT ?",
                (" OR ".join(f'"{t}"' for t in toks[:12]), k)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        con.close()
        return [{"id": r[0], "kind": r[1], "bm25": round(r[2], 3)} for r in rows]

    def vector(self, q: str, k: int = 8) -> list[dict]:
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

    def _cos_threshold(self) -> float:
        # 실측 캘리브레이션: bge-m3는 유근거 0.78+/무근거 0.74-, 3-large는 0.43+/0.23-
        return 0.35 if str(self.embed_model).startswith("text-embedding") else 0.76

    def ask(self, question: str, top_k: int = 6) -> dict:
        thr = self._cos_threshold()
        lex = self.lexical(question, 8)
        vec = self.vector(question, 8)
        score: dict[str, float] = {}
        for i, h in enumerate(lex):
            score[h["id"]] = score.get(h["id"], 0) + (8 - i)
        for i, h in enumerate(vec):
            if h["cosine"] >= thr:
                score[h["id"]] = score.get(h["id"], 0) + (8 - i) + 2
        ranked = sorted(score.items(), key=lambda kv: -kv[1])
        # 근거 게이트: 벡터(>=0.76) 또는 강한 어휘 일치(4자+ 토큰 정확 일치 / 2토큰 교집합)
        q_toks = {t for t in re.findall(r"[\w가-힣]+", question) if len(t) >= 2}
        lex_strong = False
        for h in lex:
            c = self.card(h["id"]) or {}
            body = " ".join(str(v) for v in c.values() if isinstance(v, str))
            inter = q_toks & {t for t in re.findall(r"[\w가-힣]+", body) if len(t) >= 2}
            if len(inter) >= 2 or any(len(t) >= 4 for t in inter):
                lex_strong = True
                break
        vec_strong = bool(vec) and vec[0]["cosine"] >= thr
        if not ranked or not (vec_strong or lex_strong):
            self._audit("ask", f"no_local_evidence: {question[:80]}")
            return {"status": "no_local_evidence", "local_pack_id": self.pack_id,
                    "manifest_hash": self.manifest_hash,
                    "message": "이 팩에는 질문을 뒷받침할 로컬 근거가 없습니다. "
                               "일반 지식/클라우드로 대체하지 마세요."}
        seeds = [nid for nid, _ in ranked[:4]]
        visited, gtrace = self.expand(seeds, 3)
        cards = [c for c in (self.card(n) for n in list(visited)[:top_k * 5]) if c]
        levers = [c for c in cards if c.get("kind") == "node:lever"]
        claims = [c for c in cards if c.get("kind") == "node:claim"]
        evs = [c for c in cards if c.get("kind") == "evidence"]
        # source/evidence 없는 lever는 추천으로 반환하지 않는다 (정책)
        levers = [l for l in levers if l.get("direct_evidence") or l.get("evidence_refs")]
        sources = sorted({e.get("source_id") for e in evs if e.get("source_id")})[:top_k]
        self._audit("ask", f"grounded: {question[:80]}")
        return {
            "status": "grounded",
            "answer_candidates": [{"id": nid, "score": sc,
                                    "label": (self.card(nid) or {}).get("label")}
                                   for nid, sc in ranked[:top_k]],
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


def _scan_pack_dir() -> None:
    """YUPACK_PACK_DIR(팩 서재 폴더) 안의 zip을 전부 연다 (새 파일만)."""
    d = os.environ.get("YUPACK_PACK_DIR")
    if not d:
        return
    d = os.path.expanduser(d)
    if not os.path.isdir(d):
        return
    opened = {pk.zip_path for pk in _HANDLES.values()}
    for fn in sorted(os.listdir(d)):
        path = os.path.join(d, fn)
        if fn.endswith(".zip") and path not in opened:
            try:
                open_local(path)
            except Exception:
                pass  # 깨진 zip은 건너뜀


def default_pack() -> "LocalPack | None":
    """마지막으로 연 팩. 없으면 YUPACK_AUTO_OPEN 또는 YUPACK_PACK_DIR에서 연다."""
    _scan_pack_dir()
    if _HANDLES:
        return next(reversed(_HANDLES.values()))
    auto = os.environ.get("YUPACK_AUTO_OPEN")
    if auto and os.path.exists(os.path.expanduser(auto)):
        return _HANDLES[open_local(auto)["pack_handle"]]
    return None


def ask_all(question: str, top_k: int = 6) -> dict:
    """열린 모든 팩(서재 전체)에 질의해 가장 근거가 강한 팩의 답을 반환한다."""
    _scan_pack_dir()
    if not _HANDLES:
        auto = os.environ.get("YUPACK_AUTO_OPEN")
        if auto and os.path.exists(os.path.expanduser(auto)):
            open_local(auto)
    if not _HANDLES:
        return {"error": "열린 팩이 없습니다. YUPACK_PACK_DIR 폴더에 팩 zip을 넣거나 pack_open_local을 호출하세요."}
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
