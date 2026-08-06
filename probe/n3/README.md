# probe/n3 — N=4 実測と大きな差分への対処（実験の証跡）

**これは製品コードではなく、`docs/N3_AND_LARGE_DIFF.md` の主張を再現するための証跡である。**

## 中身

| ファイル | 役割 |
|---|---|
| `cve.py` | **CVE**（唯一の検証環境）。実行し、証拠に `tree_hash` を束縛する（H4対応） |
| `brief.py` | **簡報生成**。予算内で T0証拠 > T1変更 > T2署名 > T3全文 の順に劣化させる |
| `adjudicate2.py` | **裁定器**（機械のみ、LLM不関与）。証拠なき指摘は `advisory` へ退避 |
| `rev_schema.json` | レビュアに強制する出力スキーマ |
| `caseB/` | 2ファイル相互依存。**テストが通るのに欠陥が残る**題材 |
| `caseC/` | 3ファイル相互依存。**テストが落ちる実バグ**（`range(attempts-1)`） |
| `caseD/` | 42ファイル生成物。**大きな差分**の予算検証用（ソースは再生成する） |

## 前提

CVE 用の venv が要る（本体の venv とは分離する。これが CVE の役目）:

```bash
uv venv .cve-venv
uv pip install --python .cve-venv/Scripts/python.exe pytest
```

`case*/cve.json` の python 絶対パスは**環境に合わせて書き換えること**。

## 再現

```bash
PY=<host python>

# 1. CVE が実行し、証拠を固定する
$PY cve.py caseC caseC/cve.json > caseC/evidence.json

# 2. 予算内で簡報を組む（落としたものは OMITTED に明記される）
$PY brief.py caseC 800 "util/retry.py"

# 3. レビュアは read-only。証拠を読むだけで、実行しない
codex exec --json --output-schema rev_schema.json -s read-only "$(cat caseC/brief_budgeted.txt)"

# 4. 裁定は機械が行う（レビュアの opinion は参考値）
$PY adjudicate2.py caseC/evidence.json caseC/rev_codex.json
```

### caseD の再生成

`caseD` のソースは容量のため未収録。`docs/N3_AND_LARGE_DIFF.md` §4 の手順で
42ファイルを生成し、`pkg/mod00.py` の `op0_0` にシグネチャ変更を入れる。

## 主要な結果

- **Case B**: `4 passed` でも欠陥が残る（可変Money・恒真テスト）→ **acceptance が正しさの天井**
- **Case B**: レビュア意見が `fail`/`pass` に割れても **機械裁定は一致**
- **Case C**: 実バグを **729トークン**の簡報で根本原因まで特定
- **Case D**: 9,612 → 820トークン（**-91%**）でも正答維持
