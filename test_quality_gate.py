"""품질 게이트 G1~G6 + 오디세이급 원장 회귀 (design-2026-08-13-user-pack-quality-gate).

계약: 11개 정본 팩 전수 실측 불변식(G1 Claim≥5 전부 근거 연결 · G2 locator 100% ·
G4 주제≥3 · G5 Evidence≥20 · G6 검수 질문 실측) 밑으로는 완성 팩을 저장하지 않는다.
오디세이 프로파일 점수는 차단이 아니라 quality/ledger.json의 관측값이다.
"""
import json
import tempfile
import zipfile

import yupack_mcp.server as S
from quality_fixture import fill_quality_floor


def _fresh(pack):
    S.PACKS.pop(pack, None)
    S.PACK_DESTINATIONS.pop(pack, None)


def test_thin_pack_is_blocked_with_named_gates():
    pack = "게이트박약"
    _fresh(pack)
    try:
        with tempfile.TemporaryDirectory() as td:
            S.pack_create(pack, save_to=td)
            S.ontology_add_node("evidence", "TextUnit", "ev:1",
                                {"label": "근거", "text": "근거 하나뿐인 팩."}, pack=pack)
            S.authoring_register_candidate("claim", "Claim", "claim:1",
                                           {"label": "주장"}, pack=pack)
            S.authoring_validate_candidate("claim:1", pack=pack)
            S.authoring_promote("claim:1", pack=pack, evidence_ids=["ev:1"])
            saved = S.pack_save(pack, include_embeddings=False)
            assert saved["status"] == "needs_quality"
            gates = {g["gate"] for g in saved["gates"]}
            assert {"G1", "G2", "G4", "G5", "G6"} <= gates
            assert "saved_to" not in saved
    finally:
        _fresh(pack)


def test_pack_quality_reports_save_gate_and_measure():
    pack = "게이트측정"
    _fresh(pack)
    try:
        with tempfile.TemporaryDirectory() as td:
            S.pack_create(pack, save_to=td)
            S.ontology_add_node("evidence", "TextUnit", "ev:1",
                                {"label": "근거", "text": "본문", "source_locator": "1행"},
                                pack=pack)
            q = S.pack_quality(pack)
            assert q["save_gate"]["status"] == "needs_quality"
            assert q["quality_measure"]["evidence"] == 1
    finally:
        _fresh(pack)


def test_heldout_failure_blocks_save_g6():
    pack = "게이트헬드아웃"
    _fresh(pack)
    try:
        with tempfile.TemporaryDirectory() as td:
            S.pack_create(pack, save_to=td)
            # 팩 내용과 무관한 검수 질문 → 구운 zip 실측에서 grounded 실패 → 저장 거부
            fill_quality_floor(S, pack, questions=[
                "코스피 지수 전망은 어떠한가?", "트랜스포머 학습률 스케줄은?",
                "김치찌개 레시피는?", "제주도 항공권 가격은?", "월드컵 우승국은 어디인가?"])
            saved = S.pack_save(pack, include_embeddings=False)
            assert saved["status"] == "needs_quality" and saved.get("gate") == "G6"
            assert saved["heldout"]["failures"]
            assert "saved_to" not in saved
    finally:
        _fresh(pack)


def test_notepack_overwrite_guard():
    """노트가 정본이다 — 기존 노트팩을 묻지 않고 덮어쓰지 않는다."""
    import os as _os
    pack = "게이트덮어쓰기"
    _fresh(pack)
    try:
        with tempfile.TemporaryDirectory() as td:
            S.pack_create(pack, save_to=td)
            fill_quality_floor(S, pack)
            first = S.pack_save(pack, include_embeddings=False)
            assert first["format"] == "notepack"
            again = S.pack_save(pack, include_embeddings=False)
            assert again["status"] == "needs_overwrite_confirm"
            forced = S.pack_save(pack, include_embeddings=False, overwrite=True)
            assert forced["format"] == "notepack"
            assert _os.path.isdir(forced["saved_to"])
    finally:
        _fresh(pack)


def test_floor_pack_saves_with_ledger_and_honest_score():
    pack = "게이트원장"
    _fresh(pack)
    try:
        with tempfile.TemporaryDirectory() as td:
            S.pack_create(pack, save_to=td)
            fill_quality_floor(S, pack)
            saved = S.pack_save(pack, include_embeddings=False)
            assert "saved_to" in saved, saved
            assert saved["format"] == "notepack"
            import os as _os
            lpath = _os.path.join(saved["saved_to"], "quality", "ledger.json")
            assert _os.path.isfile(lpath), "quality/ledger.json 미동봉"
            ledger = json.loads(open(lpath, encoding="utf-8").read())
            assert ledger["schema"] == "yupack.quality-ledger/v1"
            assert len(ledger["source_revision"]) == 16
            # 바닥 질량 팩은 오디세이급이 아니다 — 점수·공백이 정직하게 낮게 찍힌다
            assert ledger["odyssey_class_score"] < 80
            assert ledger["canonical_grade"] is False and ledger["gaps"]
            assert saved["quality"]["odyssey_class_score"] == ledger["odyssey_class_score"]
    finally:
        _fresh(pack)
