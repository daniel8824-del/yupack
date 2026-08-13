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


# ── §6 baseline-compliance.json 스키마 (발주 전문 기준) ──
SPEC_NODES = [
    {"id": "ev:1", "space": "evidence", "node_type": "TextUnit",
     "properties": {"label": "근거 1", "text": "근거 본문 하나", "source_locator": "1행"}},
    {"id": "ev:2", "space": "evidence", "node_type": "TextUnit",
     "properties": {"label": "근거 2", "text": "근거 본문 둘", "source_locator": "2행"}},
    {"id": "kin:1", "space": "kinetic", "node_type": "Event", "properties": {"label": "사건 1"}},
    {"id": "theme:1", "space": "concept", "node_type": "Topic",
     "properties": {"label": "주제 1", "definition": "주제"}},
    {"id": "claim:1", "space": "claim", "node_type": "Claim",
     "properties": {"label": "주장 1", "text": "주장", "evidence_refs": ["ev:1"]}},
]
SPEC_EDGES = [
    {"source": "ev:1", "target": "kin:1", "relation": "records", "properties": {}},
    {"source": "ev:2", "target": "claim:1", "relation": "supports", "properties": {}},
    {"source": "ev:1", "target": "theme:1", "relation": "describes", "properties": {}},
]
SPEC_EVIDENCE = [{"evidence_id": "ev:1", "summary": "근거 본문 하나", "source_locator": "1행"},
                 {"evidence_id": "ev:2", "summary": "근거 본문 둘", "source_locator": "2행"}]


def _compliance(**over):
    base = {
        "profile": "서사류", "source_lines": 1000, "source_sha256": "deadbeef",
        "measured": {"evidence": 2, "kinetic": 1, "theme": 1, "claim": 1,
                     "grammar_kinds": 3, "per_1000": {"evidence": 2.0, "kinetic": 1.0}},
        "floors": {"evidence_per_1000": 1, "kinetic_per_1000": 0.5,
                   "theme": 1, "claim": 1, "grammar_kinds": 2},
        "gates": {"isolated_nodes": 0, "records_coverage": "1/1"},
        "passed": True,
    }
    base.update(over)
    return base


