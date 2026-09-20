#!/usr/bin/env python3
"""SQLite 以外のデータ源を、同じ検出器で見られる形にそろえる。

CSV/JSON/JSONL はメモリ上の SQLite に載せ替える。検出器を増やすたびに
形式ごとの実装が要る状態を避けるため、入口だけを分岐させる。
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

SQLITE_EXT = {".db", ".sqlite", ".sqlite3"}
TABULAR_EXT = {".csv", ".tsv"}
JSON_EXT = {".json", ".jsonl", ".ndjson"}
SAFE = re.compile(r"[^0-9A-Za-z_]")


def _ident(name: str, used: set[str]) -> str:
    base = SAFE.sub("_", str(name)).strip("_") or "col"
    if base[0].isdigit():
        base = "c_" + base
    out, i = base, 2
    while out.lower() in used:
        out, i = f"{base}_{i}", i + 1
    used.add(out.lower())
    return out


def _memory_table(rows: list[dict], table: str) -> sqlite3.Connection:
    if not rows:
        raise SystemExit("行が1件も読めなかった")
    used: set[str] = set()
    cols = {k: _ident(k, used) for k in rows[0]}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ddl = ", ".join(f'"{c}" TEXT' for c in cols.values())
    conn.execute(f'CREATE TABLE "{table}" ({ddl})')
    ph = ",".join("?" * len(cols))
    conn.executemany(
        f'INSERT INTO "{table}" VALUES ({ph})',
        [[_flat(r.get(k)) for k in cols] for r in rows])
    conn.commit()
    return conn


def _flat(v):
    if v is None or isinstance(v, (str, int, float)):
        return v
    return json.dumps(v, ensure_ascii=False)


def _read_tabular(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        head = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(head, delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel_tab if path.suffix == ".tsv" else csv.excel
        return list(csv.DictReader(fh, dialect=dialect))


def _read_json(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".jsonl", ".ndjson"):
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
    else:
        doc = json.loads(text)
        if isinstance(doc, dict):
            # {"items": [...]} 形式。配列を持つ最初のキーを使う。
            doc = next((v for v in doc.values() if isinstance(v, list) and v), [])
        rows = doc
    return [r for r in rows if isinstance(r, dict)]


def open_source(spec: str, pg_table: str | None = None,
                pg_limit: int = 200000) -> tuple[sqlite3.Connection, str | None]:
    """spec: ファイルパス、または postgresql:// で始まる DSN。"""
    if spec.startswith(("postgresql://", "postgres://")):
        return _open_pg(spec, pg_table, pg_limit)

    path = Path(spec).expanduser()
    if not path.exists():
        raise SystemExit(f"見つからない: {path}")
    if path.suffix.lower() in SQLITE_EXT:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn, None
    if path.suffix.lower() in TABULAR_EXT:
        rows = _read_tabular(path)
    elif path.suffix.lower() in JSON_EXT:
        rows = _read_json(path)
    else:
        raise SystemExit(f"未対応の形式: {path.suffix}（.db .csv .tsv .json .jsonl）")
    name = SAFE.sub("_", path.stem) or "data"
    return _memory_table(rows, name), name


def _open_pg(dsn: str, table: str | None, limit: int):
    """PostgreSQL は標本を写して監査する。原本へ書かないことを構造で保証するため。"""
    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        try:
            import psycopg2 as psycopg  # noqa: PLC0415
        except ImportError:
            raise SystemExit("PostgreSQL には psycopg が要る: pip install psycopg[binary]")
    if not table:
        raise SystemExit("--table で対象テーブルを指定する（PostgreSQL では推定しない）")
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        cur.execute(f'SELECT * FROM {table} LIMIT {int(limit)}')
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    name = SAFE.sub("_", table.split(".")[-1])
    return _memory_table(rows, name), name
