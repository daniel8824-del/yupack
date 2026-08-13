"""유크라테스 저작 계약 패리티 테스트.

원본 계약(read-only 참조): /Users/yedulab/opencrab-study/opencrab
  - grammar/validator.py  : validate_node / validate_edge 의미론
  - grammar/manifest.py   : SPACES / META_EDGES 그래머 (version 1.3.0)
  - ontology/builder.py   : OntologyBuilder — 검증이 쓰기보다 먼저, receipt 반환
  - ontology/promotion.py : PromotionEngine — candidate → validated → promoted

로컬 어댑터는 원격 스토어 fan-out(Neo4j/Mongo/Postgres)만 팩 버퍼 + SQLite로 교체하고
검증 순서·상태 전이·근거 투영(supports/records) 의미론은 그대로 유지해야 한다.
"""
import hashlib
import os
import tempfile
import unittest

from yupack_mcp import local_authoring, server, yucrates_contract


# 서사형 원문: 사건과 인과가 있어 Kinetic 투영이 성립한다.
NARRATIVE_SOURCE = (
    "오디세우스는 폴리페모스의 눈을 찌른 뒤 자신의 이름을 밝혔다. "
    "폴리페모스는 아버지 포세이돈에게 복수를 기도했고, 포세이돈은 그의 귀향을 방해했다."
)
# 논증형 원문: 사건이 없고 주장과 근거만 있어 Kinetic이 없어야 정상이다.
ARGUMENT_SOURCE = (
    "역사는 사실의 나열이 아니다. 역사가는 무수한 사실 가운데 무엇을 기록할지 고른다. "
    "따라서 역사서에 남은 사실은 이미 해석을 거친 것이다."
)


class GrammarContractTest(unittest.TestCase):
    """Task 2: 원격 의존성 없는 그래머 검증 의미론."""

    def test_grammar_snapshot_carries_yucrates_provenance(self):
        provenance = yucrates_contract.PROVENANCE

        self.assertEqual(server.MANIFEST["version"], provenance["grammar_version"])
        self.assertIn("opencrab", provenance["source"].lower())

    def test_contract_module_pulls_no_remote_store_dependency(self):
        import sys

        # 계약 모듈은 유크라테스 원격 스토어 패키지 그래프를 끌고 오지 않는다.
        self.assertNotIn("opencrab", sys.modules)
        for remote in ("neo4j", "pymongo", "psycopg", "psycopg2", "openai"):
            self.assertNotIn(remote, sys.modules, remote)

    def test_valid_node_types_follow_space_ownership(self):
        self.assertTrue(yucrates_contract.validate_node("claim", "Claim"))
        self.assertTrue(yucrates_contract.validate_node("evidence", "TextUnit"))
        self.assertTrue(yucrates_contract.validate_node("kinetic", "Event"))

    def test_node_type_in_wrong_space_is_rejected(self):
        result = yucrates_contract.validate_node("evidence", "Claim")

        self.assertFalse(result.valid)
        self.assertIn("Claim", result.error)

    def test_unknown_space_is_rejected(self):
        result = yucrates_contract.validate_node("story", "Person")

        self.assertFalse(result.valid)
        self.assertIn("story", result.error)

    def test_evidence_supports_claim_and_records_kinetic(self):
        self.assertTrue(yucrates_contract.validate_edge("evidence", "claim", "supports"))
        self.assertTrue(yucrates_contract.validate_edge("evidence", "kinetic", "records"))
        self.assertTrue(yucrates_contract.validate_edge("kinetic", "kinetic", "triggers"))

    def test_relation_not_declared_for_the_pair_is_rejected(self):
        result = yucrates_contract.validate_edge("evidence", "kinetic", "supports")

        self.assertFalse(result.valid)
        self.assertEqual("relation_not_allowed", result.code)
        self.assertIn("records", result.error)

    def test_undeclared_space_pair_is_rejected(self):
        result = yucrates_contract.validate_edge("claim", "evidence", "supports")

        self.assertFalse(result.valid)
        self.assertEqual("pair_not_declared", result.code)

    def test_pack_declarations_extend_the_snapshot_without_mutating_it(self):
        extra_types = {"concept": ["BehavioralBias"]}
        extra_relations = [{"from_space": "claim", "to_space": "concept",
                            "relations": ["about"]}]

        self.assertTrue(yucrates_contract.validate_node("concept", "BehavioralBias",
                                                        extra_types=extra_types))
        self.assertTrue(yucrates_contract.validate_edge("claim", "concept", "about",
                                                        extra_relations=extra_relations))
        # 확장은 호출 인자에만 유효하고 스냅샷 자체를 바꾸지 않는다.
        self.assertFalse(yucrates_contract.validate_node("concept", "BehavioralBias"))
        self.assertFalse(yucrates_contract.validate_edge("claim", "concept", "about"))


