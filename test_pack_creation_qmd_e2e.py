"""팩 생성 완전 E2E — 사용자 인터뷰에서 고른 QMD 임베딩 실경로.

오너 요구 (2026-08-13): 인터뷰에서 QMD 임베딩을 그대로 고르는 경로가 팩 생성까지
끝까지 동작해야 한다. 모의 없이 실제 qmd CLI로 컬렉션을 굽고, 저장된 zip을 다시
열어 벡터 질의까지 확인한다. 원문 ZIP → ingest → 호스트 저작(후보→검수→승격) →
authoring_complete → 저장 → 재열기 → grounded + qmd 벡터 히트.

qmd 미설치 환경에서는 건너뛴다. 테스트가 만든 qmd 컬렉션은 끝나면 제거한다.
"""
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from yupack_mcp import local_pack

pytestmark = pytest.mark.skipif(not local_pack._qmd_available(), reason="qmd 미설치")


def test_full_pack_creation_with_qmd_embeddings(monkeypatch):
    import yupack_mcp.server as S
    pack = "큐엠디생성검증"
    S.PACKS.pop(pack, None)
    S.PACK_DESTINATIONS.pop(pack, None)
    qmd_collection = None
    try:
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("YUPACK_SETTINGS", str(Path(td) / "settings.json"))
            monkeypatch.delenv("YUPACK_EMBED_MODEL", raising=False)
            monkeypatch.delenv("YUPACK_PACK_DIR", raising=False)
            monkeypatch.setenv("YUPACK_QMD_DIR", str(Path(td) / "qmd-docs"))
            monkeypatch.setattr(local_pack, "QMD_DIR", str(Path(td) / "qmd-docs"))

            # ① 설정 인터뷰: 사용자가 qmd를 고른다 (env 강제가 아니라 저장값 경로)
            r = S.pack_configure(pack_dir=td, embed_model="qmd")
            assert r["ok"] and r["effective_model"] == "qmd" and r["dimension"] == 768

            # ② 원문 ZIP → Evidence 보존 → 호스트 저작 시작
            src = Path(td) / "source.zip"
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("chapter1.md",
                           "오디세우스는 폴리페모스에게 자기 정체를 밝혔다. "
                           "폴리페모스는 아버지 포세이돈에게 복수를 기도했다.")
            created = S.pack_create(pack)
            assert created["created"] == pack and created["save_to"] == td
            ing = S.pack_ingest_local_zip(str(src), pack=pack)
            assert ing["status"] == "needs_host_extraction"
            ev_ids = [nid for nid, n in S.PACKS[pack]["nodes"].items()
                      if n["space"] == "evidence"]
            assert ev_ids

            # ③ 유크라테스 저작 계약: 후보 → 검수 → 승격 (Evidence supports 투영)
            S.authoring_register_candidate(
                "claim", "Claim", "claim:naming",
                {"label": "이름을 밝힌 값",
                 "text": "정체 공개가 포세이돈의 방해를 불렀다."}, pack=pack)
            S.authoring_validate_candidate("claim:naming", pack=pack)
            promoted = S.authoring_promote("claim:naming", pack=pack,
                                           evidence_ids=ev_ids[:1])
            assert promoted["lifecycle_status"] == "promoted"
            done = S.pack_authoring_complete(pack)
            assert done["status"] == "ready_to_save", done
            from quality_fixture import fill_quality_floor
            fill_quality_floor(S, pack)

            # ④ 저장 (임베딩 포함 요청 — qmd 모드는 런타임 컬렉션 계약)
            saved = S.pack_save(pack, include_embeddings=True)
            path = Path(saved["saved_to"])
            assert path.is_file() and "-pack-final-" in path.name
            # 인터뷰에서 고른 서랍 아래(작품 폴더 포함)에만 저장한다
            assert Path(td) in path.parents

            # ⑤ 재열기 → grounded + 실제 qmd 벡터 히트
            lp = local_pack.LocalPack(str(path))
            ans = lp.ask("정체를 밝힌 결과는 무엇인가?", top_k=6)
            qmd_collection = ans.get("local_pack_id") and \
                local_pack._qmd_collection_name(ans["local_pack_id"])
            assert ans["status"] == "grounded", ans.get("status")
            trace = ans["retrieval_trace"]
            assert trace["vector_hits"], "qmd 벡터 히트가 비어 있다"
            assert "qmd" in json.dumps(trace), trace.get("backend")
            assert ans["claims"], "승격된 Claim이 답변에 없다"
            # 축별 관측 영수증: 성공 축은 ok + 실제 히트 수
            receipts = trace["axis_receipts"]
            assert receipts["vector"]["status"] == "ok"
            assert receipts["vector"]["backend"] == "qmd"
            assert receipts["lexical"]["status"].startswith(("ok", "unavailable", "error"))
            # 거절에도 영수증이 실려 "왜 축이 비었는지"가 보인다
            off = lp.ask("비트코인 반감기는 언제인가?")
            assert off["status"] == "no_local_evidence"
            assert "axis_receipts" in off
    finally:
        S.PACKS.pop(pack, None)
        S.PACK_DESTINATIONS.pop(pack, None)
        if qmd_collection:
            subprocess.run(["qmd", "collection", "remove", qmd_collection],
                           capture_output=True, text=True, timeout=30)