def _spec_zip(path, compliance, *, nodes=None, edges=None, evidence=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("nodes.jsonl", "\n".join(json.dumps(n, ensure_ascii=False)
                                            for n in (nodes or SPEC_NODES)))
        z.writestr("edges.jsonl", "\n".join(json.dumps(e, ensure_ascii=False)
                                            for e in (edges or SPEC_EDGES)))
        z.writestr("evidence.jsonl", "\n".join(json.dumps(e, ensure_ascii=False)
                                               for e in (evidence or SPEC_EVIDENCE)))
        z.writestr("reviews.jsonl", "")
        z.writestr("quality/baseline-standard.snapshot.md", "# 기준 스냅샷 (저작 시점 사본)")
        z.writestr("quality/baseline-compliance.json", json.dumps(compliance, ensure_ascii=False))
    return str(path)


def test_spec_schema_normal_pass_and_summary_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    lp = LocalPack(_spec_zip(tmp_path / "s.zip", _compliance()))
    v = lp.verify_baseline()
    assert v["status"] == "PASS", v
    assert v["baseline"]["profile"] == "서사류"
    assert all(g["verdict"] == "PASS" for g in v["baseline"]["gates"])
    bc = lp.status()["baseline_compliance"]  # T2 발주 형태
    assert bc["present"] is True and bc["profile"] == "서사류" and bc["passed"] is True
    assert bc["per_1000"] == {"evidence": 2.0, "kinetic": 1.0}


def test_spec_schema_tampered_measured_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    c = _compliance()
    c["measured"] = {**c["measured"], "kinetic": 9,
                     "per_1000": {"evidence": 2.0, "kinetic": 9.0}}
    v = LocalPack(_spec_zip(tmp_path / "s.zip", c)).verify_baseline()
    assert v["status"] == "FAIL" and v["reason"] == "declared_mismatch"
    assert any(m["check"] == "measured.kinetic" for m in v["mismatches"])


def test_spec_schema_below_floor_fails_honest(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    c = _compliance(floors={"kinetic_per_1000": 50}, passed=False)
    v = LocalPack(_spec_zip(tmp_path / "s.zip", c)).verify_baseline()
    assert v["status"] == "FAIL" and v["reason"] == "below_baseline"
    assert "floor.kinetic_per_1000" in v["below_baseline"]
    assert not v["mismatches"]  # 신고는 정직 — 조작이 아니라 미달


def test_spec_gates_isolated_and_records_coverage_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    nodes = SPEC_NODES + [
        {"id": "orphan:1", "space": "concept", "node_type": "Concept",
         "properties": {"label": "고립 노드"}},
        {"id": "kin:2", "space": "kinetic", "node_type": "Event",
         "properties": {"label": "근거 없는 사건"}},
    ]
    edges = SPEC_EDGES + [
        {"source": "kin:2", "target": "theme:1", "relation": "about", "properties": {}}]
    c = _compliance()
    c["measured"] = {**c["measured"], "kinetic": 2,
                     "grammar_kinds": 4, "per_1000": {"evidence": 2.0, "kinetic": 2.0}}
    v = LocalPack(_spec_zip(tmp_path / "s.zip", c, nodes=nodes, edges=edges)).verify_baseline()
    assert v["status"] == "FAIL" and v["reason"] == "below_baseline"
    assert "isolated_nodes" in v["below_baseline"]
    assert "records_coverage" in v["below_baseline"]
    assert v["baseline"]["isolated_sample"] == ["orphan:1"]
    assert v["baseline"]["uncovered_kinetic_sample"] == ["kin:2"]


def test_gate7_locator_ceiling_blocks_license_overrun(tmp_path, monkeypatch):
    """§4-7: Evidence locator가 본문 종료행을 넘으면 FAIL (구텐베르크 라이선스 침범 실사례)."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    ev = [{"evidence_id": "ev:1", "summary": "본문 안",
           "locator": {"chapter": "I", "start_line": 10, "end_line": 90}},
          {"evidence_id": "ev:2", "summary": "라이선스 침범",
           "locator": {"chapter": "II", "start_line": 95, "end_line": 150}}]
    c = _compliance(source_body_end_line=100)
    v = LocalPack(_spec_zip(tmp_path / "s.zip", c, evidence=ev)).verify_baseline()
    assert v["status"] == "FAIL" and "locator_ceiling" in v["below_baseline"]
    assert v["baseline"]["locator_over_sample"] == [["ev:2", 150]] or \
        v["baseline"]["locator_over_sample"] == [("ev:2", 150)]


def test_gate8_uniform_slicing_is_suspect_not_fail(tmp_path, monkeypatch):
    """§4-8: 기계 등분 패턴(변동계수 비정상 저하)은 suspect 표시 — 자동 FAIL 아님."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    ev = [{"evidence_id": f"ev:{i}", "summary": f"등분 {i}",
           "locator": {"chapter": "I", "start_line": 1 + i * 20, "end_line": 20 + i * 20}}
          for i in range(1, 6)]  # 정확히 20행 등분 5개 → CV 0
    c = _compliance(source_body_end_line=1000)
    v = LocalPack(_spec_zip(tmp_path / "s.zip", c, evidence=ev)).verify_baseline()
    assert v["status"] == "PASS", v  # suspect는 실패가 아니다
    assert v["baseline"]["uniform_slicing_suspects"], "등분 패턴이 suspect로 안 잡혔다"
    assert any(g["item"] == "uniform_slicing" and g["verdict"] == "SUSPECT"
               for g in v["baseline"]["gates"])


def test_t1_preserves_both_quality_files_and_t6_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    src = _spec_zip(tmp_path / "src.zip", _compliance())
    out = str(tmp_path / "out.zip")
    r = build_queryable(src, out, include_embeddings=False)
    assert "error" not in r
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "quality/baseline-compliance.json" in names
        assert "quality/baseline-standard.snapshot.md" in names  # md도 보존 (2종 계약)
    lp = LocalPack(out)
    assert lp.integrity == "ok"
    assert lp.verify_baseline()["status"] == "PASS"
    ans = lp.ask("근거 본문 하나", 3)
    # 발주 T6: 벡터 없는 환경에서 조용한 저하 금지 — 모드 명시
    assert ans["retrieval_trace"]["mode"] == "lexical-only"


