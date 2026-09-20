#!/usr/bin/env python3
"""fn_audit — エージェント向けデータ源の偽陰性監査。

スキーマでもセキュリティでもなく、**返ってくる答えが「無い」を偽装しないか**を見る。
出力は人向けテキストと、ツール応答へ同梱できる機械可読の警告（agent_brief）の2つ。

  python3 cli.py --db data.sqlite --table events
  python3 cli.py --db feed.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import detectors  # noqa: E402
import introspect  # noqa: E402
import loaders  # noqa: E402

MARK = {"critical": "!!", "high": "! ", "medium": "~ ", "info": "  "}


def audit(db: str, table: str | None = None, text_col=None, label_col=None,
          time_col=None, sample=15000, pairs=None) -> dict:
    conn, default_table = loaders.open_source(db, table)
    if default_table:
        table = default_table      # ファイル由来は1テーブルしか持たない
    tables = introspect.list_tables(conn)
    if not tables:
        raise SystemExit(f"テーブルが無い: {db}")
    if table is None:
        table = tables[0][0]
    elif table not in {n for n, _ in tables}:
        raise SystemExit(f"テーブル {table} が無い。候補: {[n for n, _ in tables][:10]}")

    t = introspect.infer(conn, table, text_col, label_col, time_col)
    findings = detectors.run_all(conn, t, sample=sample, pairs=pairs)
    conn.close()

    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    return {
        "schema_version": 1,
        "audited_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"db": db, "table": table, "rows": t.rows,
                   "text_col": t.text_col, "label_col": t.label_col,
                   "time_col": t.time_col, "inference_notes": t.inferred,
                   "label_is_content": t.label_is_content},
        "summary": {"findings": len(findings), "by_severity": by_sev,
                    "trust_verdict": verdict(by_sev)},
        "findings": findings,
        "agent_brief": agent_brief(t, findings),
        "agent_brief_en": agent_brief(t, findings, "en"),
    }


def verdict(by_sev: dict) -> str:
    if by_sev.get("critical"):
        return "unsafe-for-counting"   # 集計・不在判定に使ってはいけない
    if by_sev.get("high"):
        return "needs-guardrails"      # 同義語展開と観測範囲の同梱が要る
    if by_sev.get("medium"):
        return "minor-gaps"
    return "no-false-negative-signals-found"


def agent_brief(t, findings: list[dict], lang: str = "ja") -> str:
    """ツールの応答に同梱する前提。エージェントが0件を不在と読む前に必ず読む文面。"""
    ttl = "title" if lang == "ja" else "title_en"
    imp = "agent_impact" if lang == "ja" else "agent_impact_en"
    lines = [f'[{f["severity"]}] {f[ttl]} -> {f[imp]}'
             for f in findings if f["severity"] in ("critical", "high")]
    horizon = next((f for f in findings if f["detector"] == "FN-5 coverage-horizon"), None)
    if horizon:
        e = horizon["evidence"]
        lines.insert(0, (
            f'このデータ源の観測範囲は {e["first_date"]}〜{e["last_date"]}。'
            f'範囲外は必ず0件になるが、それは事象の不在を意味しない。') if lang == "ja" else (
            f'This source observes {e["first_date"]} to {e["last_date"]}. Anything outside '
            f'that window returns zero by construction, which is not evidence of absence.'))
    if not lines:
        return ("既知の偽陰性要因は検出されていない。ただし未観測の要因が無いことは保証しない。"
                if lang == "ja" else
                "No known false-negative signals were found. That is not a guarantee that "
                "none exist.")
    return "\n".join(lines)


def render(rep: dict) -> str:
    s, out = rep["source"], []
    out.append(f'fn_audit  {s["db"]} :: {s["table"]}  ({s["rows"]:,} 行)')
    out.append(f'  text={s["text_col"]}  label={s["label_col"]}  time={s["time_col"]}')
    for n in s["inference_notes"]:
        out.append(f'  推定 / inferred: {n["ja"]}')
        out.append(f'                   {n["en"]}')
    out.append("")
    sm = rep["summary"]
    out.append(f'判定 / verdict: {sm["trust_verdict"]}   '
               f'指摘 {sm["findings"]}件 / {sm["findings"]} findings  {sm["by_severity"]}')
    out.append("")
    for f in rep["findings"]:
        out.append(f'{MARK[f["severity"]]}[{f["severity"]:8}] {f["detector"]}')
        out.append(f'     JA  {f["title"]}')
        out.append(f'         影響: {f["agent_impact"]}')
        out.append(f'         検証: {f["suggested_probe"]}')
        out.append(f'     EN  {f["title_en"]}')
        out.append(f'         impact: {f["agent_impact_en"]}')
        out.append(f'         probe:  {f["suggested_probe_en"]}')
        out.append("")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="エージェント向けデータ源の偽陰性監査")
    p.add_argument("--db", required=True,
                   help=".db/.sqlite, .csv/.tsv, .json/.jsonl, または postgresql:// DSN")
    p.add_argument("--table")
    p.add_argument("--text-col")
    p.add_argument("--label-col")
    p.add_argument("--time-col")
    p.add_argument("--sample", type=int, default=15000)
    p.add_argument("--lexicon", help="対義語ペアのJSON（[[表,裏],...] か "
                                    '{"pairs":[...],"replace":true}）')
    p.add_argument("--json", dest="json_out")
    p.add_argument("--list-tables", action="store_true")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args()

    pairs = None
    if a.lexicon:
        doc = json.loads(Path(a.lexicon).read_text(encoding="utf-8"))
        extra = [tuple(x) for x in (doc["pairs"] if isinstance(doc, dict) else doc)]
        pairs = extra if (isinstance(doc, dict) and doc.get("replace")) \
            else detectors.POLARITY_PAIRS + extra

    if a.list_tables:
        conn, _ = loaders.open_source(a.db, a.table)
        for n, c in introspect.list_tables(conn):
            print(f"{c:>9,}  {n}")
        return 0

    rep = audit(a.db, a.table, a.text_col, a.label_col, a.time_col, a.sample, pairs)
    if not a.quiet:
        print(render(rep))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=2))
        print(f"written: {a.json_out}")
    return 2 if rep["summary"]["by_severity"].get("critical") else 0


if __name__ == "__main__":
    sys.exit(main())
