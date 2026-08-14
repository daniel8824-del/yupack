# yupack 레인 원장 (2026-08-13 개설, 유크라테스 커맨더 인수)

## 배경
- 코덱스(sol) 세션이 08-12 심야~08-13 04:56 유팩 결함 수사 진행 중 사용량 리밋으로 중단. 오너 지시로 이 레인(유크라테스 커맨더, Fable 5)이 인수. 워커 가용 = 오푸스·소넷·그록 4.5 (오너 명시).
- 오너 결함 신고 6: 팩 경량화 생성 / 키네틱 부재 시 답변 차단 / 팩 생성 실패 / 폴더 자동탐색 실패 / 플러그인 연결 오류 / 임베딩 옛 모델. 오너 확정 방향: "유크라테스 코드 그대로, DB 저장·임베딩 로컬만 교체" + "질의 = 벡터+키워드+그래프 동일".

## 코덱스 잔여물 실측 (08-13)
- 대증 수리 21커밋 main 직행 (48d2f90..3861898, +1202/-238, server.py·local_pack.py 중심).
- 근본 재작업 플랜 4청크 (docs/superpowers/plans/2026-08-13-yucrates-local-authoring-adapter.md, 볼트 사본 동기) - 체크박스 전부 미착수. 중단 지점 = 플랜에 질의 3축 계약 반영 직후.
- 유크라테스 원본 = /Users/yedulab/opencrab-study/opencrab (builder·promotion·grammar·stores 실측).
- 오해 정정: jjbbg-integration 워크트리 미커밋분은 7월 잔재(이번 작업 아님). 어제 코덱스 세션들의 실작업장 = /Users/yedulab/yupack.

## 사이클 1 (08-13 발주)
- 오르카 런 run_aedf383883a0. 검수 task_2c52d5325a14 = 그록 4.5 high 헤드리스(21커밋 반대편 검수, read-only) / 구현 task_b8beb1fb14da = 오푸스 max 헤드리스(플랜 Chunk 1+2, 브랜치 feat/local-authoring-adapter, main 직커밋·머지 금지).
- 발주 형태: 원장=오르카, 프로세스=세션 백그라운드 헤드리스(그록 선례 패턴 - yupack 이 오르카 미등록 폴더라 worker-start 불가 selector_not_found 실측).
- 감시 2건 무장. 다음 = 검수 verdict 로 21커밋 유지/철회 판정 -> Chunk 3+4 발주(소넷 후보) -> 오너 보고.

## 디컨플릭션 (08-13, 크로스세션 - Zettelkasten 레인)
- 별도 세션(Fable 5, 오너 직지시)이 feat/local-authoring-adapter 위에 "설정 게이트"를 구현한다: ① 첫 사용 폴더+임베딩 인터뷰(실응답 백엔드만 선택지, ~/.yupack/settings.json) ② 하루 첫 작업 확인 질문 1개(confirm_interval 해제 가능) ③ 저장 백엔드 사망 시 재질문(조용한 전환 금지) ④ EMBED_MODEL import 시점 고정 해제. 배경 = 실사용자(sagnes) bge-m3 기본값 마찰.
- 소유권 이전: 그록 검수 verdict #4(폴더 자동탐색 미해소)·#5(스킬 QMD 문구)는 그 세션 몫. **Chunk 3+4 발주서에서 이 범위 제외 의무.**
- 시퀀싱: 같은 브랜치 동시 작성 금지 - Chunk 3+4 발주는 그 세션 착지 커밋 확인 후(발주 직전 git log 재실측), 기준 HEAD = 0ca0a2b(Chunk 1-2 좌표 커밋). Chunk 1-2 산출물(yucrates_contract·local_authoring)은 양측 공통 존중.
- main 직커밋·머지 금지 규율 동일 적용. 그 세션이 완료 시 이 원장에 기록 예정.

## Zettelkasten 레인 착지 (08-13)
- 설정 게이트(오너 직지시: 첫 사용 인터뷰 + 하루 1회 확인 + 죽은 백엔드 재질문) feat/local-authoring-adapter 착지. 커밋 88a5764 (origin 반영).
- verdict #4 해소: 폴더 자동탐색을 인터뷰 후보 제시용으로 강등, 실서랍 = settings.pack_dir(사용자 선택). CloudDocs 경로 후보 추가.
- verdict #5 해소: 플러그인 소스 .mcp.json의 YUPACK_EMBED_MODEL=bge-m3 강제 env 제거 + SKILL.md에 게이트 프로토콜·승격 흐름 반영. 버전 0.2.0+codex.20260813103958. 재설치는 main 머지 후.
- 테스트 51/51 (신규 게이트 8, 기존 실패 2건은 Chunk 2 승격 계약에 정렬). MCP stdio E2E: needs_setup → pack_configure → 무재시작 적용.
- 다음: Chunk 3+4 발주 가능. zip 자급 벡터(embedded=0 문제)는 미해결 — Chunk 3 범위. main 머지는 오너 허락 대기.

