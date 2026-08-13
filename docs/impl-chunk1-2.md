# 구현 보고: Chunk 1 + 2 (유크라테스 로컬 어댑터)

- 플랜 정본: `docs/superpowers/plans/2026-08-13-yucrates-local-authoring-adapter.md`
- 브랜치: `feat/local-authoring-adapter` (main 3861898에서 분기, main 직커밋·머지 없음)
- 원본 참조: `/Users/yedulab/opencrab-study/opencrab` (read-only, 수정 0)
- 커밋: `84b5384` (Chunk 1) · `c74facd` (Chunk 2)

## 변경 좌표

| 파일 | 좌표 | 내용 |
|---|---|---|
| `yupack_mcp/yucrates_contract.py` | 신설 149줄 | 유크라테스 그래머 계약 고정본. `MANIFEST`/`GRAMMAR_VERSION`/`PROVENANCE`(:29) |
| | `:53` | `ValidationResult` — 원본 dataclass + `code`(사유 분기용) |
| | `:68` `:81` `:86` | `space_for_node_type` / `allowed_node_types` / `get_allowed_relations` |
| | `:101` `:124` | `validate_node` / `validate_edge` (원본 의미론) |
| `yupack_mcp/local_authoring.py` | 신설 299줄 | 저작 어댑터. `AuthoringError`/`GrammarError`(:33,:41) |
| | `:65` | `unpromoted_node_ids` — candidate·validated 노드 = ZIP 금지 대상 |
| | `:71` | `LocalAuthoringStore` — upsert / 엣지 중복 제거 / drop_node / draft audit / SQLite persist |
| | `:122` | `LocalOntologyBuilder` — 검증 → receipt → 버퍼 쓰기 (원본 순서) |
| | `:202` | `LocalPromotionEngine` — candidate → validated → promoted / rejected |
| | `:287` `:293` `:297` | `store_for` / `builder_for` / `engine_for` (지연 임포트로 순환 회피) |
| `yupack_mcp/server.py` | `:23` | `local_authoring` / `yucrates_contract` 임포트 |
| | `:171` `:175` `:180` | `_space_of` / `_allowed_types_for` / `_edge_relations` → 계약 모듈 위임 (중복 판정 제거, -30줄) |
| | `:653` `:670` | `ontology_add_node` / `ontology_add_edge` → 빌더 라우팅 (MCP 이름·오류 딕트 유지) |
| | `:690`–`:757` | `authoring_register_candidate` / `_validate_candidate` / `_promote` / `_reject` 신설 |
| | `:1429` | `_promotion_gate` — `needs_promotion` 응답 |
| | `:1470` `:1547` | `pack_authoring_complete` / `pack_save` 게이트 배선 |
| `tests/test_yucrates_authoring_parity.py` | 신설 295줄 | 서사형·논증형 fixture + 그래머/빌더/승격 3계층 21 테스트 |
| `tests/test_local_authoring.py` | `+15` | 후보 Claim이 완성 ZIP에 못 가는지 (기존 호스트 저작 흐름 파일 안에서) |

## 유크라테스 원본 ↔ 로컬 이식 대조

| 원본 | 이식본 | 교체된 부분 |
|---|---|---|
| `grammar/validator.py` `validate_node`/`validate_edge` | `yucrates_contract.validate_node`/`validate_edge` | 스키마 레지스트리 로더 미이식, 그래머 원본은 동봉 `manifest.json` 스냅샷 |
| `grammar/manifest.py` SPACES/META_EDGES (1.3.0) | `yupack_mcp/manifest.json` (1.3.0) | 동일 — 유팩 스냅샷이 이미 원본과 같은 판 |
| `ontology/builder.py` `OntologyBuilder.add_node/add_edge` | `LocalOntologyBuilder` | Neo4j·MongoDB·PostgreSQL fan-out → 팩 버퍼 + 로컬 SQLite |
| `ontology/promotion.py` `PromotionEngine` | `LocalPromotionEngine` | 상태는 그대로 `properties['status']`, 스토어만 로컬 |

## 의도적 차이 (적응 지점)

1. **rejected 처리** — 원본은 `status='rejected'`로 남긴다. 로컬은 버퍼 자체가 완성 ZIP의
   재료라 남기면 질의 산출물로 새어 나가므로, 노드와 그 엣지를 버퍼에서 빼고
   `buffer['authoring_audit']`(draft 전용)에 보관한다. 플랜 Task 4 3번 항목의 로컬 해석.
