"""테스트 공통: 설정 게이트를 "구성 완료된 컴퓨터" 상태로 통과시킨다.

게이트 자체의 동작(인터뷰·일일 확인·백엔드 재질문)은 test_settings_gate.py가
YUPACK_SETTINGS를 직접 제어해 검증한다. 여기서는 나머지 테스트가 게이트에
걸리지 않도록, 오늘 확인이 끝난 설정 파일을 세션 전체에 물려준다.
embed_model은 기존 기본값과 같은 bge-m3로 두어 임베딩 테스트 동작을 보존한다.
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

# bge-m3 sidecar 캐시가 테스트 산출물로 사용자 캐시(~/.cache/yupack)를 오염시키지 않게
# 격리한다. local_pack이 import 시점에 읽으므로 conftest 모듈 로드 시점에 설정한다.
os.environ.setdefault("YUPACK_BGE_CACHE_DIR", tempfile.mkdtemp(prefix="yupack-test-bge-"))


@pytest.fixture(scope="session", autouse=True)
def _configured_yupack_settings():
    if os.environ.get("YUPACK_SETTINGS"):
        yield
        return
    td = tempfile.mkdtemp(prefix="yupack-test-settings-")
    path = Path(td) / "settings.json"
    path.write_text(json.dumps({
        "pack_dir": td,
        "embed_model": os.environ.get("YUPACK_EMBED_MODEL") or "bge-m3",
        "confirm_interval": "never",
    }, ensure_ascii=False), encoding="utf-8")
    os.environ["YUPACK_SETTINGS"] = str(path)
    try:
        yield
    finally:
        os.environ.pop("YUPACK_SETTINGS", None)