## 디컨플릭션 해제 (08-13, 커맨더 접수)
- Zettelkasten 레인 착지 실측 정합: 88a5764 = 브랜치 HEAD + origin 반영, main 3861898 무접촉. 설정 게이트(needs_setup 인터뷰·needs_daily_confirm·saved_backend_unreachable·pack_configure 도구·EMBED_MODEL 재해석) + verdict #4(자동탐색 후보 제시 강등, 실서랍=settings.pack_dir)·#5(bge-m3 강제 env 제거, SKILL 0.2.0+codex.20260813103958) 해소 + 테스트 51/51(기존 실패 2건은 Chunk 2 승격 계약 정렬로 폐쇄) + MCP stdio E2E 통과 보고.
- Chunk 3+4 발주 해금(그쪽 통보 "진행하셔도 됩니다"). 기준 HEAD = 88a5764. 발주서에서 설정 게이트·verdict #4·#5 범위 제외 유지. zip 자급 벡터(QMD embedded=0)는 Chunk 3 범위 그대로 - 그쪽 무접촉 확인.
- 발주 시점 = 본류 우선 원칙대로 오푸스 레인 유휴 창(현재 GRADE RED 작업 중). 재설치·main 머지 = 오너 허락 대기 동일.

## findings 인계 접수 (08-13, Classic 북팩 레인 -> 유팩 레인, 오너 지시 라우팅)
- 결함: local_pack.py `_norm_evidence()` 가 구형 스키마 가정 - 신형 6권 팩 evidence 카드의 summary/conditions/limitations/evidence_grade 드롭, 저자 대화에서 근거 요약문 소실 가능. divine-comedy 예외 원인 포함.
- 원인 확정 = 3자 교차(북팩 워커 + 커맨더 재현 + Grok 코드 대조). 문서 = /Users/yedulab/opencrab-study/.dryforge/bookpack/findings-yupack-norm-evidence-2026-08-13.md (opencrab-study 커밋 90969fd). 북팩 레인은 규율대로 yupack 수정 0.
- 처리 방침: 유팩 다음 발주(Chunk 3+4 창)에 **별도 노드 YUPACK-NORM-EVIDENCE 수리**로 편입(신형 스키마 필드 보존 + 회귀 테스트). 유팩은 uvx 깃허브 직참조라 수리 후 push 필요(브랜치, main 머지=오너 게이트).

## Zettelkasten 레인 — 북팩 인계 수리 (08-13)
- 북팩 레인 findings(opencrab-study 90969fd, _norm_evidence 신형 스키마 드롭) 수리 완료: main 5f5370e.
- 허브 경유 라우팅 메시지는 이 세션에 미도착 — 문서를 커밋에서 직접 발굴해 처리. 크로스 메시지 회신은 오너 규율대로 생략, 이 원장과 GitHub이 조율 표면.
- 이중 스키마 매핑 + card() 노드 복구 일반화(divine-comedy 예외 제거). 회귀: 구형·신형 실팩 fixture 6건, 전체 58/58. peter-pan ask() 실증 — summary·chapter/line locator 복원.
- 데이터 소관 발견: odyssey 권 단위 컨테이너 evidence 24행은 팩 어디에도 요약문이 없음(로더 무관). 증보 백로그 후보.

## Zettelkasten 레인 — 품질 게이트 출하 (08-13)
- 오너 승인으로 저장 게이트 G1~G6 + 오디세이급 품질 원장(quality/ledger.json) 구현: main 00a1c57. 테스트 62/62, MCP 등록 확인(pack_register_checks·pack_quality).
- 기준 프로파일 재보정(오너 교정): 11팩 최소값이 아니라 오디세이(서사형)·역사란(논증형) 실측치. 얇은 4팩(peter-pan·secret-garden·anne·little-women) 증보 백로그.
- 같은 날 함께 출하: KINGCRAB 이식(리비전 지문·축별 영수증, 50827e8) + 북팩 인계 수리(_norm_evidence, 5f5370e).
- 주의(커맨더 레인 Chunk 3+4 발주 시): pack_save·pack_quality·ask 주변이 오늘 크게 움직임. 기준 HEAD를 00a1c57로 갱신 요망. 게이트·원장 범위는 이 레인 소유.

