# Yucrates Local Authoring Adapter Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore Yucrates' candidate → validation → promotion authoring contract in Yupack while replacing only remote persistence and embedding with local SQLite/ZIP and bge-m3.

**Architecture:** Port the dependency-free Yucrates grammar and promotion semantics behind a local authoring-store adapter. The adapter writes to Yupack's draft buffer plus SQLite resume state; final ZIP remains the portable SSOT. The retrieval contract is unchanged from Yucrates: vector + BM25 keyword + graph expansion → RRF/BM25 cross-rerank → grounded evidence/path output. Only the physical stores differ: ZIP `vectors.bin`/FTS/adjacency replace Yucrates' remote indexes. The authoring path has no Neo4j, MongoDB, PostgreSQL, cloud API, QMD fallback, or automatic LLM extraction.

**Tech Stack:** Python 3.11, stdlib SQLite/ZIP, Yupack MCP 1.x, local OMLX bge-m3 (1024d), Yucrates grammar/promotion semantics.

---

## Chunk 1: Pin the Yucrates contract before moving code

### Task 1: Add parity fixtures and failing lifecycle tests

**Files:**
- Create: `tests/test_yucrates_authoring_parity.py`
- Modify: `tests/test_local_authoring.py`

- [x] Write narrative and argument-source fixtures.
- [x] Assert that candidate and validated content cannot save, promoted Evidence-backed Claim can save, and argument packs do not require Kinetic.
- [x] Run `python -m unittest tests.test_yucrates_authoring_parity -v`; it must fail before the adapter exists.

### Task 2: Record the portable grammar boundary

**Files:**
- Create: `yupack_mcp/yucrates_contract.py`
- Test: `tests/test_yucrates_authoring_parity.py`

- [x] Port `validate_node` and `validate_edge` semantics from Yucrates without importing its remote-store package graph.
- [x] Use Yupack's shipped grammar snapshot with explicit Yucrates source/version provenance.
- [x] Test valid Claim/Evidence `supports`, Evidence/Kinetic `records`, Kinetic/Kinetic `triggers`; reject invalid pairs.
- [x] Commit: `refactor(authoring): pin Yucrates local contract`. (84b5384)

## Chunk 2: Connect Yucrates lifecycle to the local draft store

### Task 3: Implement the local builder adapter

**Files:**
- Create: `yupack_mcp/local_authoring.py`
- Modify: `yupack_mcp/server.py`
- Test: `tests/test_yucrates_authoring_parity.py`

- [x] Write failing tests that validation happens before writes and receipts are returned.
- [x] Add `LocalAuthoringStore`: upsert node, append/deduplicate edge, persist draft, no final ZIP mutation.
- [x] Add `LocalOntologyBuilder`, matching Yucrates ordering but replacing Neo4j/Mongo/Postgres fan-out with buffer + SQLite.
- [x] Route `ontology_add_node` and `ontology_add_edge` through this builder while preserving the MCP names.
- [x] Run `python -m unittest tests.test_yucrates_authoring_parity tests.test_local_authoring -v`. (24 tests OK)

### Task 4: Implement local candidate → validate → promote

**Files:**
- Modify: `yupack_mcp/local_authoring.py`
- Modify: `yupack_mcp/server.py`
- Modify: `yupack_mcp/governance.py`
- Test: `tests/test_yucrates_authoring_parity.py`

- [x] Provide authoring-buffer candidate tools distinct from opened-ZIP overlay governance.
- [x] Preserve `candidate → validated → promoted` and Evidence `supports` / `records` projection.
- [x] Retain rejected candidates only in draft audit state, excluding them from final query artifacts.
- [x] Verify candidate and validated saving are rejected, while a promoted grounded Claim is accepted.
- [x] Commit: `refactor(authoring): restore Yucrates promotion lifecycle locally`. (c74facd)

## Chunk 3: Make final-pack quality and retrieval obey the contract

### Task 5: Replace the shallow save gate with promotion-aware finalization

**Files:**
- Modify: `yupack_mcp/server.py`
- Test: `tests/test_local_authoring.py`
- Test: `tests/test_yucrates_authoring_parity.py`

- [ ] Require every ingested source to project to a promoted Claim or promoted Kinetic node.
- [ ] Reject Evidence-only, Concept-only, uncovered-source, and unpromoted drafts with repairable reports.
- [ ] Allow argument packs to complete with Claim + Evidence and zero Kinetic.
- [ ] For causal questions, return a causal trace only when a promoted causal path exists; do not reject an otherwise grounded Claim/Evidence answer just because Kinetic is absent.
- [ ] Keep `save_to` explicit: ask the user for a folder, never assume a vault path.
- [ ] Commit: `fix(authoring): prevent unpromoted lightweight final packs`.

### Task 6: Lock bge-m3 and decouple retrieval from authoring

**Files:**
- Modify: `yupack_mcp/local_pack.py`
- Modify: `yupack_mcp/server.py`
- Test: `tests/test_local_query_hybrid.py`
- Test: `test_smoke.py`

- [ ] Default no-key model is `bge-m3`, 1024 dimensions.
- [ ] QMD is an explicit compatibility override only.
- [ ] If OMLX is unavailable, expose `vector_unavailable` and retain lexical + graph retrieval; never silently change model.
- [ ] Save builds bge vectors only after promotion and never labels a failed vector build as vector-ready.
- [ ] Preserve the Yucrates query contract exactly: vector candidates + BM25 candidates + graph candidates feed the same explainable RRF/cross-score ranker. Local ZIP indexes may replace DB calls, but graph must remain an equal retrieval axis, not answer decoration.
- [ ] Add parity fixtures that compare candidate IDs, evidence IDs, and causal paths for the same small graph through Yucrates-style hybrid retrieval and Yupack local retrieval.

## Chunk 4: Repair MCP setup and prove complete E2E

### Task 7: Make startup user-specific without hard-coded paths

**Files:**
- Modify: `yupack_mcp/server.py`
- Modify: `README.md`
- Modify: `/Users/yedulab/plugins/yupack-mcp/.mcp.json`
- Test: `tests/test_pack_discovery.py`
- Create: `tests/test_mcp_authoring_e2e.py`

- [ ] No configured folder returns `needs_pack_location` with a user-facing question.
- [ ] Multiple folders return explicit choices; newest mtime is never silently selected.
- [ ] The plugin starts one GitHub `uvx` server with `YUPACK_EMBED_MODEL=bge-m3`; no personal, tmp, or auto-open path.
- [ ] Run real stdio MCP E2E: create → ask folder → ingest → candidate → validate → promote → complete → save → reopen → causal query.
- [ ] Assert no API key is required and existing final-pack hashes never change.
- [ ] Commit and push Yupack code/plugin source only.

## Acceptance evidence

- A no-key run does not auto-invent Claim/Kinetic from a local model.
- Candidate content cannot enter a final ZIP before validation and promotion.
- Claim + Evidence-only argument packs save and answer.
- Narrative packs return causal paths only when their promoted Evidence supports them.
- bge-m3/1024d is default; QMD is opt-in only.
- The user chooses the save/library folder; no personal path or stale plugin path exists.
