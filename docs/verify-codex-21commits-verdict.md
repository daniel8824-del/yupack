CONDITIONAL

# 코덱스 대증 수리 21커밋 검수 판정

대상: `/Users/yedulab/yupack` main `48d2f90..3861898` (21커밋, `+1202/-238`, 주 파일 `yupack_mcp/server.py`·`local_pack.py`).
검수자: read-only. 코드·커밋 변경 없음. 테스트만 실행.
판정 질문: **21커밋을 유지한 채 플랜 재작업을 얹어도 되는가.**

**답: 유지하되, 아래 걷어내기·테스트 정렬을 플랜 Chunk 1 착수 조건으로 건다.** 전면 철회(revert 21)는 불필요. 그대로 얹으면 Chunk 1+2 게이트(`기존 테스트 포함 회귀 0`)가 즉시 깨진다.

실측 범위: `git log 48d2f90..3861898` 21개 = `bbb3940` … `3861898`. HEAD `3861898a64a42b8b292d8f4fd6ff8971ebc969cc`.
테스트: `uv run pytest test_local_authoring_no_key.py test_local_pack_layout.py test_local_query_hybrid.py test_pack_discovery.py test_smoke.py tests/test_local_authoring.py -q --tb=short`

```
FAILED test_local_authoring_no_key.py::test_no_key_authoring_saves_final_zip_and_reopens_queryable - KeyError: 'saved_to'
FAILED test_local_authoring_no_key.py::test_default_pack_is_user_local_preference_not_codex_config - KeyError: 'saved_to'
2 failed, 20 passed, 1 warning in 0.28s
```

---

## ① 오너 신고 6건 해소 실측

| # | 신고 | 대응 커밋 | 판정 | 실측 |
|---|---|---|---|---|
| 1 | 경량화 팩 방지 | `a796032` reject concept-only · `3861898` host-authored | **부분** | 품질 게이트는 Evidence가 있을 때만 Claim/Kinetic을 요구한다. Evidence 없는 Concept-only는 저장된다. |
| 2 | 키네틱 부재 시 grounded 차단 해제 | `820edb9` graph-optional | **해소** | 그래프 없는 fixture에서 `status=grounded`, 인과 질문은 거절이 아니라 `not_proven_from_local_graph`. 테스트 통과. |
| 3 | 팩 생성 성공(keyless) | `ff3fe84` · `a58af3f` · `3861898` | **부분** | 호스트가 Claim을 넣으면 무키 저장·재질의가 된다. 같은 레인에서 추가한 무키 E2E 2건은 후속 품질 게이트에 깨졌다. |
| 4 | 폴더 자동탐색 금지+질문 | `269c668` (자동탐색 도입) · `c28383f` · `0a4326a` | **미해소** | 하드코딩 `~/Zettelkasten/70_Ontology`는 제거됐지만, 홈 자동탐색이 기본 경로로 남아 있다. 금지가 아니다. |
| 5 | 플러그인 연결 | `0a4326a` (설정 분리) | **부분·범위 밖** | 21커밋은 플러그인 파일을 건드리지 않는다. 디스크상 `.mcp.json`은 `uvx`+`bge-m3`로 연결 정의가 있다. 스킬 문서는 여전히 QMD 기본. 핸드셰이크는 이번 검수에서 미실행. |
| 6 | bge-m3 기본 | `d311f00` · `a72de56` · `8b866c8` | **해소** | 무키 기본이 `("bge-m3", 1024)`. QMD는 `YUPACK_EMBED_MODEL=qmd`일 때만. 테스트 통과. |

### 1. 경량화 팩 — 부분

`_authoring_quality` (`yupack_mcp/server.py:1464-1480`):

```
answer_bearing = counts.get("claim", 0) + counts.get("kinetic", 0)
return {"status": "pass" if not evidence or answer_bearing else "needs_authoring", ...}
```

