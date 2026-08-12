"""Regression: graph/kinetic is an optional enrichment, never an answer gate."""
import json
import tempfile
import zipfile
from pathlib import Path

from yupack_mcp.local_pack import LocalPack


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
