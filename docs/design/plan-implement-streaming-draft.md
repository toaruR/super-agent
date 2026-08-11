# 設計: plan および implement のストリーミング下書き (.draft) による途中成果の保存とクロスベンダー自動復元

## 概要

`architect` サブコマンド（Stage 1）で導入された `.draft` ファイルによる「ストリーミング下書き保存」および「次回実行時の自動復元・引き継ぎ」のリカバリ機構を、`plan`（タスク分解 `decomposer.py`）および `implement`（コード実装 `implementer.py`）へ拡張します。

`plan` や `implement` 実行中にタイムアウト（`TimeoutExpired`）やネットワークエラー、プロセス中断が発生した場合でも、それまでに LLM が思考・出力した途中成果を失うことなく下書きとして自動保存し、再実行時に 4 つの CLI ベンダー（`claude`, `codex`, `agy`, `hermes`）のいずれからでもシームレスに引き継いで再開できるようにします。

---

## 基本設計方針

1. **ベンダー非依存（クロスベンダー対応）**
   - ストリーミング下書き (`.draft` / `.implement_draft`) には、`invoke.py` の安全なストリーミング層が抽出・再構成した純粋な出力テキストを記録します。
   - 再実行時は LLM プロンプトのコンテキストへ埋め込んで引き継ぐため、前回試行と異なる CLI ベンダーで再実行した場合でも完全に動作します。

2. **アトミック保存によるファイル破損防止**
   - 下書き書き出しは `invoke.py` の `atomic_write_draft`（`.tmp` ファイル書き出し後の `os.replace`）を使用し、プロセス中断時でもファイル破損を防ぎます。

3. **完了時の安全なクリーンアップ**
   - 各サブコマンドの処理（`plan` における `tasks.md` の確定・`task.created` 記録、`implement` における Git コミット・`task.implemented` 記録）が正常完了した直後にドラフトファイルを自動削除します。

---

## 詳細仕様

### 1. `plan` (decomposer) におけるリカバリ仕様

- **下書きパス**: `--task_file <path>` (例: `design_tasks/feature.md`) に対し、`Path(f"{task_file}.draft")` (例: `design_tasks/feature.md.draft`) を作成。
- **ドラフト復元**: `task_file.draft` が存在する場合、その内容を読み込み `DECOMPOSE_PROMPT` の `existing_design` に以下の注意書き付きで追記：
  ```text
  【前回の試行で途中まで作成されたタスク分解ドラフト（引き継ぎ用）】
  ---
  {draft_text}
  ---
  前回の検討内容を検証・補足・完結させ、重複を避けつつ正しいフォーマットの JSON を出力してください。
  ```
- **リアルタイム保存**: `decomposer.decompose()` 内の `invoke(...)` に `draft_path=str(draft_p)` を引き渡す。
- **クリーンアップ**: タスク分解および検証（`structural_check`）が成功し、`task.created` イベント記録または `tasks.md` 保存が完了した段階で `draft_p.unlink()` を実行。

### 2. `implement` (implementer) におけるリカバリ仕様

- **下書きパス**: タスク固有の作業ツリー `worktree_path` (例: `workspaces/T1` や `workspaces/T1__claude_0`) 配下に `Path(worktree_path) / ".implement_draft"` を作成。
  - Note: Git コミット対象は `touch_allow` にあるパスのみであるため、`.implement_draft` が誤って Git コミットされる心配はありません。
- **ドラフト復元**: `.implement_draft` が存在する場合、その内容を読み込み `IMPLEMENT_PROMPT` の `design_context` に以下の注意書き付きで追記：
  ```text
  【前回の実装試行での思考ログ・ドラフト（引き継ぎ用）】
  ---
  {draft_text}
  ---
  前回の試行内容を参考に、未完了の部分を完成させ、指示に従って実装を行ってください。
  ```
- **リアルタイム保存**: `implementer.implement()` 内の `invoke(...)` に `draft_path=str(draft_p)` を引き渡す。
- **クリーンアップ**: 実装および `_commit_worktree` が正常完了し `task.implemented` を記録した直後に `draft_p.unlink()` を実行。

---

## 影響範囲とテスト計画

1. **本番コード**:
   - `src/harness/roles/decomposer.py`
   - `src/harness/roles/implementer.py`

2. **テストコード**:
   - `src/harness/tests/test_decomposer.py` (`test_decomposer_draft_saved_and_resumed`)
   - `src/harness/tests/test_implementer.py` (`test_implementer_draft_saved_and_resumed`)