- Evidence + Concept, Claim/Kinetic 0 → `needs_authoring`. 재현: `pack_create("무키생성", save_to=tmp)` 후 Evidence+Concept만 넣고 `pack_save` → `status=needs_authoring`, `answer_bearing_nodes: 0`, 문구 «Claim 또는 Kinetic이 없는 이 상태는 경량 초안이며 완성 팩으로 저장할 수 없습니다.»
- Evidence 없이 Concept만 → **통과**. `test_smoke.py:27-62` `ontology_add_node("concept", ...)` 두 개 후 `pack_save()`가 `structure`를 돌려주며 이 테스트는 통과했다. 주석(`server.py:1468`) «구조 노드도 없는 경우만 실패»와 구현(`answer_bearing=claim+kinetic`)이 어긋난다.
- 호스트 ingest 경로(`tests/test_local_authoring.py:45-50`)는 Evidence만 있으면 `needs_authoring_completion`으로 막히고, Claim+`supports`면 Kinetic 0으로 `ready_to_save` (`:52-71`). 이 3테스트는 통과.

**해소라고 말할 수 없는 이유:** 수업 중 “이름만 있는 개념 팩”이 Evidence 없이 저장되는 구멍은 21커밋 끝에도 남아 있다.

### 2. 키네틱/그래프 부재 — 해소

대응: `820edb9` (`test_local_query_hybrid.py` + `local_pack.py`).

`local_pack.py:1037-1040` «그래프 인덱스가 없거나 관계가 비어 있어도 … grounded 답변은 가능하다.»
`local_pack.py:1154-1158` 인과 질문 + 체인 0 → `causal_assessment.status = "not_proven_from_local_graph"` (거절 아님).
`server.py:1467-1468` 저장 품질도 kinetic 부재를 거절 조건으로 쓰지 않는다.

재현(테스트 통과):

- `test_keyword_evidence_is_grounded_without_kinetic_path` — `status=="grounded"`, `graph_path==[]`, `causal_chains==[]`, locator·sources 유지
- `test_causal_question_without_kinetic_path_reports_limit_not_refusal` — grounded + `not_proven_from_local_graph`
- `test_no_retrieval_evidence_still_refuses` — 무근거는 여전히 `no_local_evidence`
- `test_claim_with_evidence_can_complete_without_kinetic` — `counts["kinetic"]==0` 이어도 `ready_to_save`

### 3. 무키 팩 생성 — 부분 (후속 커밋이 자기 테스트를 깨뜨림)

살아 있는 경로:

- `ontology_extract` (`server.py:586-616`) API를 부르지 않고 `needs_host_extraction`만 반환. `test_no_key_zip_ingest_returns_host_extraction_workflow` 통과.
- `pack_create`는 `save_to` 없으면 버퍼를 만들지 않는다 (`server.py:1288-1292`). `test_pack_create_requires_user_selected_directory` 통과.

깨진 경로 (`ff3fe84`가 추가, `a796032` 이후 실패):

`test_local_authoring_no_key.py:26-45`는 Evidence+Concept만으로 `pack_save` → `saved_to`를 기대한다.
실측 `pack_save` 반환: `keys=['next','quality','status']`, `status=needs_authoring`. `saved_to` 없음 → `KeyError`.
같은 이유로 `test_default_pack_is_user_local_preference_not_codex_config` (`0a4326a` 추가, Evidence-only 저장 가정)도 실패.

무키 생성 자체는 막히지 않았다. **21커밋이 약속한 “저장까지 가는 무키 E2E”는 HEAD에서 재현 실패**다.

### 4. 폴더 자동탐색 금지+질문 — 미해소

`48d2f90`은 기본 경로를 박아 두었다.

```
48d2f90:yupack_mcp/server.py:1039-1040
d = expanduser(directory or YUPACK_PACK_DIR or "~/Zettelkasten/70_Ontology")
```

`269c668`이 그 상수를 지우고 `_discover_pack_dirs()` (`server.py:1126-1152`)를 넣었다. 시드에 `~/*/70_Ontology`, Documents/Desktop/iCloud, `~/yupack-packs`가 있다.

금지+질문이 된 곳:

- `pack_create` / `pack_save` / `pack_build_queryable` — 경로 없으면 `needs_save_path` + `ask_user` (`server.py:1288-1292`, `1604-1609`, `929-933`)

금지가 아닌 곳 (현재 기본 질의 동선):

