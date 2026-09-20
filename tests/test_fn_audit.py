#!/usr/bin/env python3
"""fn_audit regression tests.

Synthetic fixtures pin down when each detector fires. The upstream project also runs
these detectors against a real, private dataset; that suite is not part of this repo.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fn_audit"))
import cli  # noqa: E402
import detectors  # noqa: E402
import introspect  # noqa: E402



def build(rows, path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, kind TEXT, title TEXT, at TEXT)")
    c.executemany("INSERT INTO t (kind,title,at) VALUES (?,?,?)", rows)
    c.commit()
    c.close()


class SyntheticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _audit(self, rows, **kw):
        p = str(Path(self.tmp) / f"t{len(rows)}{abs(hash(str(rows[0])))}.db")
        build(rows, p)
        return cli.audit(p, "t", **kw)

    def _ids(self, rep):
        return {f["detector"] for f in rep["findings"]}

    def test_vocabulary_gap_detected(self):
        d = str(date.today())
        rows = [("買戻し", f"自己株式の取得に関する件 {i}", d) for i in range(120)]
        rows += [("決算", f"決算短信の開示 {i}", d) for i in range(60)]
        rep = self._audit(rows)
        gaps = [f for f in rep["findings"] if f["detector"] == "FN-1 vocabulary-gap"]
        self.assertTrue(any(g["evidence"]["label"] == "買戻し" for g in gaps))
        g = next(g for g in gaps if g["evidence"]["label"] == "買戻し")
        self.assertEqual(g["severity"], "critical")
        self.assertIn("自己株式", g["evidence"]["actual_notation"][0])

    def test_polarity_asymmetry_detected(self):
        d = str(date.today())
        rows = [("修正", f"通期業績予想の上方修正 {i}", d) for i in range(40)]
        rows += [("決算", f"決算短信 {i}", d) for i in range(40)]
        rep = self._audit(rows)
        pol = [f for f in rep["findings"] if f["detector"] == "FN-2 polarity-asymmetry"]
        self.assertTrue(pol)
        self.assertEqual(pol[0]["severity"], "critical")
        self.assertEqual(rep["summary"]["trust_verdict"], "unsafe-for-counting")

    def test_balanced_source_is_clean(self):
        d = str(date.today())
        rows = [("上方修正", f"上方修正のお知らせ {i}", d) for i in range(40)]
        rows += [("下方修正", f"下方修正のお知らせ {i}", d) for i in range(40)]
        rep = self._audit(rows)
        self.assertNotIn("FN-1 vocabulary-gap", self._ids(rep))
        self.assertNotIn("FN-2 polarity-asymmetry", self._ids(rep))

    def test_count_ceiling_detected(self):
        rows = []
        for k in range(15):
            day = str(date.today() - timedelta(days=k))
            n = 500 if k < 5 else 30
            rows += [("決算", f"決算短信 {k}-{i}", day) for i in range(n)]
        rep = self._audit(rows)
        ceil = [f for f in rep["findings"] if f["detector"] == "FN-6 count-ceiling"]
        self.assertTrue(ceil)
        self.assertEqual(ceil[0]["evidence"]["ceiling"], 500)

    def test_stale_tail_detected(self):
        rows = []
        for k in range(40, 20, -1):
            day = str(date.today() - timedelta(days=k))
            rows += [("決算", f"決算短信 {k}-{i}", day) for i in range(5)]
        rep = self._audit(rows)
        self.assertIn("FN-5 stale-tail", self._ids(rep))

    def test_non_content_label_column_is_skipped(self):
        """経路/状態列で語彙ギャップを出さない（post_log の偽陽性で判明した条件）。"""
        d = str(date.today())
        rows = [("telegram", f"BTC価格は本日も堅調 {i}", d) for i in range(200)]
        rows += [("discord", f"日経平均の動き {i}", d) for i in range(50)]
        p = str(Path(self.tmp) / "route.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, platform TEXT, title TEXT, at TEXT)")
        c.executemany("INSERT INTO t (platform,title,at) VALUES (?,?,?)", rows)
        c.commit(); c.close()
        rep = cli.audit(p, "t")
        self.assertIn("FN-0 non-content-label", self._ids(rep))
        self.assertNotIn("FN-1 vocabulary-gap", self._ids(rep))

    def test_english_text_uses_word_boundaries(self):
        """空白区切りの言語で語中を切らないこと（英語の実表記が読めなくなるため）。"""
        d = str(date.today())
        rows = [("buyback", f"Notice concerning the acquisition of treasury shares {i}", d)
                for i in range(120)]
        # 定型句を両カテゴリに持たせる。lift が定型句を落とすことも同時に確かめる。
        rows += [("results", f"Notice concerning the consolidated results {i}", d)
                 for i in range(40)]
        rep = self._audit(rows)
        gap = next(f for f in rep["findings"]
                   if f["detector"] == "FN-1 vocabulary-gap"
                   and f["evidence"]["label"] == "buyback")
        term = gap["evidence"]["actual_notation"][0]
        self.assertIn("treasury shares", term)
        self.assertFalse(term.split()[0].lower() in ("the", "of", "concerning"))

    def test_every_finding_carries_both_languages(self):
        """日英のどちらかだけを更新して食い違うのを防ぐ（_f が構築時に落とす）。"""
        d = str(date.today())
        rows = [("買戻し", f"自己株式の取得に関する件 {i}", d) for i in range(120)]
        rows += [("その他", f"通期業績予想の上方修正について {i}", d) for i in range(60)]
        rep = self._audit(rows)
        self.assertTrue(rep["findings"])
        for f in rep["findings"]:
            for k in ("title", "title_en", "agent_impact", "agent_impact_en",
                      "suggested_probe", "suggested_probe_en"):
                self.assertTrue(f.get(k), f'{f["detector"]} に {k} が無い')
            self.assertNotEqual(f["title"], f["title_en"])
        for n in rep["source"]["inference_notes"]:
            self.assertEqual(set(n), {"ja", "en"})

    def test_agent_brief_has_english_version(self):
        d = str(date.today() - timedelta(days=3))
        rows = [("決算", f"決算短信 {i}", str(date.today() - timedelta(days=k)))
                for k in range(10) for i in range(5)]
        rep = self._audit(rows)
        self.assertIn("観測範囲", rep["agent_brief"])
        self.assertIn("This source observes", rep["agent_brief_en"])

    def test_missing_language_is_rejected(self):
        import detectors as _d
        with self.assertRaises(ValueError):
            _d._f("FN-X test", "info", {"ja": "あ"}, {}, {"ja": "い", "en": "b"},
                  {"ja": "う", "en": "c"})

    def test_agent_brief_contains_horizon(self):
        d = str(date.today() - timedelta(days=3))
        rows = [("決算", f"決算短信 {i}", d) for i in range(30)]
        rep = self._audit(rows)
        self.assertIn("観測範囲", rep["agent_brief"])


class LoaderTest(unittest.TestCase):
    """SQLite 以外の入口でも同じ指摘が出ること。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        d = str(date.today())
        self.rows = [{"cat": "買戻し", "headline": f"自己株式の取得に係る決定 {i}",
                      "when": d} for i in range(120)]
        # ラベル列は2値以上ないと分類列と見なされない（単一値は分類ではない）
        self.rows += [{"cat": "決算", "headline": f"決算短信の開示 {i}", "when": d}
                      for i in range(60)]

    def test_csv(self):
        import csv as _csv
        p = self.tmp / "d.csv"
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=["cat", "headline", "when"])
            w.writeheader(); w.writerows(self.rows)
        rep = cli.audit(str(p))
        self.assertEqual(rep["source"]["text_col"], "headline")
        self.assertTrue(any(f["detector"] == "FN-1 vocabulary-gap"
                            for f in rep["findings"]))

    def test_jsonl(self):
        import json as _json
        p = self.tmp / "d.jsonl"
        p.write_text("\n".join(_json.dumps(r, ensure_ascii=False) for r in self.rows))
        rep = cli.audit(str(p))
        self.assertTrue(any(f["detector"] == "FN-1 vocabulary-gap"
                            for f in rep["findings"]))

    def test_json_wrapped_in_object(self):
        import json as _json
        p = self.tmp / "d.json"
        p.write_text(_json.dumps({"count": 180, "items": self.rows}, ensure_ascii=False))
        rep = cli.audit(str(p))
        self.assertEqual(rep["source"]["rows"], 180)

    def test_unsupported_extension_is_rejected(self):
        p = self.tmp / "d.parquet"
        p.write_bytes(b"x")
        with self.assertRaises(SystemExit):
            cli.audit(str(p))

    def test_custom_lexicon_pair(self):
        d = str(date.today())
        rows = [{"cat": "認可", "headline": f"申請が受理されました {i}", "when": d}
                for i in range(40)]
        rows += [{"cat": "報告", "headline": f"定例の報告です {i}", "when": d}
                 for i in range(20)]
        import csv as _csv
        p = self.tmp / "lex.csv"
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=["cat", "headline", "when"])
            w.writeheader(); w.writerows(rows)
        rep = cli.audit(str(p), pairs=[("受理", "却下")])
        pol = [f for f in rep["findings"] if f["detector"] == "FN-2 polarity-asymmetry"]
        self.assertTrue(pol)
        self.assertEqual(pol[0]["evidence"]["missing"], "却下")


if __name__ == "__main__":
    unittest.main(verbosity=2)
