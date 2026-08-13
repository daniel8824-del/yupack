import os
import tempfile
import unittest
import zipfile

from yupack_mcp import server


SOURCE = (
    "오디세우스는 폴리페모스의 눈을 찌른 뒤 자신의 이름을 밝혔다. "
    "폴리페모스는 아버지 포세이돈에게 복수를 기도했고, 포세이돈은 그의 귀향을 방해했다."
)


class HostAuthoringTest(unittest.TestCase):
    def setUp(self):
        self.old_destinations = dict(server.PACK_DESTINATIONS)
        server.PACKS.clear()
        server.PACK_DESTINATIONS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.source_zip = os.path.join(self.tmp.name, "source.zip")
        with zipfile.ZipFile(self.source_zip, "w") as z:
            z.writestr("odyssey.md", SOURCE)

    def tearDown(self):
        server.PACKS.clear()
        server.PACK_DESTINATIONS.clear()
        server.PACK_DESTINATIONS.update(self.old_destinations)
        self.tmp.cleanup()

    def _ingest(self, pack="host-authored"):
        server.pack_create(pack, save_to=self.tmp.name)
        return server.pack_ingest_local_zip(self.source_zip, pack=pack, max_files=1)

    def test_ingest_hands_source_to_host_instead_of_automatic_authoring(self):
        result = self._ingest()

        self.assertEqual("needs_host_extraction", result["status"])
        self.assertEqual(1, result["ingested_files"])
        self.assertIn("pack_authoring_sources", result["next"])
        self.assertEqual(1, len(server.PACKS["host-authored"]["nodes"]))
        self.assertFalse(any(n["space"] in ("claim", "kinetic")
                             for n in server.PACKS["host-authored"]["nodes"].values()))

    def test_save_is_blocked_until_host_marks_grounded_structure_complete(self):
        self._ingest()

        result = server.pack_save("host-authored", include_embeddings=False)

        self.assertEqual("needs_authoring_completion", result["status"])

    def test_candidate_claim_cannot_reach_the_final_zip(self):
        """유크라테스 계약: 후보는 승격 전에 완성 ZIP으로 나가지 못한다."""
        ingested = self._ingest()
        source_id = ingested["source_ids"][0]
        server.authoring_register_candidate(
            "claim", "Claim", "claim:candidate-only",
            {"label": "검수 전 주장"}, source_id=source_id, pack="host-authored")

        completion = server.pack_authoring_complete("host-authored")
        saved = server.pack_save("host-authored", include_embeddings=False)

        self.assertEqual("needs_promotion", completion["status"])
        self.assertEqual("needs_promotion", saved["status"])
        self.assertEqual(["claim:candidate-only"], saved["unpromoted_node_ids"])

    def test_claim_with_evidence_can_complete_without_kinetic(self):
        ingested = self._ingest()
        source_id = ingested["source_ids"][0]
        server.ontology_add_node(
            "claim", "Claim", "claim:identity-cost",
            {"label": "이름 공개는 귀향 지연의 대가였다",
             "statement": "오디세우스가 이름을 밝힌 일은 귀향 지연의 대가가 되었다.",
             "evidence_refs": [source_id]},
            pack="host-authored",
        )
        server.ontology_add_edge(
            "evidence", source_id, "supports", "claim", "claim:identity-cost",
            pack="host-authored",
        )

        result = server.pack_authoring_complete("host-authored")

        self.assertEqual("ready_to_save", result["status"])
        self.assertEqual(0, result["counts"]["kinetic"])
        self.assertEqual("pass", server.pack_quality("host-authored")["status"])


if __name__ == "__main__":
    unittest.main()