- `pack_list_local` (`1159-1178`): directory/env 없으면 홈 자동탐색. 후보 1개면 **조용히 그 폴더를 쓴다**. 0개/복수만 묻는다.
- `_open_verified_final_library` (`1069-1099`) → `pack_ask_local` 핸들 생략 시 (`1231-1247`) 같은 탐색. 정본 1개면 **추측으로 연다**.
- 지시문 `server.py:51-52`가 호스트에게 «팩 서랍(홈에서 자동 탐색)»을 시킨다.

플랜 원칙 4·Task 7은 `needs_pack_location` + «never assume a vault path» + «newest mtime is never silently selected». 구현은 폴더 1개·팩 1개일 때 침묵 선택이다. 오너 문구 «자동탐색 금지+질문»과 불일치.

### 5. 플러그인 연결 — 부분, 21커밋 밖

21커밋 diff는 `requirements.txt` + 테스트 6 + `local_pack.py` + `server.py`뿐. 플러그인 트리 변경 0.

디스크 실측(참고, 범위 밖):

- `/Users/yedulab/plugins/yupack-mcp/.mcp.json` — `uvx --refresh --from git+https://github.com/daniel8824-del/yupack yupack`, `YUPACK_EMBED_MODEL=bge-m3`
- `~/.codex/config.toml` `[plugins."yupack-mcp@personal"] enabled = true`
- 같은 플러그인 `skills/yupack-local-query/SKILL.md:32` «키 없는 기본 모드의 벡터 검색은 QMD의 로컬 임베딩을 쓴다» — `d311f00` 이후 코드와 모순. `:16`은 홈 자동탐색을 스킬 계약으로 고정.

런타임 MCP initialize/핸드셰이크는 이번 검수에서 돌리지 않았다. «연결 없음» 오류의 재현·소멸을 21커밋만으로 단정하지 않는다.

### 6. bge-m3 기본 — 해소

`48d2f90` `_pick_model`:

```
무키 + qmd 설치 → ("qmd", 768)
그 외 → ("text-embedding-3-small", 1536)
bge-m3는 YUPACK_EMBED_MODEL=bge-m3 명시 때만
```

HEAD (`local_pack.py:34-42`):

```
forced == "bge-m3" → 1024
forced == "qmd" → 768
forced 없음 → ("bge-m3", 1024)
```

`test_keyless_default_is_local_bge_m3_not_qmd` 통과.
`vector()` (`local_pack.py:713-714`)는 저장 벡터가 없다고 QMD로 조용히 바꾸지 않는다.
구형 3072d 팩은 zip을 건드리지 않고 sidecar (`:649-653`, `8b866c8`).

OMLX가 꺼져 있으면 벡터 없이 lexical+graph (`:347-348`). 이건 폴백이지 모델 교체가 아니다.

---

## ② 역행 결함

### 21커밋 내부 자기충돌 (실측)

1. **`ff3fe84`/`0a4326a` 테스트 vs `a796032` 품질 게이트.**  
   앞선 커밋은 Concept/Evidence-only 저장을 무키 성공의 증거로 심었다. 뒤 커밋이 그 저장을 막는다. HEAD `2 failed / 20 passed`. 테스트가 구현을 못 따라온 것이 아니라, **나중에 넣은 게이트가 같은 레인의 회귀를 무효화**한 것이다.

2. **`269c668` vs 이후 «묻기» 커밋.**  
   `c28383f`·`0a4326a`는 복수 후보/기본팩을 명시 선택으로 바꿨지만 `_discover_pack_dirs`를 제거하지 않았다. 자동탐색을 고친 것이 아니라 다듬은 것이다.

3. **카드 순서 주석 잔재 (`local_pack.py:1019-1023`).**  
   `01eaba3`이 seeds→prio로 되돌린 뒤에도 «인과 경로가 시드보다 앞선다»와 «시드가 먼저»가 같은 블록에 남아 있다. 실행 순서는 `head = list(seeds) + prio` (`:1023`). 동작은 후자. 주석 역행이지 런타임 역행은 아님.

### v0.3.x (`48d2f90`) 회귀

