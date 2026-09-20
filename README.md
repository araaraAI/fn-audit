# fn_audit

[![tests](https://github.com/araaraAI/fn-audit/actions/workflows/tests.yml/badge.svg)](https://github.com/araaraAI/fn-audit/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

**An agent cannot tell "the world contains no such record" from "the thing I called returned `[]`."**

`fn_audit` audits a *live* data source — the one your agent actually queries — for the
structures that manufacture that confusion. No LLM, no training, no network: only
deterministic counting, so every finding reproduces.

```
$ python3 fn_audit/cli.py --db examples/sample_feed.csv

判定 / verdict: unsafe-for-counting   指摘 6件 / 6 findings  {'critical': 1, 'high': 3, 'info': 2}

!![critical] FN-1 vocabulary-gap
     JA  ラベル「buyback」(180件) の表記が本文に無い（一致率 0%）
         影響: 「buyback」で検索したエージェントは 180 件を取りこぼし、「該当なし」と結論する
         検証: 代替表記の候補「treasury shares」（このラベルの 100% に出現／その語の 100% がこのラベル）
     EN  Label "buyback" (180 rows) never appears in the text (0% of its rows contain it)
         impact: An agent searching for "buyback" misses 180 rows and concludes
                 "no such records", although they exist
         probe:  Candidate wording: "treasury shares" (appears in 100% of this label;
                 100% of rows containing it carry this label)
```

Every finding is reported in both English and Japanese.

## Why this exists

Existing tools audit a different layer:

| Tool | Layer |
|---|---|
| MCProbe, MCPJam | MCP tool **schema** and protocol conformance |
| mcp-audit, OWASP mcps-audit | **Security** of server configuration |
| tabaudit, G-AUDIT | **Training datasets** — leakage, label noise, attribute bias |
| **fn_audit** | **The answers a live source returns** — do they fake absence? |

MCProbe states it "does not evaluate whether returned data is semantically correct,
covers necessary categories, matches vocabulary standards, or has appropriate result
boundaries." That gap is what this fills.

## What it detects

| ID | Signal | What the agent does wrong |
|---|---|---|
| FN-0 taxonomy-alignment | vocabulary overlap between the label column and the text | low overlap ⇒ searching by label name silently misses everything |
| FN-1 vocabulary-gap | label name never appears in the text | searches that term, gets 0, concludes "it never happened" |
| FN-2 polarity-asymmetry | one side of an antonym pair is missing | any count is structurally skewed — sentiment inverts |
| FN-3 label-drift | the label says X, the rows are mostly Y | reports the wrong kind of event |
| FN-4 catchall-hiding-categories | a real category is buried inside "other" | never reaches rows filed under a label it ignores |
| FN-5 coverage-horizon / hole / stale-tail | the observed window, its gaps, and a dead tail | reads *not observed* as *did not occur* |
| FN-6 count-ceiling | daily counts plateau on a round number | ingestion truncated silently; missing rows look like quiet days |

## Usage

```bash
python3 fn_audit/cli.py --db data.sqlite --list-tables
python3 fn_audit/cli.py --db data.sqlite --table events --json report.json
python3 fn_audit/cli.py --db feed.csv               # .csv .tsv .json .jsonl
python3 fn_audit/cli.py --db "postgresql://..." --table public.events
python3 fn_audit/cli.py --db data.sqlite --lexicon my_antonyms.json
```

Columns are inferred and the inference is printed; override with `--text-col`,
`--label-col`, `--time-col`. SQLite is opened read-only; PostgreSQL is sampled into
memory, never written to. Exit code is `2` when a `critical` finding exists.

Requires Python 3.10+. No dependencies (PostgreSQL support needs `psycopg`, optional).
A full machine-readable report for the bundled example lives at
[`examples/sample_report.json`](examples/sample_report.json), so you can see the output shape
without running anything.

## Output

- `findings[]` — each carries `evidence` (numbers), `agent_impact` (how an agent goes wrong)
  and `suggested_probe` (how to check it yourself). **Every finding is written in both
  English and Japanese** (`title` / `title_en`, `agent_impact` / `agent_impact_en`,
  `suggested_probe` / `suggested_probe_en`); a finding missing either language fails to
  construct.
- `summary.trust_verdict` — `unsafe-for-counting` / `needs-guardrails` / `minor-gaps` /
  `no-false-negative-signals-found`
- `agent_brief` (Japanese) and `agent_brief_en` — **meant to be embedded in your tool's own
  response**, so the agent reads the caveat before it reads a `0`

## Design promises

- **No LLM.** A finding that does not reproduce is not a finding.
- **No finding without a remedy.** A synonym candidate is reported only when it holds in
  both directions: it covers ≥50% of the label's rows *and* ≥60% of the rows containing it
  belong to that label. One-directional matching produced nonsense like `telegram → price`.
- **Routing and state columns are out of scope.** Values of `platform`, `status` and the
  like are not supposed to appear in the text; scolding them would train you to ignore the
  report. Override with `--label-col`.
- **No morphological analyser, no dictionary.** Character n-grams with lift for languages
  without spaces, word n-grams for languages with them, plus boundary heuristics
  (few hiragana / no leading articles).

## Known limits

- A suggested notation is a **candidate**, not a confirmed synonym. Check one row first.
- The antonym lexicon ships with a small Japanese finance/disclosure set plus generic
  English. Extend it with `--lexicon`.
- Content vs. routing column detection relies on column-name conventions.
- PostgreSQL support is written but has not been exercised against a live server.

## Origin

Extracted from a private project that serves Japanese timely-disclosure data to agents.
There, a hand audit had found five defects (a label whose wording appears in 0% of the
rows it names; a category whose contents are 79% something else; missing negative labels
that made every aggregate bullish). These detectors, given no domain knowledge, rediscovered
all five — that is the regression bar they are held to.

## License

MIT

---

# fn_audit（日本語）

**エージェントは「世界に存在しない」と「呼んだツールが空を返した」を区別できません。**
`fn_audit` は、エージェントが実際に問い合わせる**稼働中のデータ源**が、その誤読を
作り出す構造を持っていないかを検査します。LLMを使わず、決定的な集計だけで判定するため、
指摘は必ず再現します。

検出するのは7系統です。ラベルと本文の語彙一致率（FN-0）、ラベル名が本文に無い語彙ギャップ
（FN-1）、対義語の片側欠落（FN-2）、ラベルと中身の乖離（FN-3）、「その他」への埋没（FN-4）、
観測範囲・空白・末尾の沈黙（FN-5）、件数の頭打ち（FN-6）です。

指摘はすべて日英併記です（`title` / `title_en` のように `_en` 版を必ず持ちます）。
`agent_brief` は、自分のツールの応答に同梱して使うことを前提にした短文です。
エージェントが `0` を読む前に、その `0` の意味を読ませるためにあります。

設計上の約束は3つです。**LLMを使わない**こと、**代わりに何を検索すべきかを示せない指摘は
出さない**こと（被覆率と精度の双方向で裏を取ります）、**経路・状態列は監査対象にしない**
ことです。警告を形骸化させないための制約です。
