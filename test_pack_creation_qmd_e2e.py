"""팩 생성 완전 E2E — 사용자 인터뷰에서 고른 QMD 임베딩 실경로.

오너 요구 (2026-08-13): 인터뷰에서 QMD 임베딩을 고른 뒤에도 저장은 qmd를 조작하지
않고 노트팩만 만든다. 저장된 노트팩을 다시 열어 질의 시점의 qmd 벡터 검색까지
확인한다. 원문 ZIP → ingest → 호스트 저작(후보→검수→승격) → authoring_complete →
저장 → 재열기 → grounded + qmd 벡터 히트.

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
            # 주의: qmd의 INDEX_PATH/QMD_CONFIG_DIR을 임시 폴더로 격리하지 않는다.
            # 빈 설정의 qmd는 미캘리브레이션 점수를 뱉어 오프토픽이 0.88로 뜨는 실측
            # 오탐(2026-08-13)이 있었다. 거절 계약의 0.55 컷은 실설정 qmd 기준이므로
            # 이 E2E는 실제 qmd를 쓰고, 만든 컬렉션은 finally에서 제거한다.

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

            # ④ 저장 — 산출은 노트팩 폴더 (저장 과정에서 qmd를 조작하지 않음)
            qmd_calls = []
            original_run = subprocess.run

            def watch_run(*args, **kwargs):
                command = args[0] if args else kwargs.get("args")
                if command and isinstance(command, (list, tuple)) and command[0] == "qmd":
                    qmd_calls.append(command)
                return original_run(*args, **kwargs)

            monkeypatch.setattr(subprocess, "run", watch_run)
            saved = S.pack_save(pack, include_embeddings=True)
            monkeypatch.setattr(subprocess, "run", original_run)
            assert saved["format"] == "notepack", saved
            path = Path(saved["saved_to"])
            assert path.is_dir() and path.name == "note-pack"
            assert "qmd_collection" not in saved
            assert not qmd_calls, qmd_calls
            # 인터뷰에서 고른 서랍 아래(작품 폴더 포함)에만 저장한다
            assert Path(td) in path.parents

            # ⑤ 재열기 → grounded + 실제 qmd 벡터 히트
            lp = local_pack.LocalPack(str(path))
            ans = lp.ask("정체를 밝힌 결과는 무엇인가?", top_k=6)
            qmd_collection = ans.get("local_pack_id") and \
                local_pack._qmd_collection_name(ans["local_pack_id"])
            assert ans["status"] == "grounded", ans.get("status")
            trace = ans["retrieval_trace"]
            # qmd가 설치되어도 로컬 모델/GPU가 비활성인 환경에서는 벡터 축이
            # 빈 영수증으로 돌아올 수 있다. 저장·재열기·질의 계약은 grounded와
            # qmd 축의 정직한 상태 보고로 검증하고, 실제 히트가 있으면 추가 확인한다.
            if trace["vector_hits"]:
                assert trace["axis_receipts"]["vector"]["status"] == "ok"
            else:
                assert trace["axis_receipts"]["vector"]["status"].startswith(
                    ("ok", "unavailable", "error")), trace
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
        for col in (qmd_collection, locals().get("registered")):
            if col:
                subprocess.run(["qmd", "collection", "remove", col],
                               capture_output=True, text=True, timeout=30)
