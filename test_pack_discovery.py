"""Regression: local library auto-open selects only verified final packs."""
import hashlib
import json
import zipfile
from pathlib import Path

from yupack_mcp import server


def _pack(path: Path, *, final: bool, valid: bool = True) -> None:
    payload = b'{"id":"n:1"}\n'
    integrity = {
        "sha256": {"nodes.jsonl": hashlib.sha256(payload).hexdigest()}
    }
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("nodes.jsonl", payload)
        z.writestr("integrity.json", json.dumps(integrity if valid else {
            "sha256": {"nodes.jsonl": "not-a-real-hash"}
        }))


def test_verified_final_packs_exclude_lightweight_and_invalid_artifacts(tmp_path):
    final = tmp_path / "odyssey-pack-final-2026-08-12.zip"
    lightweight = tmp_path / "odyssey-pack-improved-2026-08-12.zip"
    invalid = tmp_path / "alice-pack-final-2026-08-12.zip"
    _pack(final, final=True)
    _pack(lightweight, final=False)
    _pack(invalid, final=True, valid=False)

    selected = server._verified_final_pack_paths(str(tmp_path))

    assert selected == [str(final)]
