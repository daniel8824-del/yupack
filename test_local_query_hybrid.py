"""Regression: graph/kinetic is an optional enrichment, never an answer gate."""
import json
import os
import tempfile
import zipfile
from pathlib import Path

from yupack_mcp.local_pack import LocalPack
from yupack_mcp.local_pack import _pick_model, _qmd_collection_name


def test_keyless_default_is_local_bge_m3_not_qmd(monkeypatch, tmp_path):
    """QMD는 볼트 검색용이다. 무키 Yupack 기본은 OMLX bge-m3여야 한다."""
    monkeypatch.delenv("YUPACK_EMBED_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("YUPACK_SETTINGS", str(tmp_path / "unset-settings.json"))
    assert _pick_model() == ("bge-m3", 1024)


def test_qmd_collection_name_keeps_korean_pack_ids_distinct():
    assert _qmd_collection_name("동물농장-pack-final-2026-08-12.zip") != \
        _qmd_collection_name("삼국지-pack-final-2026-08-12.zip")


def _pack_without_graph() -> tuple[tempfile.TemporaryDirectory, LocalPack]:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "no-graph.zip"
    nodes = [
        {
            "id": "theme:memory",
            "label": "Memory",
            "space": "concept",
            "node_type": "Theme",
            "properties": {
                "statement": "Memory preserves the children's stories.",
                "evidence_refs": ["ev:memory"],
            },
        },
        {
            "id": "ev:memory",
            "label": "Memory evidence",
            "space": "evidence",
            "node_type": "TextUnit",
            "properties": {
                "text": "Memory preserves the children's stories.",
                "locator": "Chapter 1",
            },
        },
    ]
    evidence = {
        "evidence_id": "ev:memory",
        "summary": "Memory preserves the children's stories.",
        "source_id": "fixture-book",
        "source_locator": "Chapter 1",
    }
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("nodes.jsonl", "".join(json.dumps(n) + "\n" for n in nodes))
        z.writestr("edges.jsonl", "")
        z.writestr("evidence.jsonl", json.dumps(evidence) + "\n")
        z.writestr("reviews.jsonl", "")
        # Deliberately empty: no legal graph grammar or kinetic path exists.
        z.writestr("graph-index/adjacency.jsonl", "")
    return tmp, LocalPack(str(path))


def _grounded_without_graph(question: str) -> dict:
    tmp, pack = _pack_without_graph()
    try:
        # Exercise the answer gate without a network/QMD dependency: retrieval found
        # a semantic/keyword Concept, but graph expansion has no edges to traverse.
        pack.lexical = lambda _q, _k: [{"id": "theme:memory", "kind": "node:concept", "bm25": -1.0}]
        pack.vector = lambda _q, _k: []
        return pack.ask(question)
    finally:
        tmp.cleanup()


def test_keyword_evidence_is_grounded_without_kinetic_path():
    answer = _grounded_without_graph("Memory")
    assert answer["status"] == "grounded"
    assert answer["graph_path"] == []
    assert answer["causal_chains"] == []
    assert answer["direct_evidence"][0]["source_locator"] == "Chapter 1"
    assert answer["sources"] == ["fixture-book"]


def test_causal_question_without_kinetic_path_reports_limit_not_refusal():
    answer = _grounded_without_graph("Why does Memory matter?")
    assert answer["status"] == "grounded"
    assert answer["causal_chains"] == []
    assert answer["causal_assessment"]["status"] == "not_proven_from_local_graph"
    assert answer["direct_evidence"][0]["source_locator"] == "Chapter 1"
    assert answer["sources"] == ["fixture-book"]


def test_no_retrieval_evidence_still_refuses():
    tmp, pack = _pack_without_graph()
    try:
        pack.lexical = lambda _q, _k: []
        pack.vector = lambda _q, _k: []
        answer = pack.ask("unrelated cryptocurrency")
    finally:
        tmp.cleanup()
    assert answer["status"] == "no_local_evidence"


def test_causal_claim_evidence_is_promoted_alongside_direct_hits():
    """A causal Claim's own evidence must not be hidden behind an unrelated hit."""
    tmp = tempfile.TemporaryDirectory()
    try:
        path = Path(tmp.name) / "causal-claim.zip"
        nodes = [
            {"id": "ev:generic", "label": "Generic evidence", "space": "evidence",
             "node_type": "TextUnit", "properties": {"text": "A nearby but generic fact."}},
            {"id": "claim:cause", "label": "Causal claim", "space": "claim",
             "node_type": "Claim", "properties": {"statement": "The named act caused the delay.",
                                                    "evidence_refs": ["ev:causal"]}},
            {"id": "ev:causal", "label": "Causal evidence", "space": "evidence",
             "node_type": "TextUnit", "properties": {"text": "The named act caused the delay.",
                                                            "locator": "Chapter 9"}},
        ]
        evidence = [
            {"evidence_id": "ev:generic", "summary": "A nearby but generic fact.",
             "source_id": "fixture-book", "source_locator": "Chapter 1"},
            {"evidence_id": "ev:causal", "summary": "The named act caused the delay.",
             "source_id": "fixture-book", "source_locator": "Chapter 9"},
        ]
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("nodes.jsonl", "".join(json.dumps(n) + "\n" for n in nodes))
            z.writestr("edges.jsonl", "")
            z.writestr("evidence.jsonl", "".join(json.dumps(e) + "\n" for e in evidence))
            z.writestr("reviews.jsonl", "")
            z.writestr("graph-index/adjacency.jsonl", "")
        pack = LocalPack(str(path))
        pack.lexical = lambda _q, _k: [
            {"id": "ev:generic", "kind": "Evidence", "bm25": -1.0},
            {"id": "claim:cause", "kind": "Claim", "bm25": -0.9},
        ]
        pack.vector = lambda _q, _k: []
        answer = pack.ask("Why did the delay happen?")
    finally:
        tmp.cleanup()
    assert answer["status"] == "grounded"
    assert answer["causal_assessment"]["status"] == "not_proven_from_local_graph"
    assert [e["evidence_id"] for e in answer["direct_evidence"][:2]] == ["ev:causal", "ev:generic"]
