# タスク分解（decompose 出力）

要求: コマンドラインでテキストを単語数カウントする小さなツールを作る

タスク数: 2

## 1. T1

- 目標: wclite パッケージの雛形と count_words(path_or_text) を実装する。文字列そのものと既存ファイルパスの両方を受け付け、空白区切りの単語数を返す。標準ライブラリのみ使用。
- 依存: （なし）
- 触ってよい範囲: pyproject.toml, wclite/__init__.py, wclite/core.py, tests/test_core.py
- 受入基準 (1):
  - `pytest` tests/test_core.py (expect_exit=0)

## 2. T2

- 目標: CLI エントリ main(argv) を実装する。argv にファイルパスがあればそれを読み、なければ標準入力を読んで count_words の結果を標準出力へ出力する。エントリポイント名は wc-lite。
- 依存: T1
- 触ってよい範囲: wclite/cli.py, tests/test_cli.py, pyproject.toml
- 受入基準 (1):
  - `pytest` tests/test_cli.py (expect_exit=0)

