"""Regression: Yupack accepts both flat and graph/ nested final-pack ZIPs."""
import json
import tempfile
import zipfile
from pathlib import Path

from yupack_mcp.local_pack import LocalPack


def _fixture(zip_path: Path, nested: bool) -> None:
    prefix = "wrapper/" if nested else ""
    graph = prefix + ("graph/" if nested else "")
    evidence = prefix + ("evidence/index.jsonl" if nested else "evidence.jsonl")
    node = {"id": "per:odysseus", "label": "Odysseus", "space": "story", "node_type": "Person", "properties": {}}
    ev = {"id": "ev:01", "source_id": "odyssey", "locator": "Book IX", "summary": "Odysseus reveals his name."}
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr(graph + "nodes.jsonl", json.dumps(node) + "\n")
        z.writestr(graph + "edges.jsonl", "")
        z.writestr(evidence, json.dumps(ev) + "\n")
        z.writestr(prefix + "reviews.jsonl", "")
        z.writestr(prefix + "graph-index/adjacency.jsonl", json.dumps({"id": "per:odysseus", "edges": [{"edge_id": "e1", "relation": "records", "node_id": "ev:01", "direction": "out"}]}) + "\n")
        z.writestr(prefix + "lexical-index/placeholder", "")
        z.writestr("__MACOSX/noise/nodes.jsonl", "not json")


def test_flat_pack_remains_flat_root():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "flat.zip"
        _fixture(path, nested=False)
        pack = LocalPack(str(path))
        assert pack.root == pack.cache
        assert pack.status()["counts"] == {"nodes": 1, "evidence": 1, "reviews": 0, "vectors": 0, "adjacency": 1}
        assert pack.adj["per:odysseus"] == [["records", "ev:01"]]


def test_nested_graph_pack_uses_wrapper_root_and_reads_siblings():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested.zip"
        _fixture(path, nested=True)
        pack = LocalPack(str(path))
        assert Path(pack.root).name == "wrapper"
        assert pack.status()["counts"] == {"nodes": 1, "evidence": 1, "reviews": 0, "vectors": 0, "adjacency": 1}
        assert pack.adj["per:odysseus"] == [["records", "ev:01"]]
