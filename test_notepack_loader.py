"""노트팩 로더 회귀 (P1 — zip 생산 폐기 전환의 소비면).

옵시디언 노트 폴더(frontmatter type·node_id·aliases + `동사:: [[대상]]` 링크
+ 근거 노트 행번호)를 LocalPack이 직접 열어, zip 아티팩트 없이 기존 3축 질의·
거절·영수증 계약을 그대로 제공하는지 검증한다. 읽기 전용 — 폴더에 파일 생성 0.
"""
import glob
import os

import pytest

from yupack_mcp.local_pack import LocalPack, refresh_embed_model


def _note(path, title, type_, node_id, body, aliases=None):
    lines = ["---", f'title: "{title}"', "tags:", "  - 테스트", "date: 2026-08-13",
             f"type: {type_}", f"node_id: {node_id}"]
    if aliases:
        lines.append("aliases:")
        lines += [f"  - {a}" for a in aliases]
    lines += ["---", f"## {title}", "", body]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("\n".join(lines))


def _build_fixture(root):
    _note(f"{root}/action/정체를 밝힘.md", "정체를 밝힘", "Action", "act:reveal",
          "오디세우스가 폴리페모스에게 자기 정체를 밝혔다.\n\ntriggers:: [[기도]]",
          aliases=["이름 공개"])
    _note(f"{root}/action/기도.md", "기도", "Action", "act:pray",
          "폴리페모스가 아버지에게 복수를 청했다.\n\ninvolves:: [[포세이돈]]")
    _note(f"{root}/deity/포세이돈.md", "포세이돈", "Deity", "per:poseidon",
          "바다의 신. 귀향을 방해했다.")
    _note(f"{root}/claim/이름의 값.md", "이름의 값", "Claim", "claim:naming",
          "정체 공개가 십년 표류의 대가가 되었다.")
    _note(f"{root}/evidence/제9권 근거.md", "제9권 근거", "TextUnit", "ev:b09",
          "정체를 밝히자 폴리페모스가 포세이돈에게 기도했다.\n\n"
          "원문:: [[오디세이아-영문전문]]\n행:: 4191~4267\n\n"
          "records:: [[정체를 밝힘]]\nsupports:: [[이름의 값]]")
    _note(f"{root}/오디세이-MOC.md", "테스트 MOC", "MOC", "moc", "목차")


def test_notepack_opens_parses_and_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    refresh_embed_model()
    root = str(tmp_path / "note-pack")
    _build_fixture(root)
    before = sum(len(fs) for _, _, fs in os.walk(root))
    lp = LocalPack(root)
    st = lp.status()
    assert st["integrity"] == "notepack"
    assert st["counts"]["nodes"] == 5 and st["counts"]["evidence"] == 1
    assert st["counts"]["adjacency"] > 0
    # 그래프: 동사:: 링크가 방향 있는 엣지로 (역방향 ~ 포함)
    assert ["triggers", "act:pray"] in lp.adj["act:reveal"]
    assert ["~records", "ev:b09"] in lp.adj["act:reveal"]
    # 인과 질의: 저장된 선을 걷는다
    ans = lp.ask("정체를 밝힌 것이 왜 문제였는가?", top_k=4)
    assert ans["status"] == "grounded"
    assert any("act:pray" in str(c) or "act:reveal" in str(c)
               for c in ans.get("causal_chains") or []), ans.get("causal_chains")
    assert any(c.get("id") == "claim:naming" or "이름의 값" in str(c.get("label"))
               for c in ans.get("claims") or [])
    ev = ans.get("direct_evidence") or []
    assert ev and ev[0].get("summary")
    assert (ev[0].get("source_locator") or {}).get("start_line") == 4191
    assert ans["retrieval_trace"]["mode"] == "lexical-only"
    assert ans["retrieval_trace"]["axis_receipts"]["lexical"]["status"] == "ok"
    # 거절 계약 유지
    off = lp.ask("비트코인 반감기는 언제인가?", 3)
    assert off["status"] == "no_local_evidence"
    # 읽기 전용: 폴더에 파일을 만들지 않는다 (runtime sqlite·캐시 0)
    after = sum(len(fs) for _, _, fs in os.walk(root))
    assert after == before, "노트팩 폴더에 파일이 생성됐다"


