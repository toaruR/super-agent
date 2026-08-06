# S2 実証結果 — 設計の中心主張を実機で検証

- 実施日: 2026-08-06 / 実施場所: `probe/ws/`
- 目的: `docs/spec.md` §7 の「実行と判定の分離」が**実際に E-4 の偽failを消すか**を確かめる。
- 結論: **消えた。** ただし途中で設計に反映すべき新しい落とし穴を1つ発見した（§4）。

---

## 1. 比較: 素朴な設計 vs 本設計

同一の成果物（claude が書いた `fizz.py`。論理的に正しい）に対する判定。

| | 素朴な設計（E-4） | 本設計（S2） |
|---|---|---|
| レビュアへの指示 | 「実行して検証し合否を返せ」 | 「証拠を読め。実行するな」 |
| レビュアの権限 | 実行可 | read-only |
| レビュアが見たもの | 自分の環境 | CVEが固定した証拠 E-991 |
| 失敗原因の混入 | **あり**（python が無い） | なし |
| **最終判定** | **fail（偽）** | **pass_with_findings（正）** |

---

## 2. 実際の実行ログ

### ①事実収集（機械・LLM不関与）

```console
$ python fizz.py > evidence_E991.txt 2>&1 ; echo exit_code=$?
['1','2','Fizz','4','Buzz','Fizz','7','8','Fizz','Buzz','11','Fizz','13','14','FizzBuzz']
exit_code=0
```
→ 証拠 **E-991** を固定。これは誰の意見でもなく、レビュアの環境にも依存しない。

### ②解釈（codex, read-only, 実行禁止）

```json
{"findings":[{"severity":"high",
              "claim":"The run exits 0, but it prints a Python list representation on one line
                       rather than standard FizzBuzz output.",
              "cites":["E-991"]}],
 "unverifiable":[],
 "opinion_verdict":"fail"}
```

注目すべき点:
- 「python が見つからない」は**発生していない**。レビュアは実行していないため、環境が判定に混入しない。
- 代わりに**本物の指摘**を出した。出力が `['1','2',...]` というリスト表現であり、
  通常の FizzBuzz 出力（1行ずつ）ではない、という指摘は**正しい**。
  素朴な設計では環境エラーに埋もれてこの指摘は得られなかった。

### ③裁定（機械・規則）

```console
$ python adjudicate.py reviewer_out.json 0
{
  "verdict": "pass_with_findings",
  "cites": ["E-991"],
  "followups": ["...prints a Python list representation..."],
  "discarded": 0
}
--- reviewer's own opinion was: fail (NOT used for the ruling) ---
```

**レビュアは fail と言ったが、裁定は pass_with_findings。**
受入基準（exit 0）は満たされているので止めず、指摘は後続タスクとして起票する。
設計 §7.2 の表の通りに動いた。

---

## 3. 裁定4分類の網羅テスト（機械のみ・コストゼロ）

```
E-4 scenario: good code, reviewer says fail   -> pass_with_findings
uncited finding (P3)                          -> pass                  (証拠なき指摘は破棄)
real failure                                  -> fail
CVE broken                                    -> environment_error
reviewer down                                 -> judgment_unavailable
```

**G2（成果物由来の失敗と、環境/判定不能を切り分ける）が実際に機能している。**

---

## 4. §4 の落とし穴は「自然言語での禁止」が原因だった（後日訂正）

> **⚠ 本節の結論は 2026-08-06 に撤回された。** 詳細は `docs/evidence/0606-permission-control.md`。
>
> 下記は「実行するな」と**自然言語だけ**で伝えた場合の失敗であり、
> **「パス渡しが不可能」の証明ではなかった**。
> `--allowedTools "Read,Grep,Glob"` のようにツール権限を明示的に与えれば、
> レビュアは正常にファイルを読み、**埋め込み方式より良い指摘を出す**（波及の検出）。
>
> **教訓: 禁止は自然言語ではなくフラグで表現する。**
> 「1通り試して失敗した」ことを「不可能」と一般化してはならない。

### 4.1 当時の記録（原文）

最初の試行で、レビュアに「**いかなるコマンドも実行するな**」と指示したところ、こう返ってきた:

```json
{"findings":[],
 "unverifiable":["I could not inspect fizz.py or evidence_E991.txt without running a command;
                  therefore the acceptance criterion cannot be judged"],
 "opinion_verdict":"fail"}
```

**「実行するな」が「ファイルを読むな」としても解釈され、レビュー自体が不能になった。**
しかもこのとき `opinion_verdict` は `fail` である——**またしても成果物と無関係な fail**。

### 対策（`docs/spec.md` §5.5 の簡報がそのまま解になる）

レビュアにファイルを**探させない**。ハーネスが簡報に**中身を埋め込んで渡す**。

```
=== ARTIFACT UNDER REVIEW (src: fizz.py) ===   <ソース本文を埋め込み>
=== EVIDENCE E-991 (cmd: python fizz.py) ===   <証拠本文を埋め込み>
```

これで上記のレビューは成立した（§2②）。簡報は **1,078 バイト**。

> **教訓: 「レビュアは read-only」だけでは足りない。「レビュアはファイルシステムに触れる必要が無い」まで
> 詰めて初めて、判定が環境から独立する。** 簡報は文脈節約策であると同時に、**判定独立性の実装**でもある。

---

## 5. G1（交換可能性）の実証

同じ簡報・同じスキーマを、レビュア役を **codex → agy** に差し替えて投入。

| ベンダー | 結果 |
|---|---|
| codex | 「リスト表現であり標準的なFizzBuzz出力ではない」`cites:["E-991"]` |
| **agy** | 「`['1','2','Fizz',...]` というリスト表現を出力しており、通常の行区切りテキストではない」`cites:["E-991"]` |

**別ベンダーが、同じ簡報から独立に同じ本質的欠陥を指摘した。**
席（role）に対して座り手を差し替えても、パイプラインは同一に動く。

---

## 6. この検証で確定したこと

| 主張 | 状態 |
|---|---|
| 実行と判定の分離で偽failが消える | ⚠ **N=1**（16行1本。かつ「レビュア環境起因の偽fail」1経路のみ） |
| 裁定4分類が機能する | ⚠ **合成入力の単体テスト**（LLM・CVE・CLI 不関与） |
| 証拠なき指摘の破棄が機能する | ⚠ **合成入力**。ただし後日の独立レビューで実例が1件出た（`docs/design-notes/review-response.md` §3.1） |
| レビュア役はベンダー差し替え可能 | ⚠ **N=1**（同じ16行題材で codex/agy） |
| 簡報の埋め込みが判定独立性に必須 | ⚠ **N=1**（§4の落とし穴） |

> ⚠ **本節の全項目は「小さな題材1件」の結果である。**
> 依存関係の複雑なタスク・大きな差分・複数ランタイムでの成立は**未検証**。
> 当初この表は全項目を「実証済み」と記載していたが、独立レビューの指摘を受けて訂正した
> （`docs/design-notes/review-response.md`）。

## 7. 再現方法

```bash
cd D:/vagrant/harnesses/super-agent/probe/ws
python fizz.py > evidence_E991.txt 2>&1; echo "exit_code=$?" >> evidence_E991.txt   # ①
codex exec --json --output-schema reviewer_schema.json -s read-only "$(cat briefing.txt)"  # ②
python adjudicate.py reviewer_out.json 0                                             # ③
```
