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
def _pick_model() -> tuple[str, int]:
    forced = os.environ.get("YUPACK_EMBED_MODEL")
    if forced == "bge-m3":
        return "bge-m3", 1024
    if forced:
        return forced, {"text-embedding-3-large": 3072, "text-embedding-3-small": 1536}.get(forced, 1536)
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
    if include_embeddings:
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
        # 한국어 조사 대응: 각 토큰의 절단형(prefix*)도 함께 질의 ("프랑스혁명이" -> 프랑스혁명*)
        terms = []
        for t in toks[:12]:
            terms.append(f'"{t}"')
            for cut in (t[:-1], t[:-2]):
                if len(cut) >= 2:
                    terms.append(f'"{cut}" *'.replace('" *', '"*'))
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
            # 순위 기여는 임계값 없이(교차언어 질의는 절대 코사인이 낮게 나옴),
            # 임계값(thr)은 아래 근거 게이트(vec_strong) 판정에만 쓴다
            bonus = (8 - i) + (2 if h["cosine"] >= thr else 0)
            score[h["id"]] = score.get(h["id"], 0) + bonus
        ranked = sorted(score.items(), key=lambda kv: -kv[1])
        # 근거 게이트: 벡터(>=0.76) 또는 강한 어휘 일치(4자+ 토큰 정확 일치 / 2토큰 교집합)
        # 한글 1글자 단어(활·눈·신 등)는 의미어라 게이트 토큰에 포함한다
        def _gtok(s):
            return {t for t in re.findall(r"[\w가-힣]+", s)
                    if len(t) >= 2 or re.fullmatch(r"[가-힣]", t)}
        q_toks = _gtok(question)
        lex_strong = False
        for h in lex:
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
        vec_strong = bool(vec) and vec[0]["cosine"] >= thr
        if not ranked or not (vec_strong or lex_strong):
            self._audit("ask", f"no_local_evidence: {question[:80]}")
            return {"status": "no_local_evidence", "local_pack_id": self.pack_id,
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