class _PackTestCase(unittest.TestCase):
    """팩 버퍼·로컬 DB를 테스트 전용으로 격리한다 (사용자 DB 미접촉)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server._DB_PATH
        server._DB_PATH = os.path.join(self.tmp.name, "test.db")
        self.old_packs = dict(server.PACKS)
        self.old_destinations = dict(server.PACK_DESTINATIONS)
        server.PACKS.clear()
        server.PACK_DESTINATIONS.clear()

    def tearDown(self):
        server._DB_PATH = self.old_db
        server.PACKS.clear()
        server.PACKS.update(self.old_packs)
        server.PACK_DESTINATIONS.clear()
        server.PACK_DESTINATIONS.update(self.old_destinations)
        self.tmp.cleanup()

    def _create(self, pack: str) -> None:
        server.pack_create(pack, save_to=self.tmp.name)


class BuilderAdapterTest(_PackTestCase):
    """Task 3: 로컬 빌더 어댑터 — 검증이 먼저, receipt 반환, 엣지 중복 제거."""

    def test_validation_happens_before_any_write(self):
        self._create("빌더팩")
        builder = local_authoring.builder_for("빌더팩")

        with self.assertRaises(local_authoring.GrammarError):
            builder.add_node("evidence", "Claim", "claim:wrong-space", {"label": "잘못된 공간"})

        self.assertEqual({}, server.PACKS["빌더팩"]["nodes"])

    def test_add_node_returns_receipt_and_local_stores(self):
        self._create("빌더팩")

        result = server.ontology_add_node("evidence", "TextUnit", "ev:1",
                                          {"label": "근거", "text": NARRATIVE_SOURCE},
                                          pack="빌더팩")

        self.assertTrue(result["receipt_id"].startswith("rcpt_"))
        self.assertIn("receipt_ts", result)
        self.assertEqual("ok", result["stores"]["buffer"])
        self.assertEqual("ok", result["stores"]["sqlite"])
        # 원격 스토어는 어댑터에 존재하지 않는다.
        self.assertNotIn("neo4j", result["stores"])

    def test_invalid_grammar_stays_a_tool_error_dict(self):
        self._create("빌더팩")
        server.ontology_add_node("concept", "Concept", "c:1", {"label": "개념"}, pack="빌더팩")
        server.ontology_add_node("claim", "Claim", "claim:1", {"label": "주장"}, pack="빌더팩")

        bad = server.ontology_add_edge("concept", "c:1", "supports", "claim", "claim:1",
                                       pack="빌더팩")

        self.assertIn("error", bad)
        self.assertEqual([], server.PACKS["빌더팩"]["edges"])

    def test_duplicate_edge_is_deduplicated(self):
        self._create("빌더팩")
        server.ontology_add_node("evidence", "TextUnit", "ev:1", {"label": "근거"}, pack="빌더팩")
        server.ontology_add_node("claim", "Claim", "claim:1", {"label": "주장"}, pack="빌더팩")

        first = server.ontology_add_edge("evidence", "ev:1", "supports", "claim", "claim:1",
                                         pack="빌더팩")
        second = server.ontology_add_edge("evidence", "ev:1", "supports", "claim", "claim:1",
                                          pack="빌더팩")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(1, len(server.PACKS["빌더팩"]["edges"]))


class PromotionLifecycleTest(_PackTestCase):
    """Task 4 + Task 1: candidate → validated → promoted 와 저장 게이트."""

    def _seed(self, pack: str, text: str) -> str:
        self._create(pack)
        ingested = server.ontology_ingest(text, pack=pack)
        return next(iter(ingested["node_data"]))

    def _register_claim(self, pack: str, source_id: str) -> dict:
        return server.authoring_register_candidate(
            "claim", "Claim", "claim:identity-cost",
            {"label": "이름 공개는 귀향 지연의 대가였다",
             "statement": "오디세우스가 이름을 밝힌 일이 귀향 지연을 불렀다."},
            confidence=0.8, source_id=source_id, pack=pack)

    def test_candidate_content_cannot_be_saved(self):
        source_id = self._seed("후보팩", NARRATIVE_SOURCE)
        registered = self._register_claim("후보팩", source_id)
        self.assertEqual("candidate", registered["lifecycle_status"])

        saved = server.pack_save("후보팩", include_embeddings=False)

        self.assertEqual("needs_promotion", saved["status"])
        self.assertIn("claim:identity-cost", saved["unpromoted_node_ids"])
        self.assertNotIn("saved_to", saved)

    def test_validated_content_still_cannot_be_saved(self):
        source_id = self._seed("검수팩", NARRATIVE_SOURCE)
        self._register_claim("검수팩", source_id)

        validated = server.authoring_validate_candidate("claim:identity-cost", pack="검수팩",
                                                        validator_id="host")
        saved = server.pack_save("검수팩", include_embeddings=False)

        self.assertEqual("validated", validated["lifecycle_status"])
        self.assertEqual("needs_promotion", saved["status"])
        self.assertIn("claim:identity-cost", saved["unpromoted_node_ids"])

    def test_promoted_claim_with_evidence_saves_and_projects_supports(self):
        source_id = self._seed("승격팩", NARRATIVE_SOURCE)
        self._register_claim("승격팩", source_id)
        server.authoring_validate_candidate("claim:identity-cost", pack="승격팩")

        promoted = server.authoring_promote("claim:identity-cost", pack="승격팩",
                                            evidence_ids=[source_id])
        from quality_fixture import fill_quality_floor
        fill_quality_floor(server, "승격팩")
        saved = server.pack_save("승격팩", include_embeddings=False)

        self.assertEqual("promoted", promoted["lifecycle_status"])
        self.assertTrue(promoted["promotion_receipt_id"].startswith("rcpt_"))
        buf = server.PACKS["승격팩"]
        self.assertIn({"from_space": "evidence", "from_id": source_id, "relation": "supports",
                       "to_space": "claim", "to_id": "claim:identity-cost", "properties": {}},
                      buf["edges"])
        self.assertEqual([source_id],
                         buf["nodes"]["claim:identity-cost"]["properties"]["evidence_refs"])
        self.assertTrue(os.path.isdir(saved["saved_to"]))  # 노트팩 폴더가 산출물이다

    def test_promoted_kinetic_projects_records_not_supports(self):
        source_id = self._seed("사건팩", NARRATIVE_SOURCE)
        server.authoring_register_candidate(
            "kinetic", "Event", "kin:name-reveal",
            {"label": "이름을 밝힘"}, source_id=source_id, pack="사건팩")

        server.authoring_promote("kin:name-reveal", pack="사건팩", evidence_ids=[source_id])

        relations = {(e["from_id"], e["relation"], e["to_id"])
                     for e in server.PACKS["사건팩"]["edges"]}
        self.assertIn((source_id, "records", "kin:name-reveal"), relations)

    def test_argument_pack_completes_with_claim_and_no_kinetic(self):
        source_id = self._seed("논증팩", ARGUMENT_SOURCE)
        server.authoring_register_candidate(
            "claim", "Claim", "claim:selection",
            {"label": "역사적 사실은 선택된 것이다",
             "statement": "역사가가 무엇을 기록할지 고르므로 남은 사실은 해석을 거친다."},
            source_id=source_id, pack="논증팩")
        server.authoring_promote("claim:selection", pack="논증팩", evidence_ids=[source_id])
        from quality_fixture import fill_quality_floor
        fill_quality_floor(server, "논증팩")

        quality = server.pack_quality("논증팩")
        saved = server.pack_save("논증팩", include_embeddings=False)

        self.assertEqual("pass", quality["status"])
        self.assertEqual(0, quality["counts"]["kinetic"])
        self.assertTrue(os.path.isdir(saved["saved_to"]))  # 노트팩 폴더가 산출물이다

    def test_rejected_candidate_lives_only_in_draft_audit(self):
        source_id = self._seed("거부팩", NARRATIVE_SOURCE)
        self._register_claim("거부팩", source_id)
        server.ontology_add_edge("evidence", source_id, "supports", "claim",
                                 "claim:identity-cost", pack="거부팩")

        rejected = server.authoring_reject("claim:identity-cost", pack="거부팩",
                                           reason="원문에 근거 없음")

        buf = server.PACKS["거부팩"]
        self.assertEqual("rejected", rejected["lifecycle_status"])
        self.assertNotIn("claim:identity-cost", buf["nodes"])
        self.assertFalse([e for e in buf["edges"] if e["to_id"] == "claim:identity-cost"])
        audit = [row for row in buf["authoring_audit"]
                 if row["node_id"] == "claim:identity-cost"]
        self.assertEqual(["rejected"], [row["lifecycle_status"] for row in audit])

    def test_authoring_never_mutates_a_saved_final_zip(self):
        source_id = self._seed("불변팩", NARRATIVE_SOURCE)
        server.authoring_register_candidate(
            "claim", "Claim", "claim:first", {"label": "첫 주장"},
            source_id=source_id, pack="불변팩")
        server.authoring_promote("claim:first", pack="불변팩", evidence_ids=[source_id])
        from quality_fixture import fill_quality_floor
        fill_quality_floor(server, "불변팩")
        saved = server.pack_save("불변팩", include_embeddings=False)

        def _tree(root):
            h = hashlib.sha256()
            for dp, _, fs in sorted(os.walk(root)):
                for f in sorted(fs):
                    h.update(f.encode())
                    h.update(open(os.path.join(dp, f), "rb").read())
            return h.hexdigest()

        before = _tree(saved["saved_to"])

        server.authoring_register_candidate(
            "claim", "Claim", "claim:second", {"label": "둘째 주장"},
            source_id=source_id, pack="불변팩")
        server.authoring_reject("claim:second", pack="불변팩", reason="근거 없음")

        after = _tree(saved["saved_to"])
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
