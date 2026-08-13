# READY: 유크라테스 로컬 어댑터 복구 Chunk 1+2 (Opus max, TDD)

## 골 3줄
- 정본 플랜 /Users/yedulab/yupack/docs/superpowers/plans/2026-08-13-yucrates-local-authoring-adapter.md 를 전문 Read 후 Chunk 1(유크라테스 계약 고정)·Chunk 2(로컬 저장소 어댑터)를 구현한다. 플랜 명시 방식(superpowers:executing-plans, 체크박스 갱신) 준수.
- 오너 확정 방향 verbatim: "유크라테스 코드를 그대로 가져오고 로컬 부분(DB 저장·임베딩)만 교체" / "질의는 벡터 + 키워드 + 그래프, 유크라테스와 동일".
- 유크라테스 원본 참조 = /Users/yedulab/opencrab-study/opencrab (ontology/builder.py·ontology/promotion.py·grammar/·stores/ - OntologyBuilder·그래머 검증·PromotionEngine·질의 RRF) - 원본 레포는 read-only.

## 브랜치·커밋 규율
- 작업 브랜치 feat/local-authoring-adapter 를 main(3861898)에서 분기해 작업한다. **main 직커밋 금지, 머지 금지** (오너 2026-08-13 경고: 머지는 오너 명시 허락만).
- 커밋은 청크 내 논리 단위로, 파일 명시 add만. push 는 브랜치만 허용(원격 있으면).

## 태스크 (플랜 체크박스 그대로)
Chunk 1: ① 서사형·논증형 fixture + candidate/validated/promoted 불변식 TDD ② yupack_mcp/yucrates_contract.py 에 원격 의존성 없는 그래머 검증 의미론 고정.
Chunk 2: ③ LocalAuthoringStore + LocalOntologyBuilder 신설, 기존 노드·엣지 MCP 를 그 builder 로 라우팅 ④ 작성 버퍼용 후보 등록·검수·승격 도구 도입, ZIP overlay 거버넌스와 분리.

## 게이트
- TDD: 각 태스크 RED -> GREEN 순서, 최종 전체 pytest GREEN(기존 테스트 포함 회귀 0).
- 플랜 통과 기준 중 Chunk 1·2 해당분: 무키에서 Claim·Kinetic 자동 생성 0 / 후보는 승격 전 ZIP 저장 0.
- 클라우드 API·QMD 자동 폴백·자동 LLM 추출 코드 삽입 금지(플랜 아키텍처 절 verbatim).
- 완성 ZIP·원문·정본 데이터 무변경. /Users/yedulab/Zettelkasten/70_Ontology 접촉 금지.
- 플랜 문서의 해당 체크박스를 [x] 로 갱신하고 impl-chunk1-2.md (docs/)에 변경 좌표 표.

## escalation
유크라테스 원본에서 가져올 의미론이 모호하거나(예: PromotionEngine 계약 해석), 기존 21커밋 수리와 충돌하면 중단 후 orca orchestration escalate. 자의 우회 금지.