- **grounding strong 공식은 유지.** 양 끝 모두 `strength = "strong" if lex_strong and vec_best >= thr else "weak"` (`48d2f90:849`, HEAD `local_pack.py:1162`).
- **strong이 나오는 빈도는 바뀐다.** 기본 모델이 qmd 768 / 3-small 1536 → bge-m3 1024, 컷이 모델별로 `0.55` / `0.35` / `0.76` (`:_cos_threshold`, `:873-880`). OMLX 미가동이면 `vec_best=0`이라 어휘가 맞아도 **전부 weak**. `48d2f90`은 같은 기계에서 qmd가 있으면 strong이 가능했다. 공식 파괴는 아니나 수업 체감(«강한 근거»)은 후퇴할 수 있다. sidecar (`8b866c8`)는 OMLX가 살아 있을 때만 이 구멍을 메운다.
- 하드코딩 `~/Zettelkasten/70_Ontology` 제거는 회귀가 아니라 수리. 그 자리에 자동탐색이 남은 것이 문제(①-4).

전면 되돌림(21커밋이 서로를 통째로 무효화)은 없다. 질의 축(`bbb3940` causal_expand, `c263ca3` FTS, `01eaba3` 편향, `820edb9` graph-optional)은 누적 개선이다.

---

## ③ 플랜 정합 — 유지 가능, 단 걷어내기 목록 있음

플랜 원칙 5 (`docs/superpowers/plans/2026-08-13-yucrates-local-authoring-adapter.md` / 볼트 `plan-2026-08-13-yucrates-yupack-local-adapter.md`).

| 원칙 | 21커밋 | 플랜 재작업 시 |
|---|---|---|
| 1. DB 드라이버 복사 금지 | Neo4j/Mongo/Postgres 드라이버 추가 없음. 로컬 SQLite 버퍼 유지. | 유지. `LocalAuthoringStore`가 이 층을 대체. |
| 2. 호스트 저작 | `3861898`/`a58af3f`가 자동 추출을 끊고 호스트에게 원문을 넘김. | 유지하되 candidate→validate→promote는 **아직 없음**. Chunk 1–2가 이 공백을 채운다. |
| 3. 키네틱 조건부 | `820edb9` + `_authoring_quality`가 부재를 거절하지 않음. | 유지. |
| 4. 폴더 질문, 추측 금지 | 저장 경로만 지킴. 목록/자동오픈은 추측. `needs_pack_location` 상태값 없음. | **`_discover_pack_dirs` 사용처를 Task 7로 교체.** |
| 5. 정본 불변 | zip 해시 재기록 없음. sidecar는 `~/.cache/yupack/bge-m3`. | 유지. |

### 플랜 재작업 전에 걷어내거나 정렬해야 할 것

1. **`269c668` 잔재 자동탐색** — `_discover_pack_dirs`, `pack_list_local`의 침묵 1후보 채택, `_open_verified_final_library` / `pack_ask_local`의 자동 오픈. Task 7 `needs_pack_location`으로 치환. 커밋 자체를 revert하면 하드코딩 경로가 돌아올 수 있으니 **revert가 아니라 치환**.
2. **깨진 무키 테스트 2건** — fixture에 Claim+`supports`를 넣거나, 품질 게이트를 플랜 Task 5(미승격/Concept-only/근거 미연결 전부 거부)에 맞춰 테스트를 다시 쓴다. 현재 HEAD는 «게이트는 세고 테스트는 약함».
3. **Concept-only(Evidence 0) 저장 구멍** — Task 5 «Evidence-only, Concept-only, uncovered-source, unpromoted drafts»와 `test_smoke.py:50-62`가 충돌한다. 플랜 착수 전에 스모크 단정을 계약(Claim 필요)으로 옮길 것. 안 그러면 Chunk 3에서 또 깨진다.
4. **QMD 코드 경로** — `_qmd_ensure_collection` / `EMBED_MODEL=="qmd"`는 플랜상 명시 오버라이드. 기본 복귀·자동 폴백 재유입 금지. 삭제는 Task 6 판단.
5. **플러그인 스킬 QMD 문구** — 레포 밖이지만 호스트가 다시 QMD로 읽을 위험. Chunk 4와 같이 고친다.

