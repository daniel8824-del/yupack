"""Regression: API 키 없이도 사용자 선택 경로에 질의 가능한 팩을 만든다."""
import os
import tempfile
import zipfile
from pathlib import Path

os.environ.pop("OPENAI_API_KEY", None)

import yupack_mcp.server as S  # noqa: E402
from yupack_mcp.local_pack import LocalPack  # noqa: E402


def _reset():
    S.PACKS.clear()
    S.PACK_DESTINATIONS.clear()


def test_pack_create_requires_user_selected_directory():
    _reset()
    result = S.pack_create("경로확인")
    assert result["status"] == "needs_save_path"
    assert "어느 폴더" in result["ask_user"]
    assert "경로확인" not in S.PACKS


def test_no_key_authoring_saves_final_zip_and_reopens_queryable():
    _reset()
    with tempfile.TemporaryDirectory() as td:
        created = S.pack_create("무키생성", save_to=td)
        assert created["created"] == "무키생성"
        S.ontology_add_node("evidence", "TextUnit", "ev:memory",
                            {"label": "기억의 근거", "text": "기억은 이야기를 보존한다.",
                             "source_id": "fixture", "source_locator": "1쪽"}, pack="무키생성")
        S.ontology_add_node("concept", "Concept", "concept:memory",
                            {"label": "기억", "definition": "기억은 이야기를 보존한다.",
                             "evidence_refs": ["ev:memory"]}, pack="무키생성")
        saved = S.pack_save("무키생성", include_embeddings=False)
        path = Path(saved["saved_to"])
        assert path.name.startswith("무키생성-pack-final-")
        assert path.is_file()
        listed = S.pack_list_local(td)
        assert str(path) in listed["verified_final_packs"]
        answer = LocalPack(str(path)).ask("기억은 무엇을 보존하나?")
        assert answer["status"] == "grounded"
        assert answer["direct_evidence"][0]["evidence_id"] == "ev:memory"


def test_no_key_zip_ingest_returns_host_extraction_workflow():
    _reset()
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "sources.zip"
        with zipfile.ZipFile(source, "w") as z:
            z.writestr("chapter.md", "오디세우스는 귀향을 원했다.")
        S.pack_create("무키원문", save_to=td)
        result = S.pack_ingest_local_zip(str(source), pack="무키원문")
        assert result["status"] == "needs_host_extraction"
        assert result["ingested_files"] == 1
        batch = S.pack_authoring_sources("무키원문")
        assert batch["total_sources"] == 1
        assert "오디세우스" in batch["items"][0]["text"]
        extract = S.ontology_extract(batch["items"][0]["text"], pack="무키원문")
        assert extract["status"] == "needs_host_extraction"
