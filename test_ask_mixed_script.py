"""Mixed-script name queries must ground without relaxing the evidence gate.

Laura는 / 인물인가 처럼 라틴 고유명사+한글 조사·어미가 붙으면 FTS가 고유명사를
잃고 일반명사 prefix에 잠겼다. 게이트 기준(3자 토큰 겹침 / 2토큰 교집합)은 그대로다.
"""
import json
import sqlite3
import zipfile
from pathlib import Path

from yupack_mcp.local_pack import LocalPack, _fts_stem, _query_tokens


def test_query_tokens_split_latin_hangul_and_drop_particles():
    assert _query_tokens("Laura는 어떤 인물인가?") == ["Laura", "어떤", "인물인가"]
    assert _fts_stem("인물인가") == "인물"
    assert _fts_stem("Laura") == "Laura"
    assert "인물*" not in " ".join(LocalPack._fts_terms(_query_tokens("Laura는 어떤 인물인가?")))


def _name_pack(tmp: Path) -> LocalPack:
    path = tmp / "laura-name.zip"
    nodes = [
        {
            "id": "book:concept:laura",
            "label": "Laura",
            "space": "concept",
            "node_type": "Person",
            "properties": {
                "label": "Laura",
                "label_ko": "로라",
                "text": "카밀라와의 일을 1인칭으로 회고하는 화자.",
                "evidence_refs": ["book:evidence:ch01"],
            },
        },
        {
            "id": "book:evidence:ch01",
            "label": "Laura speaks",
            "space": "evidence",
            "node_type": "TextUnit",
            "properties": {
                "text": "I am Laura, and I tell this tale.",
                "summary": "로라는 자신이 화자임을 밝힌다.",
            },
        },
        {
            "id": "book:concept:other",
            "label": "A generic character type",
            "space": "concept",
            "node_type": "Concept",
            "properties": {"text": "인물마다 해석이 다르다."},
        },
    ]
    evidence = {
        "evidence_id": "book:evidence:ch01",
        "summary": "로라는 자신이 화자임을 밝힌다.",
        "source_id": "fixture-book",
        "source_locator": "Chapter 1",
    }
    fts_path = tmp / "fts.sqlite"
    con = sqlite3.connect(fts_path)
    con.execute("CREATE VIRTUAL TABLE docs USING fts5(id, kind, text)")
    for n in nodes:
        p = n["properties"]
        body = " ".join(str(x) for x in [n["label"], p.get("label"), p.get("label_ko"),
                                          p.get("text"), p.get("summary")] if x)
        con.execute("INSERT INTO docs VALUES(?,?,?)", (n["id"], n["node_type"], body))
    con.commit()
    con.close()
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("nodes.jsonl", "".join(json.dumps(n) + "\n" for n in nodes))
        z.writestr("edges.jsonl", "")
        z.writestr("evidence.jsonl", json.dumps(evidence) + "\n")
        z.writestr("reviews.jsonl", "")
        z.writestr("graph-index/adjacency.jsonl", "")
        z.write(fts_path, "lexical-index/fts.sqlite")
    return LocalPack(str(path))


def test_mixed_script_proper_name_is_grounded(tmp_path):
    pack = _name_pack(tmp_path)
    pack.vector = lambda _q, _k: []
    statuses = [pack.ask("Laura는 어떤 인물인가?")["status"] for _ in range(8)]
    assert statuses == ["grounded"] * 8
    off = pack.ask("비트코인 반감기는 언제인가?")
    assert off["status"] == "no_local_evidence"
