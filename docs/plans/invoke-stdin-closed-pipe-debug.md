# invoke stdin closed-pipe デバッグ計画

対象: `src/harness/core/invoke.py` `_run_hermes` の stdin 修正（`_closed_pipe_read_end`）と `src/harness/tests/test_invoke.py` の追随。
前提実測 (2026-09-11): `harness/tests/test_invoke.py` 39 passed。未コミット3件のうち `vendors.yaml` はユーザー所有のため対象外。

## 1. 対象の特定
- 修正点: `_run_hermes` が `stdin=DEVNULL` ではなく closed-pipe の read end を渡す。Windows の NUL は `isatty()==True` を報告し、hermes contributor-tier の consent gate が headless 呼び出しを `Model override cancelled.` (exit 1) で落とすため。
- 対象外: `vendors.yaml`（35行差分）は読むだけ。write/patch/`git checkout`/`git reset --hard` 禁止。

## 2. 再現手順（DEVNULL vs closed-pipe 比較、live 2回まで）
```bash
cd D:/vagrant/harnesses/super-agent/src
CVE=D:/vagrant/harnesses/super-agent/.cve-venv/Scripts/python.exe
# 実コマンドは build_command の出力を使用（hermes chat -q ... -Q --provider opencode-free -m ... --reasoning high）
# A: DEVNULL 再現（失敗側の証拠、1回のみ）— 実測 2026-09-11: exit 1 + "Model override cancelled."
# B: closed-pipe 確認（成功側、1回のみ）— 実測 2026-09-11: exit 0、stdout に OK、stderr に
#   "Proceeding in non-interactive mode because security.allow_data_training_tiers_noninteractive is true."
```
- 2回で終わらせる。追加の live は必要時のみ1回追加（上限3回）。
- live 実績（2026-09-11、本計画の承認後に実行）: A=exit 1 cancel再現、B=exit 0 OK。2回で確定、3回目は未使用。

## 3. 修正の検証（headless 1ショット）
```bash
$CVE -m harness.cli implement --help 2>&1 | head -n 5; echo EXIT=$?
$CVE -m pytest harness/tests/test_invoke.py -q 2>&1 | tail -n 3; echo EXIT=$?
```
- 判定: pytest 全緑 + help の exit 0。hermes 実呼び出し B が exit 0 なら修正有効。

## 4. 回帰の網羅
- `harness/tests/test_invoke.py` 全件（現状 39 passed を基準に増減なしを確認）。
- 全体スイートは `harness/tests` の invoke 関連のみに限定（時間・コスト抑制）。全体を回す場合は別途承認を得る。

## 5. vendors.yaml 不干渉ルール
- 許可: `git status --short` / `git diff --stat` での状態確認、`read_file` での参照。
- 禁止: `write_file`/`patch`/`git checkout`/`git reset` の vendors.yaml への適用。`SUPER_AGENT_TEST=1` なしの drive live 実行（stash/checkout を伴うため）。

## 6. ブランチ・コミット規律
- ブランチ: `feat/invoke-stdin-closed-pipe`（未作成。作成前に承認を得る）。
- コミットメッセージは日本語。stage + commit 前に差分サマリを提示し、明示承認後に実行。

## 7. コスト配慮
- live hermes 呼び出し上限3回、モデルは `muse-spark-1.3-contributor-free`（現 vendors.yaml の hermes 既定）に固定。
- 順序: dry-run/単体テスト → live A/B の順。live でしか確かめられない所（consent gate の挙動）のみ live を使う。

## 8. stdout / exit code 契約
- 各手順は `echo EXIT=$?` を付け、成否を exit code で判定。結果テキストは stdout に残す。JSON 整形はしない（生テキストで足りる）。
- 次工程: 本計画の live A/B 結果を添えて修正の keep/revert を判断する。