2. **`existing_properties` 인자 제거** — 원본은 원격 스토어 왕복을 피하려 호출자가 속성을
   들고 다녔다. 로컬은 버퍼에서 바로 읽으므로 인자를 없앴다.
3. **예외 → 오류 딕트** — 원본은 `ValueError`. MCP 도구 계약(오류도 딕트)을 지키려
   `AuthoringError(message, **details)`로 올리고 도구 경계에서 기존 형태로 변환한다.
   `declared_types` / `custom_types` / `declared_pairs_from_here` / `hint` 키 모두 보존.
4. **`get_allowed_relations`의 None** — 원본은 미선언 쌍에 빈 리스트. 유팩은 '쌍 미선언'과
   '관계 미허용'을 다른 안내로 갈라야 해서 `None`으로 구분한다.
5. **메시지 지역화** — 사용자 대면 문구는 한국어(기존 유팩 오류 문구 유지). 판정 자체는 동일.
6. **엣지 중복 제거** — 원본 Neo4j는 `MERGE`로 자연 멱등. 로컬 리스트 버퍼는 같은
   `(from, relation, to)`면 새로 쌓지 않고 속성만 갱신하고 `created=false`를 돌려준다.

## 게이트 증거

| 게이트 | 결과 |
|---|---|
| 원본 판정 일치 | `validate_node` 407조합 / `validate_edge` 6534조합 전수 대조, 불일치 **0** (opencrab.grammar.validator 직접 호출 비교) |
| TDD RED | 84b5384 시점 트리에서 `tests/test_yucrates_authoring_parity.py` 수집 실패(ImportError: local_authoring) 확인 |
| TDD GREEN | `python -m unittest tests.test_yucrates_authoring_parity tests.test_local_authoring -v` → **24 tests OK** |
| 무키 Claim·Kinetic 자동 생성 0 | `test_ingest_hands_source_to_host_instead_of_automatic_authoring` (기존) 통과 유지 |
| 후보는 승격 전 ZIP 저장 0 | `test_candidate_content_cannot_be_saved` / `test_validated_content_still_cannot_be_saved` / `test_candidate_claim_cannot_reach_the_final_zip` |
| 완성 ZIP 무변경 | `test_authoring_never_mutates_a_saved_final_zip` (저장 후 저작 계속 → sha256 동일) |
| 클라우드 API·QMD 폴백·자동 LLM 추출 삽입 | 없음 (신규 모듈은 stdlib만 임포트, `sys.modules`에 openai/neo4j 부재를 테스트로 고정) |
| 볼트 접촉 | `/Users/yedulab/Zettelkasten/70_Ontology` 변경 파일 0 |

## 전체 pytest

```
2 failed, 41 passed   (main 3861898 기준선: 2 failed, 20 passed)
```

- 신규 통과 +21, **회귀 0**.
- 남은 2건은 분기 이전부터 빨간 상태다: `test_local_authoring_no_key.py`의
  `test_no_key_authoring_saves_final_zip_and_reopens_queryable`,
  `test_default_pack_is_user_local_preference_not_codex_config`.
  원인은 a796032(`fix(quality): reject concept-only pack drafts`)가 도입한
  `needs_authoring` 게이트이고, 두 테스트는 Concept·Evidence만 든 팩이 저장되던
  옛 계약을 단정한다. 실패 상태(`needs_authoring`)는 분기 전후가 동일하며
  이번 변경의 `needs_promotion` 게이트와 무관함을 실행으로 확인했다.
  이 두 단정의 정리는 플랜 Chunk 3 Task 5(“Reject Evidence-only, Concept-only …
  drafts with repairable reports”) 소관이라 이번 범위에서 건드리지 않았다.

## 새 MCP 도구 (저작 버퍼 전용)

`authoring_register_candidate` → `authoring_validate_candidate` → `authoring_promote`
(`authoring_reject`로 폐기). 열린 ZIP overlay 거버넌스(`promotion_register_candidate` 등,
`pack_handle` 인자)와 이름·경로 모두 분리했다. 승격 시 Evidence는 Claim이면 `supports`,
Kinetic이면 `records`로 투영되고 `evidence_refs`에도 기록되어 기존
`_authoring_coverage` 검사에 그대로 걸린다.

## 이번 범위 밖 (플랜 그대로 남김)

- Chunk 3: 승격 인지 최종화(Task 5), bge-m3 고정·검색 분리(Task 6)
- Chunk 4: MCP 기동 경로·E2E(Task 7)
