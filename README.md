# yupack

수업용 팩 공방 MCP 서버. 모든 쓰기는 서버 인메모리 팩 버퍼에만 쌓이고, Neo4j는 읽기 전용, pack_save로 zip을 받아 직접 반영한다.
Neo4j(NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD/NEO4J_DATABASE)와 OpenAI(OPENAI_API_KEY, OPENAI_EMBED_MODEL) 키는 선택이며 없으면 기능이 buffer-only로 우아하게 폴백한다.
