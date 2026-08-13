"""북팩 레인 인계 회귀 (findings-yupack-norm-evidence-2026-08-13).

증상: 신형 6권 팩에서 direct_evidence 카드의 summary·locator가 null로 드롭.
원인: _norm_evidence가 구형 flat 키만 매핑, 최상위 summary·중첩 locator{} 미수용.
divine-comedy만 노드 properties.text로 card()가 우연 복구하던 예외도 일반화한다.
인계 권고대로 구형 5권·신형 6권 양쪽 fixture로 검증한다.
"""
import glob

import pytest

from yupack_mcp.local_pack import LocalPack, _norm_evidence


def test_legacy_flat_schema_passthrough_and_mapping():
    # evidence_id가 이미 있으면 그대로
    row = {"evidence_id": "ev:1", "summary": "요약", "source_locator": "3쪽"}
    assert _norm_evidence(row) == row
    # 구형 flat 대체 키
    out = _norm_evidence({"id": "ev:2", "text": "본문", "condition": "조건",
                          "limitation": "한계", "grade": "A", "source_locator": "5쪽"})
    assert out["evidence_id"] == "ev:2" and out["summary"] == "본문"
    assert out["conditions"] == "조건" and out["evidence_grade"] == "A"


def test_final_pack_schema_top_summary_and_nested_locator():
    out = _norm_evidence({
        "id": "peter-pan:evidence:ch01",
        "summary": "모든 아이는 자란다, 단 한 명만 빼고.",
        "locator": {"chapter": "I", "start_line": 71, "end_line": 77},
        "source_id": "src:peter-pan",
        "properties": {"short_quote": "All children, except one, grow up."},
    })
    assert out["summary"] == "모든 아이는 자란다, 단 한 명만 빼고."
    assert out["source_locator"] == {"chapter": "I", "start_line": 71, "end_line": 77}
    assert out["source_id"] == "src:peter-pan"
    # 최상위 summary가 없으면 properties의 short_quote까지 폴백
    out2 = _norm_evidence({"id": "x", "properties": {"short_quote": "quote"}})
    assert out2["summary"] == "quote"


def _final_zip(book: str) -> str | None:
    hits = glob.glob(f"/Users/yedulab/Zettelkasten/70_Ontology/{book}/*-pack-final-*.zip")
    return hits[0] if hits else None


@pytest.mark.parametrize("book", [
    "odyssey-butler",        # 구형 배치 대표
    "animal-farm-orwell",    # 구형 배치
    "peter-pan-barrie",      # 신형 6권 — 인계 증상 재현 대상
    "divine-comedy-longfellow",  # 신형 중 예외였던 책
])
def test_real_pack_evidence_cards_keep_summary_and_locator(book):
    path = _final_zip(book)
    if not path:
        pytest.skip(f"{book} 정본 팩 없음")
    lp = LocalPack(path)  # read_only, 임베딩 호출 없음
    assert lp.evidence, "evidence가 비어 있다"
    # 사용자에게 닿는 계약은 card() 단계다. 회귀 정의:
    # ① 팩 어딘가에 있는 summary(evidence 행 또는 노드 text/summary/short_quote)는
    #    절대 드롭되지 않는다. ② 데이터 자체에 요약이 없는 행(구형 오디세이의 권 단위
    #    컨테이너 24건)은 지어내지 않는다 — 그건 팩 데이터 소관이지 로더 소관이 아니다.
    sample = list(lp.evidence)[:80]
    dropped, recoverable_n = [], 0
    for nid in sample:
        node_p = (lp.nodes.get(nid) or {}).get("properties", {})
        recoverable = bool(lp.evidence[nid].get("summary") or node_p.get("text")
                           or node_p.get("summary") or node_p.get("short_quote"))
        card = lp.card(nid)
        if recoverable:
            recoverable_n += 1
            if not card.get("summary"):
                dropped.append(nid)
        if not (card.get("source_locator") or card.get("locator")):
            dropped.append(f"{nid} (locator)")
    assert recoverable_n, "복구 가능한 summary가 하나도 없다 — fixture가 잘못됐다"
    assert not dropped, f"복구 가능한 카드 필드 드롭 {len(dropped)}: {dropped[:4]}"
    # 신형 6권은 전 행이 최상위 summary를 가지므로 전량 복구가 회귀 기준이다
    if book in {"peter-pan-barrie", "divine-comedy-longfellow"}:
        assert all((lp.card(nid) or {}).get("summary") for nid in sample), \
            "신형 팩에서 summary 없는 카드 발생 (인계 증상 재발)"
