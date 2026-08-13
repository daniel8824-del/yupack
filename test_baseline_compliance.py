"""품질층 보존·노출·재계산 대조 회귀 (발주서 T1~T3, 2026-08-13).

인수 4케이스: 정상 PASS · 조작 FAIL(declared_mismatch) · 미달 FAIL(below_baseline)
· 부재 absent(구세대 팩 하위호환, 오류 금지). + T1(build_queryable이 quality/ 보존)
+ 실팩 전수(기존 11팩을 깨뜨리지 않음: PASS 또는 absent만 허용).
"""
import glob
import json
import os
import tempfile
import zipfile

import pytest

from yupack_mcp.local_pack import LocalPack, build_queryable

NODES = [
    {"id": "per:a", "space": "concept", "node_type": "Person",
     "properties": {"label": "인물 A", "text": "인물 A 설명"}},
    {"id": "ev:1", "space": "evidence", "node_type": "TextUnit",
     "properties": {"label": "근거 1", "text": "근거 본문", "source_locator": "1행"}},
]
EDGES = [{"source": "ev:1", "target": "per:a", "relation": "describes", "properties": {}}]
EVIDENCE = [{"evidence_id": "ev:1", "summary": "근거 본문", "source_locator": "1행"}]


def _src_zip(path, *, quality=None, edges=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("nodes.jsonl", "\n".join(json.dumps(n, ensure_ascii=False) for n in NODES))
        z.writestr("edges.jsonl", "\n".join(json.dumps(e, ensure_ascii=False)
                                            for e in (edges if edges is not None else EDGES)))
        z.writestr("evidence.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in EVIDENCE))
        z.writestr("reviews.jsonl", "")
        if quality is not None:
            z.writestr("quality/finalization.json", json.dumps(quality, ensure_ascii=False))
    return str(path)


def test_t1_build_queryable_preserves_quality_and_t2_status_exposes(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    src = _src_zip(tmp_path / "src.zip",
                   quality={"checks": {"all_edges_resolve": True, "unique_nodes": True},
                            "counts": {"nodes": 2, "edges": 1}})
    out = str(tmp_path / "out.zip")
    r = build_queryable(src, out, include_embeddings=False)
    assert "error" not in r
    with zipfile.ZipFile(out) as z:
        assert "quality/finalization.json" in z.namelist()  # T1 보존
    lp = LocalPack(out)
    assert lp.integrity == "ok"  # 품질층이 무결성 해시 범위 안에 있다
    bc = lp.status()["baseline_compliance"]  # T2 노출
    assert bc["present"] is True and "quality/finalization.json" in bc["files"]
    v = lp.verify_baseline()
    assert v["status"] == "PASS", v


def test_t3_tampered_declaration_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    # 자기 신고가 사실과 다르다: counts.nodes=99 (실제 2)
    src = _src_zip(tmp_path / "src.zip",
                   quality={"checks": {"all_edges_resolve": True},
                            "counts": {"nodes": 99, "edges": 1}})
    lp = LocalPack(src)
    v = lp.verify_baseline()
    assert v["status"] == "FAIL" and v["reason"] == "declared_mismatch"
    assert any(m["check"] == "counts.nodes" for m in v["mismatches"])


def test_t3_below_baseline_fails_even_when_honest(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    # 정직하게 false로 신고했어도, 기준 자체를 어기면 FAIL이다
    broken = EDGES + [{"source": "ev:1", "target": "per:ghost", "relation": "describes",
                       "properties": {}}]
    src = _src_zip(tmp_path / "src.zip", edges=broken,
                   quality={"checks": {"all_edges_resolve": False}})
    lp = LocalPack(src)
    v = lp.verify_baseline()
    assert v["status"] == "FAIL" and v["reason"] == "below_baseline"
    assert "all_edges_resolve" in v["below_baseline"]
    assert not v["mismatches"]  # 신고는 정직했다 — 조작이 아니라 미달


def test_t3_absent_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    src = _src_zip(tmp_path / "src.zip", quality=None)
    lp = LocalPack(src)
    assert lp.status()["baseline_compliance"] == {"present": False}
    v = lp.verify_baseline()
    assert v["status"] == "absent" and v["present"] is False


def test_existing_final_packs_never_fail(monkeypatch):
    """기존 정본 팩을 깨뜨리는 구현은 실패다 — 전수에서 PASS 또는 absent만 허용."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    paths = sorted(p for p in glob.glob(
        "/Users/yedulab/Zettelkasten/70_Ontology/**/*-pack-final-*.zip", recursive=True)
        if "_archive" not in p)
    if not paths:
        pytest.skip("정본 팩 없음")
    bad = []
    for p in paths:
        v = LocalPack(p).verify_baseline()
        if v["status"] == "FAIL":
            bad.append((os.path.basename(p), v["reason"],
                        [m["check"] for m in v["mismatches"]][:3], v["below_baseline"]))
    assert not bad, bad