### 유지할 것 (철회 금지)

`820edb9` graph-optional, `d311f00`/`8b866c8`/`a72de56` bge-m3, `3861898`/`a58af3f` 호스트 저작, `9be847e` verified-final, `c28383f` 복수 팩 질문, `0a4326a` 기본팩을 `~/.yupack/settings.json`에만 저장, `2edf4c5` `mcp>=1.9.0,<2.0`, `bbb3940`/`c263ca3`/`01eaba3` 질의, `86eb237` 중첩 zip, `2addc65` QMD 컬렉션 격리.

`ready-impl-chunk1-2-2026-08-13.md`가 분기점을 `3861898`로 잡은 것은 맞다. **단, 그 문서의 «최종 전체 pytest GREEN»은 오늘 실측에서 거짓**이므로 Chunk 1 전에 위 2·3을 닫지 않으면 구현 레인이 즉시 escalate 조건에 걸린다.

---

## ④ 위생

| 항목 | 실측 |
|---|---|
| `/Users/yedulab` 하드코딩 | **커밋된 코드 0.** `rg` on `yupack_mcp/` 무일치. 남은 히트는 미추적 `docs/` 작업지시·플랜뿐. |
| 시크릿 | 레포 소스에 키/토큰 리터럴 없음. `OPENAI_API_KEY`는 env 읽기만 (`server.py:469`, `local_pack.py:94`). |
| em dash | 영문 코드베이스, 축 무관. |

`48d2f90`의 `~/Zettelkasten/70_Ontology`는 특정 홈 경로 가정이지 `/Users/yedulab` 리터럴은 아니었다. HEAD는 그 가정도 코드에서 뺐다.

---

## ⑤ 미커밋 잔재

`git status --short` (검수 시점):

```
 M .omc/state/last-tool-error.json
?? docs/
?? uv.lock
```

권고:

| 경로 | 커밋? | 이유 |
|---|---|---|
| `docs/superpowers/plans/2026-08-13-yucrates-local-authoring-adapter.md` | **예** | 재작업 SSOT. 볼트 사본과 같이 가야 한다. |
| `uv.lock` (769줄, 미추적) | **예** | 이번 검수가 `uv run pytest`로 환경을 고정했다. 재현성에 필요. |
| `docs/ready-verify-*.md` · `docs/ready-impl-*.md` · `docs/lane-ledger-*.md` | 아니오(또는 별도 ops 커밋) | 발주서. 런타임 계약이 아니다. |
| `docs/verify-codex-21commits-verdict.md` (본 파일) | 오너 판단 | 검수 산출. 코드와 묶을 필요는 없음. |
| `.omc/state/last-tool-error.json` | **아니오** | 도구 상태. |

---

## NOTE (축 밖)

- 21커밋은 main 직행이다. 플랜 구현 지시(`ready-impl-chunk1-2`)는 `feat/local-authoring-adapter`에서만 작업·main 머지 금지를 건다. 이번 21개와 별개 규율.
- `server.py:20` `from mcp.server.fastmcp import FastMCP` — 시스템 `python3 -m pytest`는 `ModuleNotFoundError: mcp`. 레포 관행은 `uv run pytest`.
- `_authoring_quality` 주석(구조 노드)과 구현(claim+kinetic) 불일치.
- 플러그인 스킬과 코드의 임베딩 계약이 어긋남 (QMD vs bge-m3).
- MCP 실핸드셰이크·실제 수업 팩 질의·OMLX `:8000` 생존은 미실행. 벡터 기본값 테스트는 `_pick_model` 단위까지만 증명한다.

---

## 한 줄 지시 (구현 레인)

`3861898`을 베이스로 유지한다. Chunk 1 착수 전에 (a) 무키 테스트 2건을 Claim 계약에 맞추고, (b) Concept-only 스모크를 플랜 Task 5와 충돌하지 않게 고치며, (c) `_discover_pack_dirs` 침묵 선택을 Task 7 질문으로 치환할 좌표를 남긴다. 21커밋 전체 revert는 하지 않는다.
