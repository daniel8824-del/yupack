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
    assert "/download/{token}" in paths, paths


def test_manifest():
    m = S.ontology_manifest()
    assert m["version"] == "1.0.0"
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
    assert "선수" in r["explanation"]


def test_pack_save_zip():
    r = S.pack_save()
    assert r["embeddings"] == "skipped(no key)"
    assert r["counts"]["nodes"] == 2
    token = r["download_url"].rsplit("/", 1)[-1]
    data = S._BUNDLES[token]
    import zipfile
    import io
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    assert "pack.json" in names
    md_files = [n for n in names if n.endswith(".md")]
    assert len(md_files) == 2, names


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