def test_existing_final_packs_no_false_tamper(monkeypatch):
    """기존 팩을 깨뜨리는 구현은 실패다 — 전수에서 예외 0 + 거짓 조작 판정 0.

    정직하게 하한을 미달한 팩(예: 증보 웨이브의 수리 라운드 중간물)이 below_baseline으로
    떨어지는 것은 도구가 제 일을 한 것이다 — 그건 허용하고, declared_mismatch(조작 판정)가
    실팩에서 나오면 재계산기의 오탐이므로 실패로 본다.
    """
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    paths = sorted(p for p in glob.glob(
        "/Users/yedulab/Zettelkasten/70_Ontology/**/*-pack-final-*.zip", recursive=True)
        if "_archive" not in p)
    if not paths:
        pytest.skip("정본 팩 없음")
    false_tamper, honest_fails = [], []
    for p in paths:
        v = LocalPack(p).verify_baseline()  # 예외가 나면 그 자체로 실패
        if v["status"] == "FAIL" and v["reason"] == "declared_mismatch":
            false_tamper.append((os.path.basename(p),
                                 [m["check"] for m in v["mismatches"]][:3]))
        elif v["status"] == "FAIL":
            honest_fails.append((os.path.basename(p), v["below_baseline"][:3]))
    assert not false_tamper, false_tamper
    # 정직한 미달은 정보로만 (증보 중간물 등) — 존재 자체는 실패가 아니다


# ── 게이트9 + suspect + 봉인 (발주 2026-08-13: prompt-yupack-authoring-quality-gates) ──

SRC_TEXT = "\n".join([
    "제1장",
    "오디세우스는 트로이에서 돌아오는 길에 폭풍을 만났다.",
    "바다의 신이 그의 귀향을 십 년 동안 막았다.",
    "제2장",
    "그는 이름을 밝힌 대가로 표류를 얻었다.",
])


