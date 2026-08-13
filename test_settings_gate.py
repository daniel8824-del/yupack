"""설정 게이트 회귀: 미설정 인터뷰 → 저장 → 일일 확인 → 백엔드 사망 재질문.

오너 계약 (2026-08-13):
- 설정 없음 → 전체 인터뷰 (이 컴퓨터에서 실제 응답하는 백엔드만 선택지로)
- 오늘 이미 확인함 → 조용히 진행
- 오늘 첫 작업 → 확인 질문 1개 (안정화될 때까지, confirm_interval=never로 해제)
- 저장된 백엔드 죽어 있음 → 날짜 무관 즉시 재질문 (조용한 전환 금지)
"""
import datetime
import json
import os
import tempfile
from pathlib import Path

import pytest

import yupack_mcp.server as S
from yupack_mcp import local_pack


@pytest.fixture()
def own_settings(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        monkeypatch.setenv("YUPACK_SETTINGS", str(path))
        monkeypatch.delenv("YUPACK_EMBED_MODEL", raising=False)
        monkeypatch.delenv("YUPACK_PACK_DIR", raising=False)
        yield td, path


def test_first_use_requires_interview(own_settings):
    td, _ = own_settings
    gate = S.pack_ask_local("아무 질문")
    assert gate["status"] == "needs_setup"
    # 어휘+그래프(none)는 항상 마지막 선택지로 존재한다
    assert gate["embed_options"][-1]["embed_model"] == "none"
    assert "pack_configure" in gate["next"]


def test_configure_saves_and_passes_gate(own_settings):
    td, path = own_settings
    r = S.pack_configure(pack_dir=td, embed_model="none")
    assert r["ok"] and r["effective_model"] == "none"
    # 소스 리비전 지문: 낡은 프로세스 탐지용 (import 시점 고정, 16 hex)
    assert len(r["source_revision"]) == 16
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["pack_dir"] == td
    assert saved["last_confirmed"] == datetime.date.today().isoformat()
    assert S._setup_gate() is None


def test_daily_confirm_until_stabilized(own_settings):
    td, path = own_settings
    S.pack_configure(pack_dir=td, embed_model="none")
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["last_confirmed"] = "2026-01-01"
    path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    gate = S._setup_gate()
    assert gate["status"] == "needs_daily_confirm"
    assert td in gate["ask_user"]
    S.pack_configure(confirm=True)
    assert S._setup_gate() is None


def test_confirm_interval_never_disables_daily(own_settings):
    td, path = own_settings
    S.pack_configure(pack_dir=td, embed_model="none", confirm_interval="never")
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["last_confirmed"] = "2026-01-01"
    path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    assert S._setup_gate() is None


def test_dead_backend_reasks_regardless_of_date(own_settings, monkeypatch):
    td, path = own_settings
    S.pack_configure(pack_dir=td, embed_model="none")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["embed_model"] = "bge-m3"  # 저장 후 OMLX가 죽은 상황 재현
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(local_pack, "omlx_alive", lambda timeout=0.6: False)
    gate = S._setup_gate()
    assert gate["status"] == "saved_backend_unreachable"
    # 죽은 백엔드는 선택지에 나열되지 않는다
    assert all(o["embed_model"] != "bge-m3" for o in gate["embed_options"])
    assert "조용히" in gate["next"]


def test_configure_rejects_dead_backend(own_settings, monkeypatch):
    td, _ = own_settings
    monkeypatch.setattr(local_pack, "omlx_alive", lambda timeout=0.6: False)
    r = S.pack_configure(pack_dir=td, embed_model="bge-m3")
    assert "error" in r and "embed_options" in r


def test_settings_merge_preserves_default_pack(own_settings):
    td, path = own_settings
    S.pack_configure(pack_dir=td, embed_model="none")
    S._save_default_pack_path("/tmp/some-pack.zip")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["default_pack"] == "/tmp/some-pack.zip"
    # default_pack 저장이 인터뷰로 받은 다른 키를 지우면 안 된다
    assert saved["pack_dir"] == td and saved["embed_model"] == "none"


def test_pack_create_uses_interviewed_pack_dir(own_settings):
    td, _ = own_settings
    S.pack_configure(pack_dir=td, embed_model="none")
    S.PACKS.pop("게이트생성", None)
    S.PACK_DESTINATIONS.pop("게이트생성", None)
    created = S.pack_create("게이트생성")
    assert created["created"] == "게이트생성"
    assert created["save_to"] == td  # 인터뷰에서 받은 서랍이 기본 저장처
    S.PACKS.pop("게이트생성", None)
    S.PACK_DESTINATIONS.pop("게이트생성", None)