## Zettelkasten 레인 — 발주서 T1~T3 출하 + 노트팩 파일럿 (08-13 오후)
- 품질층 발주서 이행: T1 build_queryable quality/ 보존(해시 이중잠금) · T2 status() baseline_compliance(부재 시 present:false) · T3 verify_baseline()+pack_verify_baseline(자기신고 불신, 조작=declared_mismatch/미달=below_baseline/부재=absent). main 40c30c7, 인수 4케이스+실팩 전수(깨뜨림 0), 전체 67/67. T4(_norm_evidence)는 5f5370e 기완료.
- 별건: 오너 방향으로 오디세이 노트팩 파일럿 — 정본 팩 583노드를 70_Ontology/odyssey-butler/note-pack/ 노트 584개(MOC+동사:: 링크+aliases+근거 행번호)로 자동 생성, 2홉 파일 보행·백링크 근거·스코프 qmd 질의 실측 통과. qmd 컬렉션 odyssey-note-pack.

## Zettelkasten 레인 — 발주 전문 정합 출하 (08-13 저녁)
- 발주 전문·기준 문서(§3·§4·§6·§7) 정독 후 요지판과의 갭 3개 폐합: quality 2종(.md 포함) 보존 / status() 발주 형태 요약(profile·passed·per_1000) / verify_baseline §6 스키마 재계산(measured 대조 + 고립 0·records 커버리지·floors 밀도). T6(mode: lexical-only 정직 표기) 선반영. main fd42b51, 전체 72/72.
- T5(MultiPack/PackSet)는 스펙 권고대로 별도 창 대기 — 사이드카 관계 인덱스는 미들 레인 소관.
- 참고: 전체 스위트 7분으로 증가(QMD E2E의 전역 qmd update가 오늘 볼트 노트 584건 추가로 길어짐) — 테스트 속도 백로그.

## Zettelkasten 레인 — 발주 개정판(게이트 8종) 반영 (08-13 저녁 2)
- §4 6종→8종 개정 반영: 게이트 7 locator 상한(본문 종료행 침범 = FAIL, 피터팬 라이선스 실사례) + 게이트 8 등분 탐지(CV<0.1 suspect, 자동 FAIL 아님·정독 대상, 화원 실사례) + compliance source_body_end_line 수용. main f5e746e, 74/74.
- 간헐 실패 2건(no_key 계열, 8분 저속 실행 중 1회 발생) 재실행 비재현(74/74·31초) — 외부 qmd/launchd 경합 창의 일시 간섭으로 판정, 테스트 격리 백로그에 병기. 파이프 exit 은폐로 red push 1회 발생 → pipefail 규율로 교정.
- 코덱스 브리프(brief-codex-yupack-2026-08-13.md) 좌표 낡음: HEAD 88a5764·NORM-EVIDENCE 미수리·T1~T3 미구현 전제는 전부 경과 — 현재 main에 T1~T4·T6·게이트 7/8 반영됨. 남은 발주 = T5(사이드카 인덱스 대기)·Chunk 3(zip 자급 벡터)·Chunk 4.

## Zettelkasten 레인 — 노트팩 전환 P1~P3 출하 (08-13 밤)
- 오너 결정: zip 생산 폐기, 유팩 = 옵시디언 노트팩 도구 (D1 zip 소비 이행기 유지, D2 임베딩 qmd 소관, 기준 = 오디세이). 사전 검증: KINGCRAB 레포 전체 테스트 통과·유크라테스 MCP 문법 v1.3.0 정합.
- P1 로더(f0589ca): 폴더→frontmatter+동사:: 파싱→3축 질의·거절·영수증, 인메모리 FTS, 볼트 무생성. 실물 오디세이 노트팩 584노트 grounded.
- P2 저장(55cfc83): pack_save → {서가}/{팩}/note-pack/ + MOC + quality/ledger + qmd 컬렉션 자동 등록 + G6 재열기 실측 + 덮어쓰기 가드. 구 zip 파이프라인 도달 불가 처리(P4 제거 예정).
- P3 이행(e9852f1): pack_migrate_to_notes — zip 정본 충실 복제(무게이트), zip 보존, faithful 실측. 전체 79/79.
- 대기: 11권 일괄 이행(볼트 대량 쓰기 — 오너 확인 후), P4(스킬 2종 재작성·규약 v3 초안·구 zip 코드 제거·pack_list_local 노트팩 목록).