def _anchor_zip(path, evidence, *, anchor=None, quality=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("nodes.jsonl", "\n".join(json.dumps(n, ensure_ascii=False) for n in NODES))
        z.writestr("edges.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in EDGES))
        z.writestr("evidence.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in evidence))
        z.writestr("reviews.jsonl", "")
        z.writestr("quality/finalization.json", json.dumps(
            quality or {"checks": {"all_edges_resolve": True}, "counts": {"nodes": 2, "edges": 1}},
            ensure_ascii=False))
        if anchor is not None:
            z.writestr("quality/anchor-check.json", json.dumps(anchor, ensure_ascii=False))
    return str(path)


def test_gate9_quote_in_span_recomputes_and_catches_memory_quote(tmp_path, monkeypatch):
    """원문 대조: 스팬 안 인용은 PASS, 기억 인용(스팬 밖)은 그 한 방으로 FAIL."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    src_txt = tmp_path / "source.txt"
    src_txt.write_text(SRC_TEXT, encoding="utf-8")
    good = {"evidence_id": "ev:1", "summary": "귀향 방해",
            "locator": {"start_line": 2, "end_line": 3},
            "short_quote": "바다의 신이 그의 귀향을 십 년 동안 막았다."}
    z = _anchor_zip(tmp_path / "ok.zip", [good])
    v = LocalPack(z).verify_baseline(str(src_txt))
    assert v["anchor"]["mode"] == "recomputed"
    assert v["anchor"] == {**v["anchor"], "checked": 1, "passed": 1}
    assert v["status"] == "PASS", v
    # 기억 인용: 원문에 없는 문장을 같은 스팬에 선언
    bad = {**good, "short_quote": "그는 바다를 저주하며 울부짖었다."}
    z2 = _anchor_zip(tmp_path / "bad.zip", [bad])
    v2 = LocalPack(z2).verify_baseline(str(src_txt))
    assert v2["status"] == "FAIL" and "quote_in_span" in v2["below_baseline"]
    assert v2["anchor"]["failures"][0]["id"] == "ev:1"


def test_gate9_declared_modes_green_red_absent(tmp_path, monkeypatch):
    """원문 없이: 내장 anchor-check green은 신뢰(정직 표기), red는 FAIL, 부재는 하위호환."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    ev = [{"evidence_id": "ev:1", "summary": "근거", "locator": {"start_line": 2, "end_line": 3}}]
    green = {"source_sha256": "x" * 64, "source_body_end_line": 5,
             "per_chapter_counts": {"1": 1}, "quote_in_span": {"checked": 9, "passed": 9},
             "generated_at_rev": "test"}
    v = LocalPack(_anchor_zip(tmp_path / "g.zip", ev, anchor=green)).verify_baseline()
    assert v["anchor"]["mode"] == "declared" and v["anchor"]["green"] is True
    assert "재계산 아님" in v["anchor"]["note"] and v["status"] == "PASS"
    red = {**green, "quote_in_span": {"checked": 30, "passed": 20}}
    v2 = LocalPack(_anchor_zip(tmp_path / "r.zip", ev, anchor=red)).verify_baseline()
    assert v2["status"] == "FAIL" and "quote_in_span_declared_red" in v2["below_baseline"]
    v3 = LocalPack(_anchor_zip(tmp_path / "a.zip", ev)).verify_baseline()
    assert v3["anchor"]["mode"] == "absent" and v3["status"] == "PASS"


def test_suspect_uniform_chapter_count_and_floor_landing(tmp_path, monkeypatch):
    """기계 패턴 suspect: 장당 고정 개수 + 총량 하한 정확 착지 — 차단 아닌 정독 지정."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    nodes = [{"id": "per:a", "space": "concept", "node_type": "Person",
              "properties": {"label": "A"}}]
    evidence, edges = [], []
    sizes = [3, 9, 21, 45]  # 장 내 구간 크기는 출렁이게 (gate8 CV 간섭 차단)
    line = 1
    for ch in range(1, 6):          # 5개 장 × 정확히 4개 = 유니크 {4}
        for i, sz in enumerate(sizes):
            eid = f"ev:{ch}-{i}"
            nodes.append({"id": eid, "space": "evidence", "node_type": "TextUnit",
                          "properties": {"label": eid}})
            evidence.append({"evidence_id": eid, "summary": "s",
                             "locator": {"start_line": line, "end_line": line + sz - 1,
                                          "chapter": f"c{ch}"}})
            edges.append({"source": eid, "target": "per:a", "relation": "describes",
                          "properties": {}})
            line += sz
    comp = {"profile": "test", "passed": True, "source_lines": 1000,
            "source_body_end_line": 1000,
            "measured": {"evidence": 20, "kinetic": 0, "theme": 0, "claim": 0,
                          "grammar_kinds": 1,
                          "per_1000": {"evidence": 20.0, "kinetic": 0.0}},
            "floors": {"evidence": 20}}   # 20/천행 정확 착지 → suspect
    p = tmp_path / "s.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("nodes.jsonl", "\n".join(json.dumps(n, ensure_ascii=False) for n in nodes))
        z.writestr("edges.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in edges))
        z.writestr("evidence.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in evidence))
        z.writestr("reviews.jsonl", "")
        z.writestr("quality/baseline-compliance.json", json.dumps(comp, ensure_ascii=False))
    v = LocalPack(str(p)).verify_baseline()
    verdicts = {g["item"]: g["verdict"] for g in v["baseline"]["gates"]}
    assert verdicts.get("uniform_per_chapter_count") == "SUSPECT", verdicts
    assert verdicts.get("floor_landing") == "SUSPECT", verdicts
    assert verdicts.get("uniform_slicing") == "PASS", verdicts
    assert v["status"] == "PASS", v  # suspect는 차단이 아니다


def test_t4_build_queryable_refuses_red_anchor_and_warns_absent(tmp_path, monkeypatch):
    """봉인 순서: 게이트 red 상태의 자가 봉인은 빌드 시점에 무효."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    red = {"source_sha256": "x" * 64, "source_body_end_line": 5,
           "quote_in_span": {"checked": 30, "passed": 20}}
    z = _anchor_zip(tmp_path / "red.zip", EVIDENCE, anchor=red)
    r = build_queryable(z, str(tmp_path / "out.zip"), include_embeddings=False)
    assert "error" in r and "red" in r["error"]
    z2 = _anchor_zip(tmp_path / "old.zip", EVIDENCE)
    r2 = build_queryable(z2, str(tmp_path / "out2.zip"), include_embeddings=False)
    assert "error" not in r2 and "anchor_warning" in r2


def test_gate9_works_on_notepack(tmp_path, monkeypatch):
    """12권째 정본은 노트팩 — verify_baseline이 폴더에서도 같은 대조를 수행한다."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    from test_notepack_loader import _build_fixture
    root = str(tmp_path / "note-pack")
    _build_fixture(root)
    qdir = os.path.join(root, "quality")
    os.makedirs(qdir)
    json.dump({"source_sha256": "x" * 64, "source_body_end_line": 9999,
               "per_chapter_counts": {}, "quote_in_span": {"checked": 5, "passed": 5},
               "generated_at_rev": "t"},
              open(os.path.join(qdir, "anchor-check.json"), "w", encoding="utf-8"))
    v = LocalPack(root).verify_baseline()
    assert v["present"] is True and v["anchor"]["mode"] == "declared"
    assert v["anchor"]["green"] is True and v["status"] == "PASS", v


def test_kinetic_chain_kinds_gate(tmp_path, monkeypatch):
    """키네틱 실효 게이트(북팩 확정): %하한이 아니라 사슬 엣지 종수 4+."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")

    def _pack(path, kinds):
        nodes = [{"id": "per:a", "space": "concept", "node_type": "Person",
                  "properties": {"label": "A"}}]
        edges, evidence = [], []
        for i in range(6):
            kid, eid = f"k:{i}", f"ev:{i}"
            nodes.append({"id": kid, "space": "kinetic", "node_type": "Event",
                          "properties": {"label": kid}})
            nodes.append({"id": eid, "space": "evidence", "node_type": "TextUnit",
                          "properties": {"label": eid}})
            evidence.append({"evidence_id": eid, "summary": "s",
                             "locator": {"start_line": i * 10 + 1, "end_line": i * 10 + 5}})
            edges.append({"source": eid, "target": kid, "relation": "records", "properties": {}})
        for i, rel in enumerate(kinds):
            edges.append({"source": f"k:{i}", "target": f"k:{i+1}", "relation": rel,
                          "properties": {}})
        comp = {"profile": "t", "passed": True,
                "measured": {"evidence": 6, "kinetic": 6, "theme": 0, "claim": 0,
                              "grammar_kinds": len({e["relation"] for e in edges})}}
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("nodes.jsonl", "\n".join(json.dumps(n, ensure_ascii=False) for n in nodes))
            z.writestr("edges.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in edges))
            z.writestr("evidence.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in evidence))
            z.writestr("reviews.jsonl", "")
            z.writestr("quality/baseline-compliance.json", json.dumps(comp, ensure_ascii=False))
        return str(path)

    ok = _pack(tmp_path / "ok.zip", ["triggers", "causes", "results_in", "precedes"])
    v = LocalPack(ok).verify_baseline()
    g = {x["item"]: x["verdict"] for x in v["baseline"]["gates"]}
    assert g.get("kinetic_chain_kinds") == "PASS", g
    thin = _pack(tmp_path / "thin.zip", ["triggers", "triggers", "triggers"])
    v2 = LocalPack(thin).verify_baseline()
    g2 = {x["item"]: x["verdict"] for x in v2["baseline"]["gates"]}
    assert g2.get("kinetic_chain_kinds") == "FAIL", g2
    assert "kinetic_chain_kinds" in v2["below_baseline"]
