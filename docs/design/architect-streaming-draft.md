# 設計: architect のストリーミング下書き (.draft) による途中成果の保存とクロスベンダー自動復元

## 概要

`architect` コマンド（Stage 1）の実行中にタイムアウト（`TimeoutExpired`）やネットワークエラー、プロセス中断が発生すると、それまでに LLM がリポジトリ探索や思考・生成した途中成果がすべて破棄され、再実行時にゼロからのやり直しになってしまう問題がありました。

本設計では、ベンダー（`claude`, `codex`, `agy`, `hermes`）からストリーミングされる LLM の回答本文（思考過程・設計提案テキスト）をリアルタイムに下書きファイル (`.draft`) へ同期保存します。途中で異常終了した場合でも、次回再実行時にどのベンダーからでも（クロスベンダー対応）前回の試行内容を自動でコンテキストへ読み込み、中断された箇所から補完・完結できるようにします。

---

## 基本設計方針

1. **ベンダー非依存（クロスベンダー対応）**
   - 下書きファイル (`.draft`) には JSON メタフレームや CLI のデバッグログではなく、抽出・再構成された純粋な「設計提案本文（Markdown / 自然言語）」のみを書き込みます。
   - 再実行時は LLM プロンプトのコンテキスト（`existing_design`）として引き継ぐため、「前回 `claude` で中断し、次回 `agy` や `codex` で再実行する」といったベンダーを跨いだ再試行にも完全に対応します。

2. **アトミック保存によるファイル破損防止**
   - プロセスが突然 kill されたりタイムアウトで中断されたりした場合でも、不完全・破損したファイルが残らないよう、一時ファイル作成と `os.replace` によるアトミック上書き更新（Atomic Flush）を行います。

3. **完了時の安全なクリーンアップ**
   - 最終的に JSON の抽出、正式な `design_file` (`.md`) のディスク書き出し、および Ledger への `adr.written` イベント記録が正常に成功した直後に `.draft` ファイルを削除します。

---

## 詳細仕様

### 1. 下書きファイルの命名・パス規約
- 対象の設計ファイルパスが `--design_file design/feature.md` の場合、下書きファイルは **`design/feature.md.draft`** とします。
- `--design_file` 未指定で自走命名される場合（例: `design/my-req.md`）も、対応する `design/my-req.md.draft` へアトミック保存を行います。

### 2. ストリーミング応答の本文抽出と安全な下書き同期 (`invoke.py`)
各ベンダーのストリーミング受信ループにおいて、デバッグログや JSON フレーム等のメタデータではなく、**パーサが抽出・再構成した「LLMの回答本文（設計ドラフトテキスト）」のみ**を特定して書き込み対象とします。

プロセスの中断時にファイルが破損（切り詰め・不完全な書き込み）するのを防ぐため、下書きファイル (`draft_path`) への反映は、一時ファイル (`.tmp`) 作成と `os.replace` による**アトミック上書き保存（Atomic Flush）**で行います。

- **`claude` / `agy`**: 受信した `text_delta` から累積構築された最新の回答本文テキストをアトミックに上書き保存。
- **`codex`**: `agent_message` イベントから取得した最新の回答本文テキストをアトミックに上書き保存。
- **`hermes`**: 応答本文（`stdout`）または logtail から得られた中間テキストをアトミックに上書き保存。

### 3. 再実行時の自動引き継ぎ・コンテキスト補元 (`architect.py`)
`architect.propose()` 呼び出し時、`spec_path` に対応する `.draft` ファイルが存在するかを事前確認します。

`.draft` ファイルが存在する場合：
1. `.draft` 内のテキストを読み出します。
2. `ARCHITECT_PROMPT` を拡張し、前回の途中成果を引き継ぎコンテキストとして注入します。

```text
既存の設計: {existing_design}

【前回の試行で途中まで作成された設計ドラフト（引き継ぎ用）】
---
{draft_text}
---

指示:
前回の検討で作成された上記ドラフトの内容を検証・補足・完結させ、
重複を避けつつ正しいフォーマットの JSON (decisions / open_questions) を出力してください。
```

### 4. クリーンアップとエラーハンドリング
- **正常終了**: `design/feature.md` の保存および Ledger への `adr.written` 記録が完了した直後に `design/feature.md.draft` を削除します。
- **異常終了（Timeout/Exit Non-Zero）**: `.draft` ファイルは削除せずにディスクに残し、`architect.error` イベントおよび `progress` サイドチャネルに `draft_saved: design/feature.md.draft` を記録します。

---

## コンポーネント別変更点

### 1. `src/harness/core/invoke.py`
- `_run_streaming()` および `_run_hermes()`、`invoke()` に `draft_path: str | Path | None = None` パラメータを追加。
- 本文テキストの更新を検知するたび、`draft_path` に対する `atomic_write_draft(draft_path, text)` を実行。

### 2. `src/harness/roles/architect.py`
- `propose()` にて `spec_path` から `.draft` パスを算出。
- `.draft` の存在チェックとプロンプト注入処理を追加。
- `_invoke_design()` 経由で `invoke()` に `draft_path` を渡す。
- 成功時に `.draft` ファイルを `unlink()` で安全消去。

### 3. `src/harness/tests/test_architect_draft.py` (新規)
- モックベンダーを用いた以下の自動テストを追加：
  - 途中失敗時に `.draft` ファイルが正しく残るかの検証。
  - 再実行時に `.draft` 内のテキストがプロンプトに注入されるかの検証（クロスベンダー引き継ぎのテスト）。
  - 正常終了時に `.draft` が消えて正式な `.md` が生成されるかの検証。
