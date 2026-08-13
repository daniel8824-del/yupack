# yupack

수업용 팩 공방 MCP 서버. 모든 쓰기는 서버 인메모리 팩 버퍼에 쌓이고, Neo4j는 읽기 전용이며, `pack_save`는 옵시디언 노트팩 폴더(노트+MOC+품질 원장)를 산출한다. 첫 사용 시 `pack_configure`가 팩 서가와 임베딩을 인터뷰하며 설정은 `~/.yupack/settings.json`에 저장된다.
Neo4j(NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD/NEO4J_DATABASE)와 OpenAI(OPENAI_API_KEY, OPENAI_EMBED_MODEL) 키는 선택이며 없으면 기능이 buffer-only로 우아하게 폴백한다.
