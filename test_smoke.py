"""오프라인 스모크: 키 없이 buffer-only 경로 검증. 실행: python test_smoke.py"""
import os

for _k in ("OPENAI_API_KEY", "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_USER", "NEO4J_PASSWORD"):
    os.environ.pop(_k, None)

from yupack_mcp.server import app, PACKS  # noqa: E402
import yupack_mcp.server as S  # noqa: E402


def test_app_routes():
    paths = [r.path for r in app.router.routes]
    assert any(p.startswith("/mcp") for p in paths), paths
    assert any(p.startswith("/sse") for p in paths), paths
    assert "/upload" in paths, paths


def test_manifest():
    m = S.ontology_manifest()
    # 그래머 1.3.0 합류(a88364c) 이후 manifest 버전이 올라갔다. 상수와 대조해
    # 다음 그래머 개정 때 또 낡지 않게 한다.
    assert m["version"] == S.MANIFEST["version"], m["version"]
    assert "concept" in m["spaces"]


def test_add_node_and_edge():
    PACKS.clear()
    r1 = S.ontology_add_node("concept", "Concept", "factoring",
                                 {"label": "인수분해", "definition": "다항식을 곱으로 나누는 것"})
    assert r1["stores"]["buffer"] == "ok"
    r2 = S.ontology_add_node("concept", "Concept", "multiplication",
                                 {"label": "곱셈", "definition": "수를 곱하는 연산"})
    assert r2["stores"]["buffer"] == "ok"
    r3 = S.ontology_add_edge("concept", "multiplication", "related_to", "concept", "factoring")
    assert r3["stores"]["buffer"] == "ok"
    bad = S.ontology_add_edge("concept", "factoring", "not_a_real_relation", "concept", "multiplication")
    assert "error" in bad


def test_pack_ask_chain():
    r = S.pack_ask("인수분해")
    assert r["matched"]["id"] == "factoring"
    assert any(c["rel"] == "related_to" for c in r["chain"])
    # explanation은 관계 사슬을 그대로 렌더한다 ("인수분해 -(related_to)-> 곱셈").
    # 예전 문구("선수")를 기대하던 단정은 렌더 형식 변경으로 낡았다.
    assert "related_to" in r["explanation"], r["explanation"]
    assert "인수분해" in r["explanation"], r["explanation"]


def test_pack_save_notepack():
    import os as _os
    from quality_fixture import fill_quality_floor
    fill_quality_floor(S, S.DEFAULT_PACK)
    r = S.pack_save()
    # zip 생산 폐기 (2026-08-13) — 산출은 옵시디언 노트팩 폴더. 임베딩은 qmd 소관.
    assert r["format"] == "notepack", r
    assert r["counts"]["nodes"] == len(S.PACKS[S.DEFAULT_PACK]["nodes"])
    assert _os.path.isdir(r["saved_to"])
    assert _os.path.isfile(_os.path.join(r["saved_to"], r["moc"]))
    assert _os.path.isfile(_os.path.join(r["saved_to"], "quality", "ledger.json"))


def test_schema_pack_install():
    r = S.schema_pack_install("saas")
    assert r["installed"] == "saas"
    lst = S.schema_pack_list()
    assert "saas" in lst["installed"]


if __name__ == "__main__":
    test_app_routes()
    test_manifest()
    test_add_node_and_edge()
    test_pack_ask_chain()
    test_pack_save_zip()
    test_schema_pack_install()
    print("SMOKE OK")