def test_notepack_frontmatter_edgecases(tmp_path, monkeypatch):
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    refresh_embed_model()
    root = str(tmp_path / "np")
    _note(f"{root}/concept/주제.md", "주제", "Topic", "topic:1", "주제 설명")
    open(f"{root}/README.md", "w", encoding="utf-8").write("프론트매터 없는 문서")
    lp = LocalPack(root)
    assert list(lp.nodes) == ["topic:1"]  # MOC·무프론트매터 문서는 노드가 아니다


def test_migrate_zip_to_notepack(tmp_path, monkeypatch):
    """P3: 기존 zip 정본 → 노트팩 충실 이행 (zip 보존, 노드 수 일치 실측)."""
    import json
    import zipfile
    import yupack_mcp.server as S
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    refresh_embed_model()
    nodes = [
        {"id": "per:a", "space": "concept", "node_type": "Person",
         "properties": {"label": "인물 A", "text": "설명"}},
        {"id": "ev:1", "space": "evidence", "node_type": "TextUnit",
         "properties": {"label": "근거 1", "text": "근거 본문", "source_locator": "1행"}},
        {"id": "claim:1", "space": "claim", "node_type": "Claim",
         "properties": {"label": "주장 1", "text": "주장", "evidence_refs": ["ev:1"]}},
    ]
    edges = [{"source": "ev:1", "target": "claim:1", "relation": "supports", "properties": {}},
             {"source": "ev:1", "target": "per:a", "relation": "describes", "properties": {}}]
    book = tmp_path / "테스트책"
    book.mkdir()
    src = book / "테스트책-pack-final-2026-08-13.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("nodes.jsonl", "\n".join(json.dumps(n, ensure_ascii=False) for n in nodes))
        z.writestr("edges.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in edges))
        z.writestr("evidence.jsonl", json.dumps(
            {"evidence_id": "ev:1", "summary": "근거 본문", "source_locator": "1행"},
            ensure_ascii=False))
        z.writestr("reviews.jsonl", "")
        z.writestr("quality/finalization.json", json.dumps({"checks": {"unique_nodes": True}}))
    r = S.pack_migrate_to_notes(str(src))
    assert r["format"] == "notepack" and r["faithful"] is True, r
    assert r["counts"]["zip_nodes"] == r["counts"]["note_nodes"] == 3
    assert os.path.isfile(os.path.join(r["saved_to"], "quality", "finalization.json"))
    assert os.path.isfile(src)  # zip 보존 (이행기 하위호환)
    lp = LocalPack(r["saved_to"])
    ans = lp.ask("근거 본문은 무엇을 말하나?", 3)
    assert ans["status"] == "grounded"
    # 재이행은 묻지 않고 덮지 않는다
    again = S.pack_migrate_to_notes(str(src))
    assert again["status"] == "needs_overwrite_confirm"


@pytest.mark.skipif(
    not glob.glob("/Users/yedulab/Zettelkasten/70_Ontology/odyssey-butler/note-pack"),
    reason="파일럿 노트팩 없음")
def test_pilot_odyssey_notepack_grounded(monkeypatch):
    """실물 검증: 오디세이 노트팩 584노트를 열어 인과 질문이 grounded로 답한다."""
    monkeypatch.setenv("YUPACK_EMBED_MODEL", "none")
    refresh_embed_model()
    lp = LocalPack("/Users/yedulab/Zettelkasten/70_Ontology/odyssey-butler/note-pack")
    assert len(lp.nodes) > 500, len(lp.nodes)
    ans = lp.ask("오디세우스의 귀향은 왜 10년이나 걸렸는가?", top_k=6)
    assert ans["status"] == "grounded"
    assert ans.get("claims"), "노트팩에서 Claim 층이 답하지 않았다"
    assert ans["retrieval_trace"]["axis_receipts"]["lexical"]["hits"] > 0