## 저녁 — 검증단 → 코덱스 P4 → 게이트 정정 (Fable 게이트 레인)
- 검증단(소넷 4) findings: pack_save 도달불가 121행·_contract_files·_store_zip 데드코드, _INSTRUCTIONS 등 stale zip 문구, discovery *.zip 전용 격차(노트팩 미탐지), 미선언 커스텀 타입 106노드·관계쌍(우리 실측 309 + contains_segment 206 = 515엣지), aliases 타입별 편차(Event 1.5%·Deity 0%), 인과 엣지 희박(표준 8건).
- Fable 직접: 데드코드 3덩이 절제 + 저장 경로 qmd 디커플링(D2 — _qmd_register_folder 삭제). 질의 시점 qmd 호환 모드(_qmd_ensure_collection)는 정당 경로로 유지.
- 코덱스(Luna high) P4 발주: stale 문구·discovery 노트팩·hydrate 폴더 분기·pack-contract v1(3항 대조)·테스트·플러그인 표면. 자기신고 "81 passed" — 게이트 재실행에서 1 FAIL 검출.
- 게이트 정정: 코덱스가 넣은 qmd INDEX_PATH/QMD_CONFIG_DIR 격리가 미캘리브레이션 qmd를 만들어 오프토픽에 0.88 쓰레기 점수 → grounded 오탐. 실설정 qmd 실측으로 반증(오프토픽 vec 0건·정상 거절). 격리 제거, "프로덕션 경로 검증은 실컴포넌트 그대로" 원칙 주석 봉인. 재게이트 81/81 GREEN.
- 플러그인(/Users/yedulab/plugins/yupack-mcp)은 git 저장소 아님 — 파일 갱신만.

## 저녁 2 — 북팩 레인 발주 착지: 게이트9·suspect·봉인 (6e32524 전달문)
- T1 게이트9 quote-in-span: verify_baseline(source_path) 재계산(정규화 대조: 공백·스마트따옴표·소문자) / 원문 부재 시 anchor-check.json green 검증 + "재계산 아님" 정직 표기 / 둘 다 있으면 교차 대조(declared green vs 재계산 실패 = declared_mismatch).
- T2 suspect 2종: uniform_per_chapter_count(장≥5·개수 유니크≤2), floor_landing(하한 ±5% 착지) — SUSPECT 표기만, 차단 아님.
- T3 승계: 유팩 몫은 T1이 승계분 구분 없이 동일 대조하는 것으로 커버 (재저작 큐는 저작 레인 소관).
- T4 봉인: build_queryable이 anchor-check red면 error로 거부, 부재면 anchor_warning(구팩 하위호환). 팩 트리 보존은 기존 경로.
- 노트팩 지원: verify_baseline이 is_notepack이면 파싱된 nodes/adj/evidence로 동일 대조 — 12권째(노트팩 정본) 컴파일 게이트 성립.
- 테스트 5종 추가(기억 인용 적발·3태·suspect·봉인 거부·노트팩), 86/86 GREEN.

## 밤 — 강의안 세션 발견 처리: 배포 구조 규약 확정
- 발견: 납품 zip이 {책}/타입폴더 직결 구조라 discovery 0개 인식 (로더 마커는 폴더명 note-pack).
- 판정: 로더 유지 (일반 옵시디언 MOC 폴더 오인 방지용 의도된 이름 계약), 배포 zip을 {책}/note-pack/로 재포장한 강의안 세션 처리가 정답 — 공식 배포 규약으로 확정.
- 조치: 플러그인 SKILL에 "배포·서가 규약" 절 신설, 0.3.2 재설치 (캐시=소스 검증). main 무변경 (7d73354 유지).
- 백로그(북팩 증보 레인): 신곡 "베르길리우스" 별칭 공백 — "단테의 안내자"만 응답. 별칭 보강 대상 실측 사례.

## 밤 2 — 재검증 스위프가 함대 신규 봉인 2건 적발 (오탐 아님)
- anne r2: grammar_kinds 선언 25 ≠ 실측 14 · per_1000 분모 상이(선언 31.373 = 본문행 분모, 자기 선언 source_lines(11253)로는 30.4 — §6 분모 모호성).
- iliad r2: kinetic 374↔355 · claim 13↔32 — 정확히 19노드가 선언은 kinetic, 아티팩트 space는 claim (봉인-변환 사이 선언 지연 추정). grammar_kinds 17≠16.
- 처분: 팩 무수정(판정 도구 원칙) — 북팩 레인 인계. 스위프 테스트 전제 정확화: 오탐 0 단정은 기준 세트(8-12 finals)만, 이후 봉인분 mismatch는 findings 출력. 87/87 GREEN.
- 스키마 룰링 요청(북팩): §6 per_1000 분모를 source_lines(총행)로 볼지 source_body_end_line(본문행)로 볼지 명시 — 유팩 재계산기는 룰링을 따른다.
