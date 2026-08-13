"""저장 게이트(G1~G6) 통과용 최소 품질 질량 — 테스트 전용 헬퍼.

게이트 계약이 "저장 가능한 팩 = 11개 정본 팩 실측 불변식 충족"으로 바뀌면서,
성공 저장을 검증하는 테스트는 이 바닥 질량 위에서 각자의 단정을 검증한다.
"""


def fill_quality_floor(server, pack, *, questions=None):
    buf = server.PACKS[pack]
    ev = [nid for nid, n in buf["nodes"].items() if n["space"] == "evidence"]
    for i in range(len(ev), 20):
        server.ontology_add_node("evidence", "TextUnit", f"ev:floor:{i:02d}",
                                 {"label": f"바닥 근거 {i}",
                                  "text": f"게이트 검증용 바닥 근거 문장 {i}이다.",
                                  "source_id": "fixture",
                                  "source_locator": f"{i + 1}행"}, pack=pack)
    ev = [nid for nid, n in buf["nodes"].items() if n["space"] == "evidence"]
    # 기존 evidence에 locator가 없으면 G2에 걸린다 — 바닥 locator를 채운다
    for nid in ev:
        p = buf["nodes"][nid].setdefault("properties", {})
        if not (p.get("source_locator") or p.get("locator")):
            p["source_locator"] = "fixture:1행"
    claims = [nid for nid, n in buf["nodes"].items() if n["space"] == "claim"]
    for i in range(len(claims), 5):
        nid = f"claim:floor:{i}"
        server.authoring_register_candidate(
            "claim", "Claim", nid,
            {"label": f"바닥 주장 {i}", "text": f"바닥 주장 {i}"}, pack=pack)
        server.authoring_validate_candidate(nid, pack=pack)
        server.authoring_promote(nid, pack=pack, evidence_ids=[ev[i % len(ev)]])
    themes = [nid for nid, n in buf["nodes"].items()
              if n.get("node_type") in ("Theme", "Topic")]
    for i in range(len(themes), 3):
        server.ontology_add_node("concept", "Topic", f"topic:floor:{i}",
                                 {"label": f"바닥 주제 {i}", "definition": f"바닥 주제 {i}",
                                  "evidence_refs": [ev[i % len(ev)]]}, pack=pack)
    server.pack_register_checks(
        questions=questions or [f"바닥 근거 {i}는 무엇을 말하는가?" for i in range(5)],
        pack=pack)
