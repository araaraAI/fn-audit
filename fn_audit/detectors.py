#!/usr/bin/env python3
"""偽陰性（エージェントが「無い」と誤読する）を生む構造を検出する。

対象は学習用データセットではなく、**稼働中のエージェントが問い合わせるデータ源**。
すべて決定的な集計で判定し、LLMを使わない。再現しない指摘は指摘ではないため。
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import date, timedelta

from mining import LabelMiner

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}

# 対義語。片側だけが存在するデータ源は、数えた時点で結論が傾く。
# --lexicon で差し替え可能。ここは日本語の開示・財務文脈＋汎用英語の最小セット。
POLARITY_PAIRS = [
    ("上方修正", "下方修正"), ("増配", "減配"), ("増益", "減益"), ("増収", "減収"),
    ("黒字", "赤字"), ("復配", "無配"), ("上昇", "下落"), ("増加", "減少"),
    ("取得", "売却"), ("締結", "解消"), ("承認", "否決"), ("開始", "中止"),
    ("改善", "悪化"), ("拡大", "縮小"), ("達成", "未達"), ("好調", "不振"),
    ("increase", "decrease"), ("upgrade", "downgrade"), ("profit", "loss"),
    ("growth", "decline"), ("approved", "rejected"), ("success", "failure"),
    ("surplus", "deficit"), ("positive", "negative"),
]

CATCHALL = re.compile(r"(その他|other|misc|unknown|未分類|不明|n/?a|etc)", re.I)
ROUND_NUMBERS = {20, 25, 50, 100, 200, 250, 300, 500, 1000, 2000, 5000, 10000}


def _f(detector, severity, title, evidence, impact, probe) -> dict:
    """title / impact / probe は {"ja": ..., "en": ...}。日英を同じ指摘に必ず持たせる。

    片方だけ更新して食い違うのを防ぐため、欠けた言語は組み立て時に落とす。
    """
    for label, d in (("title", title), ("impact", impact), ("probe", probe)):
        if set(d) != {"ja", "en"}:
            raise ValueError(f"{detector}: {label} に ja/en が揃っていない: {sorted(d)}")
    return {"detector": detector, "severity": severity,
            "title": title["ja"], "title_en": title["en"],
            "evidence": evidence,
            "agent_impact": impact["ja"], "agent_impact_en": impact["en"],
            "suggested_probe": probe["ja"], "suggested_probe_en": probe["en"]}


def _like(conn, table, col, term) -> int:
    return conn.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" LIKE ?', (f"%{term}%",)
    ).fetchone()[0]


# ---------------------------------------------------------------- FN-1
def label_text_gap(conn, t, miner: LabelMiner, min_rows=20, column_alignment=0.0) -> list[dict]:
    """ラベル名がテキストに現れない＝その語で検索したエージェントは0件を受け取る。

    ただし「代わりに何を検索すべきか」を示せない指摘は出さない。状態列（status 等）は
    そもそも本文を説明しておらず、そこでの不一致は欠陥ではないため。
    """
    out = []
    labels = conn.execute(
        f'SELECT "{t.label_col}" AS lab, COUNT(*) AS n FROM "{t.table}" '
        f'WHERE "{t.label_col}" IS NOT NULL GROUP BY 1 ORDER BY 2 DESC').fetchall()
    for r in labels:
        lab, n = r["lab"], r["n"]
        if not isinstance(lab, str) or n < min_rows or CATCHALL.search(lab):
            continue
        full = _like(conn, t.table, t.text_col, lab)
        best_part, best_hits = None, 0
        for size in range(len(lab), 1, -1):
            for i in range(len(lab) - size + 1):
                piece = lab[i:i + size]
                if SYMBOL_ONLY.match(piece):
                    continue
                h = _like(conn, t.table, t.text_col, piece)
                if h > best_hits:
                    best_part, best_hits = piece, h
            if best_hits >= n * 0.5:
                break
        cov = full / n
        if cov >= 0.2:
            continue
        actual = miner.top_terms(lab, k=4)
        dominant = next((a for a in actual
                         if a["coverage"] >= 0.5 and a["precision"] >= 0.6), None)
        if dominant:
            sev = ("critical" if n >= 100 else "high") if cov == 0 else "high"
        elif column_alignment >= 0.3:
            sev = "medium"      # 列全体は一致しているのに、このラベルだけ外れている
        else:
            continue            # 本文を説明していない列。指摘にならない
        out.append(_f(
            "FN-1 vocabulary-gap", sev,
            {"ja": f'ラベル「{lab}」({n}件) の表記が本文に無い（一致率 {cov:.0%}）',
             "en": f'Label "{lab}" ({n} rows) never appears in the text '
                   f'({cov:.0%} of its rows contain it)'},
            {"label": lab, "rows": n, "exact_hits_in_text": full,
             "best_partial": best_part, "best_partial_hits": best_hits,
             "actual_notation": [a["term"] for a in actual]},
            {"ja": f'「{lab}」で検索したエージェントは {n} 件を取りこぼし、'
                   f'「該当なし」と結論する（実際には存在する）',
             "en": f'An agent searching for "{lab}" misses {n} rows and concludes '
                   f'"no such records", although they exist'},
            {"ja": (f'代替表記の候補「{dominant["term"]}」'
                    f'（このラベルの {dominant["coverage"]:.0%} に出現／その語の '
                    f'{dominant["precision"]:.0%} がこのラベル）。同義語として登録する前に '
                    f'1件だけ中身を確認する') if dominant else
                   f'代替表記の候補: {[a["term"] for a in actual] or [best_part]}（要確認）',
             "en": (f'Candidate wording: "{dominant["term"]}" (appears in '
                    f'{dominant["coverage"]:.0%} of this label; {dominant["precision"]:.0%} '
                    f'of rows containing it carry this label). Check one row before '
                    f'registering it as a synonym') if dominant else
                   f'Candidate wording: {[a["term"] for a in actual] or [best_part]} '
                   f'(unverified)'}))
    return out


SYMBOL_ONLY = re.compile(r"^[\s\d０-９()（）、。・,\.\-/:：　]*$")


# ---------------------------------------------------------------- FN-2
def polarity_asymmetry(conn, t, pairs=POLARITY_PAIRS, min_count=20) -> list[dict]:
    """対義語の片側欠落。件数を数えるだけで結論が一方向に傾く。"""
    out = []
    for pos, neg in pairs:
        for col, kind in ((t.label_col, "label"), (t.text_col, "text")):
            if not col:
                continue
            a, b = _like(conn, t.table, col, pos), _like(conn, t.table, col, neg)
            hi, lo = max(a, b), min(a, b)
            if hi < min_count:
                continue
            missing = neg if a > b else pos
            present = pos if a > b else neg
            if lo == 0:
                sev = "critical"
            elif hi / lo >= 20:
                sev = "high"
            else:
                continue
            out.append(_f(
                "FN-2 polarity-asymmetry", sev,
                {"ja": f'{kind}列に「{present}」{hi}件 / 「{missing}」{lo}件（比 '
                       f'{"∞" if lo == 0 else f"{hi/lo:.0f}:1"}）',
                 "en": f'{kind} column holds "{present}" x{hi} vs "{missing}" x{lo} '
                       f'(ratio {"infinite" if lo == 0 else f"{hi/lo:.0f}:1"})'},
                {"column": col, "kind": kind, "present": present, "present_count": hi,
                 "missing": missing, "missing_count": lo},
                {"ja": f'この列で件数を集計すると構造的に「{present}」側へ偏る。'
                       f'センチメントや趨勢の判断に使うと逆方向の結論を出す',
                 "en": f'Any count over this column is structurally skewed toward '
                       f'"{present}". Used for sentiment or trend, it inverts the '
                       f'conclusion'},
                {"ja": f'「{missing}」の事象が本当に無いのか、別表記で入っているのかを '
                       f'原データ（本文・一次資料）で1件だけ確かめる',
                 "en": f'Check one primary record to see whether "{missing}" events are '
                       f'truly absent or merely worded differently'}))
    return out


# ---------------------------------------------------------------- FN-3
def label_drift(conn, t, miner: LabelMiner, min_rows=30) -> list[dict]:
    """ラベル名と中身の乖離。名前を額面どおり信じたエージェントが誤る。"""
    out = []
    for lab, n in miner.by_label.items():
        if not isinstance(lab, str) or n < min_rows or CATCHALL.search(lab):
            continue
        terms = [x for x in miner.top_terms(lab, k=6) if x["precision"] >= 0.6]
        if not terms:
            continue
        top = terms[0]
        # ラベル名（かその一部）が上位語に含まれるなら乖離ではない
        named = any(lab in x["term"] or x["term"] in lab for x in terms)
        if named or top["coverage"] < 0.5:
            continue
        out.append(_f(
            "FN-3 label-drift", "high" if top["coverage"] >= 0.7 else "medium",
            {"ja": f'ラベル「{lab}」({n}件) の中身は主に「{top["term"]}」'
                   f'（{top["coverage"]:.0%}）',
             "en": f'Label "{lab}" ({n} rows) is mostly about "{top["term"]}" '
                   f'({top["coverage"]:.0%})'},
            {"label": lab, "rows": n,
             "dominant_terms": [{"term": x["term"], "coverage": x["coverage"],
                                 "precision": x["precision"]} for x in terms[:4]]},
            {"ja": f'「{lab}」を額面どおり解釈したエージェントは、{n} 件の中身が '
                   f'「{top["term"]}」であることに気づかないまま集計する',
             "en": f'An agent taking "{lab}" at face value aggregates {n} rows without '
                   f'noticing they are actually "{top["term"]}"'},
            {"ja": f'ラベルに注記を付ける、または「{top["term"]}」で再問い合わせして中身を確認する',
             "en": f'Annotate the label, or re-query with "{top["term"]}" to see what the '
                   f'rows really are'}))
    return out


# ---------------------------------------------------------------- FN-4
def catchall_composition(conn, t, miner: LabelMiner) -> list[dict]:
    """「その他」に実は名前のある塊が隠れている場合、その塊は検索で到達できない。"""
    out = []
    for lab, n in miner.by_label.items():
        if not isinstance(lab, str) or not CATCHALL.search(lab) or n < 30:
            continue
        terms = miner.top_terms(lab, k=6, min_lift=2.0)
        big = [x for x in terms if x["coverage"] >= 0.1]
        if not big:
            continue
        share, share_rows = miner.union_coverage(lab, [x["term"] for x in big[:3]])
        out.append(_f(
            "FN-4 catchall-hiding-categories",
            "high" if share >= 0.4 else "medium",
            {"ja": f'包括ラベル「{lab}」({n}件) の中身は雑多でない（上位3語で約{share:.0%}）',
             "en": f'Catch-all label "{lab}" ({n} rows) is not miscellaneous: its top 3 '
                   f'terms cover about {share:.0%} of it'},
            {"label": lab, "rows": n, "explained_share": round(share, 3),
             "explained_rows": share_rows,
             "hidden_groups": [{"term": x["term"], "coverage": x["coverage"],
                                "rows": x["df"]} for x in big[:5]]},
            {"ja": f'「{lab}」は無視されやすいラベルであるため、この中の '
                   f'約{share_rows}件の実在イベントにエージェントは到達しない',
             "en": f'Agents skip a label named "{lab}", so roughly {share_rows} real '
                   f'events buried in it are never reached'},
            {"ja": f'まず「{big[0]["term"]}」を本文検索して、独立したラベルが要るか判断する',
             "en": f'Search the text for "{big[0]["term"]}" and decide whether it deserves '
                   f'its own label'}))
    return out


# ---------------------------------------------------------------- FN-5
def temporal_coverage(conn, t, quiet_factor=3.0) -> list[dict]:
    """「0件」が不在なのか未観測なのかを、エージェントが判別する材料を作る。"""
    if not t.time_col:
        return []
    rows = conn.execute(
        f'SELECT substr("{t.time_col}",1,10) AS d, COUNT(*) AS n FROM "{t.table}" '
        f'WHERE "{t.time_col}" IS NOT NULL GROUP BY 1 ORDER BY 1').fetchall()
    days = [(r["d"], r["n"]) for r in rows if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}$", r["d"] or "")]
    if not days:
        return []
    out = []
    parse = lambda s: date(*map(int, re.split(r"[-/]", s)))
    first, last = parse(days[0][0]), parse(days[-1][0])
    span = (last - first).days + 1
    present = {parse(d) for d, _ in days}
    missing = span - len(present)

    out.append(_f(
        "FN-5 coverage-horizon", "info",
        {"ja": f'観測範囲 {first} 〜 {last}（{span}日、実データのある日 {len(present)}日）',
         "en": f'Observed window {first} to {last} ({span} days, {len(present)} of them '
               f'with data)'},
        {"first_date": str(first), "last_date": str(last), "span_days": span,
         "days_with_data": len(present), "days_without_data": missing,
         "rows": sum(n for _, n in days)},
        {"ja": f'{first} より前を問い合わせたエージェントは必ず0件を得る。'
               f'これは「事象が無い」ではなく「このデータ源が持っていない」',
         "en": f'Any query before {first} returns zero by construction. That means this '
               f'source does not hold it, not that nothing happened'},
        {"ja": '0件応答に必ずこの観測範囲を同梱する',
         "en": 'Always attach this window to a zero-result response'}))

    if len(days) < 5:
        return out          # 空白・沈黙の判定は最低5日ないと意味を持たない

    # 最長の空白。休日由来か障害由来かは区別しないが、長さは事実として返す。
    longest, run, prev = 0, 0, None
    for d in sorted(present):
        if prev and (d - prev).days > 1:
            run = (d - prev).days - 1
            longest = max(longest, run)
        prev = d
    if longest >= 7:
        out.append(_f("FN-5 coverage-hole", "medium",
                      {"ja": f'最長 {longest} 日の空白がある',
                       "en": f'Longest stretch without any row: {longest} days'},
                      {"longest_gap_days": longest},
                      {"ja": '空白期間を問い合わせたエージェントは0件を「不在」と読む',
                       "en": 'An agent querying inside the gap reads zero as absence'},
                      {"ja": '空白が休日か収集停止かを、期間を指定して原データで確かめる',
                       "en": 'Check the primary source for that period to tell holidays '
                             'from a halted collector'}))

    # 末尾の沈黙。取り込みが止まったまま気づかれない状態を、0件の前に伝える。
    med = sorted(n for _, n in days)[len(days) // 2]
    stale = (date.today() - last).days
    typical_gap = span / max(len(present), 1)
    if stale > max(quiet_factor * typical_gap, 3):
        out.append(_f("FN-5 stale-tail", "high",
                      {"ja": f'最終データが {stale} 日前（通常間隔 {typical_gap:.1f}日、'
                             f'中央値 {med}件/日）',
                       "en": f'Newest row is {stale} days old (typical gap '
                             f'{typical_gap:.1f} days, median {med} rows/day)'},
                      {"days_since_last": stale, "typical_gap_days": round(typical_gap, 2),
                       "median_rows_per_day": med},
                      {"ja": '直近を問い合わせたエージェントは0件を得るが、それは'
                             '事象の不在ではなく取り込みの停止である可能性が高い',
                       "en": 'A query for the recent past returns zero, most likely '
                             'because ingestion stopped, not because nothing happened'},
                      {"ja": '上流の収集ジョブの最終実行時刻を確認する',
                       "en": 'Check when the upstream collector last ran'}))
    return out


# ---------------------------------------------------------------- FN-6
def count_ceiling(conn, t) -> list[dict]:
    """1回の取り込み上限に当たっていないか。上限は静かに削るので0件より気づきにくい。"""
    if not t.time_col:
        return []
    rows = conn.execute(
        f'SELECT substr("{t.time_col}",1,10) AS d, COUNT(*) AS n FROM "{t.table}" '
        f'WHERE "{t.time_col}" IS NOT NULL GROUP BY 1').fetchall()
    counts = [r["n"] for r in rows if r["d"]]
    if len(counts) < 10:
        return []
    top = max(counts)
    hits = sum(1 for c in counts if c == top)
    if top in ROUND_NUMBERS and hits >= 3:
        return [_f("FN-6 count-ceiling", "high",
                   {"ja": f'1日あたり件数が {top} で {hits} 回頭打ちになっている',
                    "en": f'Daily row count plateaus at exactly {top} on {hits} days'},
                   {"ceiling": top, "days_at_ceiling": hits, "days_observed": len(counts)},
                   {"ja": '上限に当たった日は静かに切り捨てられている。'
                          'エージェントは欠けた分を「その日は起きなかった」と読む',
                    "en": 'Rows past the cap were dropped silently; an agent reads the '
                          'missing ones as events that never happened'},
                   {"ja": f'上流の取得処理に LIMIT {top} が無いか確認する',
                    "en": f'Look for a LIMIT {top} in the upstream fetch'})]
    return []


def run_all(conn, t, sample=15000, pairs=None) -> list[dict]:
    findings: list[dict] = []
    miner = None
    if t.text_col and t.label_col:
        miner = LabelMiner(conn, t.table, t.text_col, t.label_col, sample=sample)
        miner.build()
        align = sum(1 for lab, txt in miner.rows
                    if isinstance(lab, str) and lab in txt) / max(miner.total, 1)
        findings.append(_f(
            "FN-0 taxonomy-alignment", "info",
            {"ja": f'ラベル列「{t.label_col}」と本文「{t.text_col}」の語彙一致率 {align:.0%}',
             "en": f'Vocabulary overlap between label column "{t.label_col}" and text '
                   f'"{t.text_col}": {align:.0%}'},
            {"label_col": t.label_col, "text_col": t.text_col,
             "alignment": round(align, 3)},
            {"ja": 'この値が低いほど、ラベル名をそのまま検索語にしたエージェントは空振りする',
             "en": 'The lower this is, the more often searching by label name returns '
                   'nothing'},
            {"ja": '一致率が低いのに分類列として提供するなら、同義語展開を必ず挟む',
             "en": 'If you expose this as a taxonomy despite low overlap, put synonym '
                   'expansion in front of it'}))
        if t.label_is_content:
            findings += label_text_gap(conn, t, miner, column_alignment=align)
            findings += label_drift(conn, t, miner)
            findings += catchall_composition(conn, t, miner)
        else:
            findings.append(_f(
                "FN-0 non-content-label", "info",
                {"ja": f'ラベル列「{t.label_col}」は経路/状態列と判定したため語彙監査を行わない',
                 "en": f'Label column "{t.label_col}" looks like a routing/state column, '
                       f'so vocabulary checks were skipped'},
                {"label_col": t.label_col},
                {"ja": 'この列の値（配信先・処理状態など）は本文に現れないのが正常で、'
                       '不一致は欠陥ではない',
                 "en": 'Values like a destination or a processing state are not supposed '
                       'to appear in the text; a mismatch here is not a defect'},
                {"ja": '内容分類の列が別にあるなら --label-col で指定して再実行する',
                 "en": 'If a real content taxonomy exists elsewhere, rerun with '
                       '--label-col'}))
    if t.text_col or t.label_col:
        findings += polarity_asymmetry(conn, t, pairs or POLARITY_PAIRS)
    findings += temporal_coverage(conn, t)
    findings += count_ceiling(conn, t)
    findings.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["detector"]))
    return findings
