#!/usr/bin/env python3
"""ラベルごとの「実際の表記」を、形態素解析器も辞書も使わずに抽出する。

文字N-gramの偏り（lift = ラベル内出現率 / 補集合の出現率）で拾う。補集合と比べるのは、
支配的なラベルを全体比で見ると lift が 1 に潰れて何も出なくなるため（本体 D-008 と同じ理由）。
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter

NGRAM_MIN, NGRAM_MAX = 2, 8          # 日本語など分かち書きしない言語用の文字N-gram
WORD_NGRAM_MAX = 3                   # 空白で区切る言語用の語N-gram
TEXT_CLIP = 120
WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*|\d+")
# 語N-gramの両端に来ても意味を持たない語。日本語側のひらがな境界推定に対応する処置。
EDGE_WORDS = {"the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "with",
              "by", "at", "from", "its", "this", "that", "as", "is", "are", "be"}
SYMBOLS = re.compile(r"^[\s\d０-９()（）「」『』、。・．,\.\-－―/／：:；;％%　\[\]<>|*#_~`'\"＊＃]*$")


def _trim_edges(words: list[str]) -> str:
    w = list(words)
    while w and w[0].lower() in EDGE_WORDS:
        w.pop(0)
    while w and w[-1].lower() in EDGE_WORDS:
        w.pop()
    return " ".join(w)


class LabelMiner:
    def __init__(self, conn: sqlite3.Connection, table: str, text_col: str,
                 label_col: str, sample: int = 15000):
        q = (f'SELECT "{label_col}" AS lab, "{text_col}" AS txt FROM "{table}" '
             f'WHERE "{text_col}" IS NOT NULL LIMIT {sample}')
        self.rows = [(r["lab"], (r["txt"] or "")[:TEXT_CLIP]) for r in conn.execute(q)]
        self.total = len(self.rows)
        self.by_label: dict[str, int] = Counter(lab for lab, _ in self.rows)
        self._global: Counter[str] = Counter()
        self._per_label: dict[str, Counter[str]] = {}
        self._built = False

    @staticmethod
    def _spaced(text: str) -> bool:
        """空白で語を区切る言語か。文字N-gramを英文に当てると語中で切れるため。"""
        if " " not in text:
            return False
        letters = sum(1 for c in text if c.isascii() and c.isalpha())
        return letters >= 0.5 * max(len(text.replace(" ", "")), 1)

    @classmethod
    def ngrams(cls, text: str) -> set[str]:
        if cls._spaced(text):
            words = WORD.findall(text)
            out = set()
            for n in range(1, WORD_NGRAM_MAX + 1):
                for i in range(len(words) - n + 1):
                    g = _trim_edges(words[i:i + n])
                    if g and not SYMBOLS.match(g):
                        out.add(g)
            return out
        t = re.sub(r"[\s　]+", "", text)
        out = set()
        for n in range(NGRAM_MIN, NGRAM_MAX + 1):
            for i in range(len(t) - n + 1):
                g = t[i:i + n]
                if not SYMBOLS.match(g):
                    out.add(g)
        return out

    def build(self) -> None:
        if self._built:
            return
        for lab, txt in self.rows:
            grams = self.ngrams(txt)
            self._global.update(grams)
            self._per_label.setdefault(lab, Counter()).update(grams)
        self._built = True

    def union_coverage(self, label: str, terms: list[str]) -> tuple[float, int]:
        """複数語が重なって数えられるのを避ける。行単位の和集合で測る。"""
        rows = [t for lab, t in self.rows if lab == label]
        if not rows:
            return 0.0, 0
        hit = sum(1 for t in rows if any(x in t for x in terms))
        return hit / len(rows), hit

    def top_terms(self, label: str, k: int = 8, min_df: int = 5,
                  min_lift: float = 3.0) -> list[dict]:
        """そのラベルに偏って現れる語。定型句は lift で自動的に落ちる。"""
        self.build()
        n_lab = self.by_label.get(label, 0)
        if n_lab < min_df:
            return []
        local = self._per_label.get(label, Counter())
        n_other = self.total - n_lab
        scored = []
        for term, df in local.items():
            if df < min_df:
                continue
            other_df = self._global[term] - df
            p_lab = df / n_lab
            p_other = (other_df / n_other) if n_other else 0.0
            lift = p_lab / p_other if p_other else float("inf")
            if lift < min_lift:
                continue
            scored.append({"term": term, "df": df, "coverage": round(p_lab, 3),
                           # precision = その語を含む行のうち、このラベルである割合。
                           # 同義語は双方向でなければならない（片方向だと汎用語を拾う）。
                           "precision": round(df / self._global[term], 3),
                           "lift": (round(lift, 1) if lift != float("inf") else None)})
        # 同件数で重なる語は1つに畳む。助詞をまたいだ窓が残ると実表記として読めないため、
        # ひらがなの少ないものを優先する（形態素解析器を持たない代わりの境界推定）。
        noise = lambda w: (sum(1 for ch in w if "\u3041" <= ch <= "\u309f")
                           + sum(1 for x in w.split() if x.lower() in EDGE_WORDS))
        scored.sort(key=lambda s: (-s["df"], noise(s["term"]), -len(s["term"])))
        kept: list[dict] = []
        for s in scored:
            if any(s["df"] == k2["df"] and (s["term"] in k2["term"] or k2["term"] in s["term"])
                   for k2 in kept):
                continue
            kept.append(s)
            if len(kept) >= k:
                break
        return kept
