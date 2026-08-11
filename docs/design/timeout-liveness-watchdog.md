# 設計: ACPを使わないタイムアウト対策(liveness監視)とダッシュボードのリアルタイム進捗表示

## 概要
現状の `invoke()`/`implementer.py` は vendor サブプロセスを `subprocess.run()` で1回叩き、
プロセスが終了するまで完全にブラックボックスで待つ「壁時計タイムアウト一発勝負」になっている
(`DEFAULT_TIMEOUT=1800`、`harness/core/invoke.py:588-648`、`harness/roles/implementer.py:178-192`)。
進行中でも殺すと成果が消えるため、長時間の呼び出しほど「本当にハングしているのか、単に時間が
かかっているだけなのか」を区別できない。

ACP(Agent Client Protocol)への全面移行は不採用(claude/codexのみ公式アダプター経由で対応、
agy/hermesは非公式または不整合が大きく、常駐JSON-RPCクライアントへの書き換えはコストに見合わない)。
代わりに、各ベンダーCLIが既に持つ「ストリーミングJSON出力」または「ログtail」機能を使い、
一定時間 **活動(activity)がない場合のみ** 停止・リトライする idle-timeout 方式に切り替える。
あわせて、この liveness 情報を dashboard のリアルタイム進捗表示にも使う。

## 各ベンダーの実測結果 (2026-08-11)
詳細は `CLAUDE.md` の「ハマりポイント」に記録済み。要約:

| vendor | 使うフラグ | イベント形式 | 終端イベント | 備考 |
|---|---|---|---|---|
| claude | `--verbose --output-format stream-json` (`--verbose`必須) | NDJSON, `type`フィールド | `type:"result"` → `result` に最終テキスト | `--verbose`でhookイベント等も混じる。未知typeは無視して素通しする実装にする |
| agy | `--output-format stream-json` を `--print` より**前**に配置(順序厳守) | NDJSON, `event`フィールド | `event:"result"` → `result.response` | 順序を誤ると指示を無視した的外れな応答を返す(実測で確認)。vendors.yamlの`headless`テンプレート自体を直す必要あり |
| codex | `codex exec --json` | NDJSON, `type`フィールド(`thread.started`/`turn.started`...) | 未確認(実測時はモデル過負荷で`turn.failed`しか見られず) | 正常終端のイベント名は実装時に追加実測が必要。`--output-schema`/`prompt_stdin`との併用可否も未検証 |
| hermes | `--pass-session-id` | ストリーミングなし。stdout1行目に`session_id: <callee生成id>`が出るのみ | (なし。プロセス終了で完了) | 呼び出し前にsession_idを指定する手段はない。1行目でid確定後、別プロセス`hermes logs -f --session <id>`を並走させてliveness検知する専用経路が必要 |

## アーキテクチャ

### 0. progress サイドチャネル(新設)
`Ledger.append_event()` は呼ぶたびに全chunkを読み直し `_rewrite()` で全ファイル書き直しをするため
(`harness/core/ledger.py:112-150`)、ストリーミングイベントを `Sequencer.propose()` 経由で ledger に
流すと長時間タスクで書き込み増幅になる。よってheartbeatはledgerを経由しない。

- 新規: `harness/ledger/progress/<task_id>.json`(sub-channel名含む、上書き方式)
- 内容: `{last_activity_ts, detail, vendor, status: "running"|"done"}`
- ledger には従来どおり開始/終了などのマイルストーンイベントのみ記録する(変更なし)

### 1. invoke.py: ストリーミング対応
- `invoke()`/`build_command()` に `progress_cb` と新規 `idle_timeout` を追加。既存 `timeout` は
  「絶対上限のセーフティネット」として残す(idle-timeoutが主、timeoutは保険なので現行より緩めてよい)。
- `subprocess.run` → `subprocess.Popen(stdout=PIPE)` + リーダースレッド + `queue.Queue` に変更
  (Windowsはパイプを`select()`できないため、リーダースレッドがqueueに行を積み、メインは
  `queue.get(timeout=idle_timeout)` で待つ形にする。空になったら stall と判定してプロセスを終了)。
- ベンダー別パーサは上表の実測結果に基づいて実装。未知の `type`/`event` は無視して素通しする
  (ベンダー側の将来の型追加に耐える設計にする)。
- `extract_result()` によるstdoutスクレイピングはフォールバックとして残す。

### 2. hermes専用経路
- `--pass-session-id` を追加。stdout 1行目の `session_id: <id>` をパースしたら、別Popenで
  `hermes logs -f --session <id>` を並走させ、そのログ行をliveness heartbeatとしてprogressチャネル
  に流す。主プロセスのstdout自体は完了まで無音なので、liveness判定は専らlogtail側で行う。

### 3. implementer.py
- 現状 `invoke()` を経由せず独自に `subprocess.run` している(`cwd=worktree_path` のため、
  `harness/roles/implementer.py:178-192`)。`invoke()` に `cwd` 引数を追加して統合し、二重実装の
  乖離を解消する。
- `task_id`(sub-channel名含む、例 `PA__hermes_0`)をprogressファイル名のキーにする。

### 4. dashboard.py
- `load_progress()` を追加。`is_stale` 判定を `max(ledger updated_at, progress last_activity_ts)`
  基準に変更する。現状は非終端タスクの `updated_at` が最初の `task.leased` のまま固まるため、長時間
  進行中の単発implement呼び出しが「実は進行中なのにstale扱い」される穴があり、これを修正する。
- HTML/MDに「最終活動: N秒前」的な表示を追加する。

### 5. cli.py
- `dashboard --watch [--interval N]` を追加。ループで再生成するだけ(サーバ/websocket不要)。
  HTML出力には `<meta http-equiv="refresh">` を入れてブラウザ側でauto reloadさせる。

## 影響範囲
- harness/core/invoke.py
- harness/roles/implementer.py
- harness/roles/dashboard.py
- harness/config/vendors.yaml (agyの`headless`テンプレート順序変更、hermesの`--pass-session-id`追加)
- harness/cli.py (`dashboard --watch`)
- harness/tests/test_invoke.py, test_implementer.py, test_dashboard.py

## 未検証で実装時に潰す項目
- codexの正常終端イベント名(`turn.completed` 等、モデル過負荷で未確認)
- codexの `--json` と `--output-schema`/`prompt_stdin` 併用可否
- agyの `stream-json` がプロンプト内JSON指示との相性(既存の「`--output-format json` は説明モードに
  入る」問題の再来がないか)

## 実装順序
0. progress サイドチャネルの土台
1. invoke.py のストリーミング対応(claude/agy/codex)
2. hermes専用経路(session_id確定 → log tail並走)
3. implementer.py を invoke() 経由に統合
4. dashboard.py のprogress取り込み・is_stale改善
5. cli.py の `dashboard --watch`
6. 各ベンダーNDJSONパーサ・idle-timeout・stale判定のユニットテスト
