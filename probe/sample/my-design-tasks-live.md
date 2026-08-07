# タスク分解（decompose 出力）

要求: リポジトリ直下に live_probe.txt を作成し、内容を hello from live probe とする（実装のみ、テスト不要）

タスク数: 1

## 1. TLIVE

- 目標: リポジトリ直下に `live_probe.txt` を作成し、内容を `hello from live probe` とする。さらに `tests/test_live_probe.py` を作成し、live_probe.txt の存在と内容を assert するテストを書く。標準ライブラリのみ使用。
- 依存: （なし）
- 触ってよい範囲: live_probe.txt, tests/test_live_probe.py
- 受入基準 (1):
  - `pytest` tests/test_live_probe.py (expect_exit=0)

