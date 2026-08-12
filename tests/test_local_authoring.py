import unittest

from yupack_mcp import server


SOURCE = (
    "오디세우스는 폴리페모스의 눈을 찌른 뒤 자신의 이름을 밝혔다. "
    "폴리페모스는 아버지 포세이돈에게 복수를 기도했고, 포세이돈은 그의 귀향을 방해했다."
)


class LocalAuthoringTest(unittest.TestCase):
    def setUp(self):
        self.old_extract = server._omlx_authoring_extract
        server.PACKS.clear()

    def tearDown(self):
        server._omlx_authoring_extract = self.old_extract
        server.PACKS.clear()

    def test_local_extraction_keeps_claim_evidence_and_causal_path(self):
        server._omlx_authoring_extract = lambda text, max_nodes: ({
            "concepts": [{"label": "오디세우스", "kind": "person"},
                         {"label": "포세이돈", "kind": "deity"}],
            "claims": [{"statement": "복수 기도가 귀향 방해로 이어진다.",
                        "quote": "폴리페모스는 아버지 포세이돈에게 복수를 기도했고, 포세이돈은 그의 귀향을 방해했다."}],
            "kinetic": [{"label": "복수 기도", "type": "Action",
                         "quote": "폴리페모스는 아버지 포세이돈에게 복수를 기도했고,"},
                        {"label": "귀향 방해", "type": "Event",
                         "quote": "포세이돈은 그의 귀향을 방해했다."}],
            "causal": [{"from": "복수 기도", "relation": "triggers", "to": "귀향 방해"}],
            "involves": [{"kinetic": "복수 기도", "concept": "포세이돈"}],
        }, None)

        result = server.ontology_extract(SOURCE, pack="test-local", source_id=None)

        self.assertEqual(result["status"], "local_extracted")
        nodes = server.PACKS["test-local"]["nodes"]
        claim = next(n for n in nodes.values() if n["space"] == "claim")
        self.assertTrue(claim["properties"]["evidence_refs"])
        self.assertIn(result["source_id"], claim["properties"]["evidence_refs"])
        self.assertTrue(any(e["relation"] == "triggers" for e in server.PACKS["test-local"]["edges"]))
        self.assertEqual(server.pack_quality("test-local")["status"], "pass")

    def test_evidence_or_concept_only_pack_cannot_be_saved_as_final(self):
        server.PACKS["thin"] = {"nodes": {
            "ev:only": {"space": "evidence", "node_type": "TextUnit",
                        "properties": {"label": "원문", "text": "근거만 있음"}},
            "concept:only": {"space": "concept", "node_type": "Entity",
                             "properties": {"label": "이름만 있음"}},
        }, "edges": [], "schema_packs": []}

        self.assertEqual(server.pack_quality("thin")["status"], "needs_authoring")
        self.assertEqual(server.pack_save("thin")["status"], "needs_authoring")


if __name__ == "__main__":
    unittest.main()
