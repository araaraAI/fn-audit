#!/usr/bin/env python3
"""監査対象の列を推定する。

利用者が列名を知らなくても走るようにするため。指定があればそれを優先し、
推定結果は必ずレポートに出して、間違った列で監査した可能性を人が検算できるようにする。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

TIME_NAME = re.compile(r"(date|time|_at$|^ts$|created|updated|published|fetched)", re.I)
DATE_VAL = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")
TEXT_NAME = re.compile(r"(title|headline|name|subject|text|body|summary|descr|content|message)", re.I)
LABEL_NAME = re.compile(r"(type|category|categor|kind|class|label|tag|genre|sector|topic|group)", re.I)
# 経路・状態・環境を表す列。値は内容を説明していないので、語彙ギャップの指摘対象にしない。
NON_CONTENT_NAME = re.compile(
    r"(platform|channel|source$|status|state$|stage|env|host|account|user|device|"
    r"provider|vendor|mode|priority|severity|version|locale|lang|queue|worker|job)", re.I)
OPAQUE_NAME = re.compile(r"(json|payload|raw|blob|html|url|uri|key|hash|path|^id$|_id$)", re.I)
SAMPLE = 5000


@dataclass
class ColumnStat:
    name: str
    declared: str
    nonnull: int
    distinct: int
    avg_len: float
    date_like: float          # 値が日付として読める割合
    numeric_like: float
    opaque: float = 0.0       # JSON/URLなど、自然文でない割合

    @property
    def distinct_ratio(self) -> float:
        return self.distinct / self.nonnull if self.nonnull else 0.0


@dataclass
class Target:
    table: str
    text_col: str | None
    label_col: str | None
    time_col: str | None
    rows: int
    columns: list[ColumnStat] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)
    label_is_content: bool = True


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.create_function("_len", 1, lambda s: len(s) if isinstance(s, str) else 0)
    return conn


def list_tables(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    out = []
    for n in names:
        try:
            c = conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
        except sqlite3.Error:
            continue
        out.append((n, c))
    return sorted(out, key=lambda x: -x[1])


def _stat(conn: sqlite3.Connection, table: str, col: str, declared: str) -> ColumnStat:
    q = f'SELECT "{col}" AS v FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT {SAMPLE}'
    vals = [r["v"] for r in conn.execute(q)]
    n = len(vals)
    strs = [v for v in vals if isinstance(v, str)]
    date_like = sum(1 for v in strs if DATE_VAL.match(v)) / n if n else 0.0
    opaque = sum(1 for v in strs if v[:1] in "{[" or v[:4] == "http") / n if n else 0.0
    num = sum(1 for v in vals if isinstance(v, (int, float))) / n if n else 0.0
    avg_len = sum(len(v) for v in strs) / len(strs) if strs else 0.0
    distinct = len({v for v in vals})
    return ColumnStat(col, declared, n, distinct, avg_len, date_like, num, opaque)


def profile(conn: sqlite3.Connection, table: str) -> list[ColumnStat]:
    cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [_stat(conn, table, c["name"], c["type"] or "") for c in cols]


def alignment(conn: sqlite3.Connection, table: str, text_col: str, label_col: str,
              limit: int = 2000) -> float:
    """ラベル値がそのまま本文に現れる割合。低いほど「名前だけの分類」。"""
    q = (f'SELECT "{label_col}" AS lab, "{text_col}" AS txt FROM "{table}" '
         f'WHERE "{label_col}" IS NOT NULL AND "{text_col}" IS NOT NULL LIMIT {limit}')
    rows = conn.execute(q).fetchall()
    if not rows:
        return 0.0
    hit = sum(1 for r in rows
              if isinstance(r["lab"], str) and isinstance(r["txt"], str)
              and r["lab"] in r["txt"])
    return hit / len(rows)


def infer(conn: sqlite3.Connection, table: str, text_col=None, label_col=None,
          time_col=None) -> Target:
    rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    stats = profile(conn, table)
    by = {s.name: s for s in stats}
    notes: list[str] = []

    if text_col is None:
        # 自由文＝長くて重複の少ないテキスト列。エージェントが検索語を当てる先。
        # JSON・URL は人が読む文でないので、機械的に長くても選ばない。
        # 列名が自由文を示すなら長さの下限を緩める（短い見出し列を落とさないため）
        cand = [s for s in stats
                if s.avg_len >= (8 if TEXT_NAME.search(s.name) else 12)
                and s.distinct_ratio > 0.15
                and s.numeric_like < 0.5 and s.date_like < 0.5 and s.opaque < 0.3
                and not OPAQUE_NAME.search(s.name)]
        if cand:
            named = [s for s in cand if TEXT_NAME.search(s.name)]
            text_col = max(named or cand, key=lambda s: s.avg_len).name
            notes.append(f"text_col={text_col} を推定（平均長 {by[text_col].avg_len:.0f}字"
                         f"{'・列名一致' if named else ''}）")

    if label_col is None:
        # 分類列＝値の種類が少なく繰り返す文字列。0件判定の入口になりやすい。
        cand = [s for s in stats if s.name != text_col and s.avg_len > 0
                and 2 <= s.distinct <= 120 and s.distinct_ratio < 0.35
                and s.numeric_like < 0.5 and s.date_like < 0.5
                and not OPAQUE_NAME.search(s.name)]
        if cand:
            # 監査したいのは「中身を説明していると称する列」。本文との語彙一致で選ぶ。
            scored = [(alignment(conn, table, text_col, s.name), s) for s in cand] \
                if text_col else [(0.0, s) for s in cand]
            scored.sort(key=lambda x: (1 if NON_CONTENT_NAME.search(x[1].name) else 0,
                                       -x[0], 0 if LABEL_NAME.search(x[1].name) else 1,
                                       -x[1].distinct))
            align, best = scored[0]
            label_col = best.name
            notes.append(f"label_col={label_col} を推定（{best.distinct}種・本文語彙一致 "
                         f"{align:.0%}）")
            if len(scored) > 1:
                notes.append("他の候補: " + ", ".join(
                    f"{s.name}({a:.0%})" for a, s in scored[1:4]))

    if time_col is None:
        cand = [s for s in stats if s.date_like > 0.8]
        cand.sort(key=lambda s: (0 if TIME_NAME.search(s.name) else 1, -s.date_like))
        if cand:
            time_col = cand[0].name
            notes.append(f"time_col={time_col} を推定")

    t = Target(table, text_col, label_col, time_col, rows, stats, notes)
    if label_col and NON_CONTENT_NAME.search(label_col):
        t.label_is_content = False
        notes.append(f"label_col={label_col} は経路/状態列とみなす"
                     f"（内容分類として監査するなら --label-col で明示）")
    return t
