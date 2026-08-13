"""Regression: API 키 없이도 사용자 선택 경로에 질의 가능한 팩을 만든다."""
import os
import tempfile
import zipfile
from pathlib import Path

os.environ.pop("OPENAI_API_KEY", None)

import yupack_mcp.server as S  # noqa: E402
from quality_fixture import fill_quality_floor  # noqa: E402
from yupack_mcp.local_pack import LocalPack  # noqa: E402


def _reset():
    S.PACKS.clear()
    S.PACK_DESTINATIONS.clear()


def test_pack_create_uses_user_selected_directory_from_settings():
    _reset()
    # 저장 서랍은 설정 인터뷰에서 사용자가 이미 골랐다 (conftest가 그 상태를 재현한다).
    # 인터뷰 전이라면 pack_create는 needs_setup 게이트에 막힌다 (test_settings_gate 참조).
    result = S.pack_create("경로확인")
    assert result["created"] == "경로확인"
    assert result["save_to"] == S._load_settings()["pack_dir"]
    S.PACKS.pop("경로확인", None)
    S.PACK_DESTINATIONS.pop("경로확인", None)


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
        # 유크라테스 계약: Claim은 후보 → 검수 → 승격을 거쳐 근거와 연결돼야 저장된다
        S.authoring_register_candidate("claim", "Claim", "claim:memory",
                                       {"label": "기억 주장",
                                        "text": "기억은 이야기를 보존한다."}, pack="무키생성")
        S.authoring_validate_candidate("claim:memory", pack="무키생성")
        S.authoring_promote("claim:memory", pack="무키생성", evidence_ids=["ev:memory"])
        fill_quality_floor(S, "무키생성")
        saved = S.pack_save("무키생성", include_embeddings=False)
        assert saved["format"] == "notepack", saved
        path = Path(saved["saved_to"])
        assert path.is_dir() and path.name == "note-pack"
        assert (path / saved["moc"]).is_file()
        answer = LocalPack(str(path)).ask("기억은 무엇을 보존하나?")
        assert answer["status"] == "grounded"
        assert answer["direct_evidence"][0]["evidence_id"] == "ev:memory"


def test_default_pack_is_user_local_preference_not_codex_config():
    _reset()
    old_settings = os.environ.get("YUPACK_SETTINGS")
    try:
        with tempfile.TemporaryDirectory() as td:
            os.environ["YUPACK_SETTINGS"] = str(Path(td) / "settings.json")
            S.pack_configure(pack_dir=td, embed_model="none", confirm_interval="never")
            S.pack_create("기본팩", save_to=td)
            S.ontology_add_node("evidence", "TextUnit", "ev:default",
                                {"label": "기본 근거", "text": "기본 팩의 근거다."}, pack="기본팩")
            S.authoring_register_candidate("claim", "Claim", "claim:default",
                                           {"label": "기본 주장",
                                            "text": "기본 팩의 근거다."}, pack="기본팩")
            S.authoring_validate_candidate("claim:default", pack="기본팩")
            S.authoring_promote("claim:default", pack="기본팩", evidence_ids=["ev:default"])
            fill_quality_floor(S, "기본팩")
            saved = S.pack_save("기본팩", include_embeddings=False)
            chosen = S.pack_set_default(saved["saved_to"])
            assert chosen["ok"]
            assert Path(chosen["settings"]).is_file()
            S._AUTO_OPENED = {}
            S._auto_open_on_start()
            assert S._AUTO_OPENED["path"] == saved["saved_to"]
            assert S._AUTO_OPENED["pack_handle"].startswith("pack_")
    finally:
        S._AUTO_OPENED = {}
        if old_settings is None:
            os.environ.pop("YUPACK_SETTINGS", None)
        else:
            os.environ["YUPACK_SETTINGS"] = old_settings


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
